"""Unit tests for the browser_use pack.

Covers:

- Pack manifest loads cleanly and declares ``requires_sidecar``.
- ProfileManager creates dirs with mode 0700 and refuses drifted modes.
- secrets.assert_keyfile_safe refuses missing / world-readable keyfiles.
- SecretBundle.scrub redacts decrypted values from logs.
- SidecarClient surfaces connect errors as SidecarUnreachable.
- Handler refuses to start when the sidecar is unreachable.
- .gitignore covers the two new dirs.

The actual age binary and the sidecar's HTTP API are mocked — these
are unit tests, not integration tests.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from router.packs.loader import PackError, load_pack

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "browser_use"

# Make the pack importable as a flat ``helpers.*`` and ``handler`` package.
# The handler does the same path mutation at runtime; doing it once here
# at module import means all three helpers share one module identity
# across the test file.
if str(PACK_DIR) not in sys.path:
    sys.path.insert(0, str(PACK_DIR))


@pytest.fixture(scope="module")
def secrets_mod():
    return importlib.import_module("helpers.secrets")


@pytest.fixture(scope="module")
def profile_mod():
    return importlib.import_module("helpers.profile_manager")


@pytest.fixture(scope="module")
def sidecar_mod():
    return importlib.import_module("helpers.sidecar_client")


# ── Pack-shape guards ────────────────────────────────────────────────


class TestPackShape:
    def test_manifest_loads_cleanly(self) -> None:
        pack = load_pack(PACK_DIR)
        assert pack.name == "browser_use"
        # No env-injected secrets at dispatch time.
        assert pack.needs == []
        # Write verbs require approval; reads bypass it.
        assert set(pack.approve) == {"submit", "post", "apply", "purchase"}
        assert pack.description.strip()

    def test_manifest_declares_sidecar(self) -> None:
        pack = load_pack(PACK_DIR)
        assert pack.requires_sidecar is True
        assert pack.sidecar_service_name == "browser-use"
        assert pack.sidecar_compose_profile == "browser"

    def test_companion_files_exist(self) -> None:
        pack = load_pack(PACK_DIR)
        assert pack.prompt_path is not None
        assert (PACK_DIR / "handler.py").exists()
        assert (PACK_DIR / "helpers" / "sidecar_client.py").exists()
        assert (PACK_DIR / "helpers" / "profile_manager.py").exists()
        assert (PACK_DIR / "helpers" / "secrets.py").exists()
        assert (PACK_DIR / "README.md").exists()

    def test_prompt_mentions_sidecar_and_approval(self) -> None:
        text = (PACK_DIR / "prompt.md").read_text()
        assert "sidecar" in text.lower()
        assert "draft-approval" in text
        assert "browser_use" in text


class TestPackLoaderRequiresSidecarValidation:
    def test_requires_sidecar_without_service_name_is_an_error(self, tmp_path: Path) -> None:
        pack_dir = tmp_path / "broken_pack"
        pack_dir.mkdir()
        (pack_dir / "pack.yaml").write_text("name: broken_pack\ndescription: x\nrequires_sidecar: true\n")
        with pytest.raises(PackError, match="sidecar_service_name"):
            load_pack(pack_dir)


# ── Profile manager ──────────────────────────────────────────────────


class TestProfileManager:
    def test_invalid_name_raises(self, profile_mod) -> None:
        for bad in ("", "Foo", "../escape", "with/slash", "_leading", "-leading", "a." * 5):
            with pytest.raises(profile_mod.ProfileError):
                profile_mod.validate_name(bad)

    def test_valid_names_accepted(self, profile_mod) -> None:
        for good in ("linkedin-bram", "indeed_bram", "a", "a1", "a-b_c-d"):
            assert profile_mod.validate_name(good) == good

    def test_creates_profile_with_mode_0700(self, profile_mod, tmp_path: Path) -> None:
        # Force a permissive umask so we'd see drift if mkdir didn't enforce 0700.
        old_umask = os.umask(0o022)
        try:
            profile = profile_mod.ensure_profile("linkedin-bram", profiles_dir=tmp_path)
        finally:
            os.umask(old_umask)
        assert profile.path.exists()
        mode = profile.path.stat().st_mode & 0o777
        assert mode == 0o700, f"profile dir mode drifted to {mode:#o}"

    def test_reuse_existing_profile_with_correct_mode(self, profile_mod, tmp_path: Path) -> None:
        profile_mod.ensure_profile("indeed-bram", profiles_dir=tmp_path)
        again = profile_mod.ensure_profile("indeed-bram", profiles_dir=tmp_path)
        assert again.name == "indeed-bram"

    def test_drifted_mode_refused(self, profile_mod, tmp_path: Path) -> None:
        profile = profile_mod.ensure_profile("linkedin-bram", profiles_dir=tmp_path)
        os.chmod(profile.path, 0o755)
        with pytest.raises(profile_mod.ProfileError, match="mode"):
            profile_mod.ensure_profile("linkedin-bram", profiles_dir=tmp_path)

    def test_no_create_missing_raises(self, profile_mod, tmp_path: Path) -> None:
        with pytest.raises(profile_mod.ProfileError, match="does not exist"):
            profile_mod.ensure_profile("nope", profiles_dir=tmp_path, create_missing=False)

    def test_list_profiles_sorted(self, profile_mod, tmp_path: Path) -> None:
        for name in ("b-prof", "a-prof", "c-prof"):
            profile_mod.ensure_profile(name, profiles_dir=tmp_path)
        assert profile_mod.list_profiles(tmp_path) == ["a-prof", "b-prof", "c-prof"]


# ── Secrets ──────────────────────────────────────────────────────────


def _make_keyfile(tmp_path: Path, mode: int = 0o400) -> Path:
    keyfile = tmp_path / "age.key"
    keyfile.write_text(
        "# created by age-keygen\n"
        "# public key: age1exampletestpubkey0000000000000000000000000000000000000\n"
        "AGE-SECRET-KEY-EXAMPLE0000000000000000000000000000000000000000\n"
    )
    os.chmod(keyfile, mode)
    return keyfile


class TestKeyfileSafety:
    def test_missing_keyfile_raises(self, secrets_mod, tmp_path: Path) -> None:
        with pytest.raises(secrets_mod.SecretError, match="not found"):
            secrets_mod.assert_keyfile_safe(tmp_path / "absent.key")

    def test_world_readable_keyfile_raises(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path, mode=0o644)
        with pytest.raises(secrets_mod.SecretError, match="unsafe permissions"):
            secrets_mod.assert_keyfile_safe(keyfile)

    def test_group_readable_keyfile_raises(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path, mode=0o440)
        with pytest.raises(secrets_mod.SecretError, match="unsafe permissions"):
            secrets_mod.assert_keyfile_safe(keyfile)

    def test_safe_keyfile_passes(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path, mode=0o400)
        secrets_mod.assert_keyfile_safe(keyfile)  # no raise

    def test_user_only_rw_also_safe(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path, mode=0o600)
        secrets_mod.assert_keyfile_safe(keyfile)  # no raise

    def test_directory_in_place_of_keyfile_raises(self, secrets_mod, tmp_path: Path) -> None:
        not_a_file = tmp_path / "agekey"
        not_a_file.mkdir()
        with pytest.raises(secrets_mod.SecretError, match="not a regular file"):
            secrets_mod.assert_keyfile_safe(not_a_file)


class TestSecretBundleScrub:
    def test_scrub_replaces_known_values(self, secrets_mod) -> None:
        bundle = secrets_mod.SecretBundle(
            values={"TOKEN": "topsecret1234", "COOKIE": "abcdwxyz"},
            sources={"TOKEN": "/tmp/x.age", "COOKIE": "/tmp/x.age"},
        )
        scrubbed = bundle.scrub("got TOKEN=topsecret1234 and COOKIE=abcdwxyz")
        assert "topsecret1234" not in scrubbed
        assert "abcdwxyz" not in scrubbed
        assert scrubbed.count(secrets_mod.REDACTED) == 2

    def test_scrub_skips_short_values(self, secrets_mod) -> None:
        # A 1-char value would otherwise mangle every occurrence of that
        # letter in the log line. The scrubber skips values shorter than
        # 4 chars on purpose.
        bundle = secrets_mod.SecretBundle(values={"X": "a"})
        assert bundle.scrub("a quick brown fox") == "a quick brown fox"


class TestDecryptBlob:
    def test_missing_blob_raises(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path)
        with pytest.raises(secrets_mod.SecretError, match="not found"):
            secrets_mod.decrypt_blob(tmp_path / "missing.age", keyfile=keyfile)

    def test_age_binary_missing_raises(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path)
        blob = tmp_path / "x.age"
        blob.write_text("ciphertext")
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("no age")):
            with pytest.raises(secrets_mod.SecretError, match="on PATH"):
                secrets_mod.decrypt_blob(blob, keyfile=keyfile)

    def test_age_decrypt_failure_raises(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path)
        blob = tmp_path / "x.age"
        blob.write_text("ciphertext")
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"bad key")
        with patch.object(subprocess, "run", return_value=completed):
            with pytest.raises(secrets_mod.SecretError, match="bad key"):
                secrets_mod.decrypt_blob(blob, keyfile=keyfile)

    def test_age_decrypt_success_returns_plaintext(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path)
        blob = tmp_path / "x.age"
        blob.write_text("ciphertext")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"K=v\n", stderr=b"")
        with patch.object(subprocess, "run", return_value=completed):
            plaintext = secrets_mod.decrypt_blob(blob, keyfile=keyfile)
        assert plaintext == "K=v\n"


class TestLoadBundle:
    def test_no_blob_returns_empty_bundle(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path)
        bundle = secrets_mod.load_bundle("missing-profile", keyfile=keyfile, bundle_dir=tmp_path)
        assert bundle.values == {}

    def test_parses_env_lines(self, secrets_mod, tmp_path: Path) -> None:
        keyfile = _make_keyfile(tmp_path)
        bundle_dir = tmp_path / "bundles"
        bundle_dir.mkdir()
        blob = bundle_dir / "linkedin-bram.env.age"
        blob.write_text("ciphertext")

        plaintext = (
            b"# comment line - ignored\n"
            b"\n"
            b"FOO=hello-world-1234\n"
            b"BAR=another-secret-5678\n"
            b"malformed line without equals\n"
        )
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=plaintext, stderr=b"")
        with patch.object(subprocess, "run", return_value=completed):
            bundle = secrets_mod.load_bundle("linkedin-bram", keyfile=keyfile, bundle_dir=bundle_dir)

        assert bundle.values == {"FOO": "hello-world-1234", "BAR": "another-secret-5678"}
        assert bundle.sources["FOO"] == str(blob)


# ── Sidecar client ──────────────────────────────────────────────────


class _StubHttpClient:
    """Tiny stand-in for httpx.Client supporting `request` and `close`."""

    def __init__(self, exc: Exception | None = None, response: MagicMock | None = None) -> None:
        self._exc = exc
        self._response = response
        self.closed = False
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, url: str, *, json: dict | None = None):  # noqa: A002
        self.calls.append((method, url, json))
        if self._exc is not None:
            raise self._exc
        return self._response

    def close(self) -> None:
        self.closed = True


def _ok_response(status: int = 200, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"{}" if body is None else json.dumps(body).encode()
    resp.json.return_value = body or {}
    resp.text = ""
    return resp


class TestSidecarClient:
    def test_connect_error_surfaces_as_unreachable(self, sidecar_mod) -> None:
        stub = _StubHttpClient(exc=httpx.ConnectError("refused"))
        client = sidecar_mod.SidecarClient(base_url="http://browser-use:8080", client=stub)
        with pytest.raises(sidecar_mod.SidecarUnreachable, match="docker compose --profile browser"):
            client.health()

    def test_timeout_surfaces_as_unreachable(self, sidecar_mod) -> None:
        stub = _StubHttpClient(exc=httpx.ConnectTimeout("slow"))
        client = sidecar_mod.SidecarClient(base_url="http://browser-use:8080", client=stub)
        with pytest.raises(sidecar_mod.SidecarUnreachable, match="did not respond"):
            client.health()

    def test_5xx_surfaces_as_bad_response(self, sidecar_mod) -> None:
        resp = _ok_response(status=503, body={})
        resp.text = "upstream gone"
        stub = _StubHttpClient(response=resp)
        client = sidecar_mod.SidecarClient(base_url="http://browser-use:8080", client=stub)
        with pytest.raises(sidecar_mod.SidecarBadResponse, match="503"):
            client.invoke("navigate", {"url": "https://example.com"})

    def test_invoke_posts_to_action_path(self, sidecar_mod) -> None:
        resp = _ok_response(status=200, body={"ok": True})
        stub = _StubHttpClient(response=resp)
        client = sidecar_mod.SidecarClient(base_url="http://browser-use:8080", client=stub)
        result = client.invoke("navigate", {"url": "https://example.com"})
        assert result.status == 200
        assert result.body == {"ok": True}
        method, url, body = stub.calls[0]
        assert method == "POST"
        assert url == "http://browser-use:8080/api/navigate"
        assert body == {"url": "https://example.com"}

    def test_close_closes_owned_client(self, sidecar_mod) -> None:
        stub = _StubHttpClient(response=_ok_response())
        client = sidecar_mod.SidecarClient(client=stub)
        with client:
            client.health()
        # The stub wasn't created by SidecarClient (it's injected), so
        # _owned_client is False and the stub stays open. Sanity check.
        assert stub.closed is False


# ── Handler ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def handler_mod():
    return importlib.import_module("handler")


@pytest.fixture
def safe_keyfile(tmp_path: Path, monkeypatch):
    keyfile = _make_keyfile(tmp_path)
    monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(keyfile))
    return keyfile


class TestHandler:
    def test_unknown_action_exits_usage(self, handler_mod, capsys) -> None:
        rc = handler_mod.run(["weird-verb", "--profile", "x"])
        assert rc == handler_mod.EXIT_USAGE
        out = capsys.readouterr().out
        assert "unknown_action" in out

    def test_write_action_refused_without_approval(self, handler_mod, capsys) -> None:
        rc = handler_mod.run(["submit", "--profile", "linkedin-bram"])
        assert rc == handler_mod.EXIT_USAGE
        out = capsys.readouterr().out
        assert "approval_required" in out

    def test_missing_profile_for_non_health_exits_usage(self, handler_mod, safe_keyfile, capsys) -> None:
        rc = handler_mod.run(["navigate"])
        out = capsys.readouterr().out
        assert rc == handler_mod.EXIT_USAGE
        assert "missing_profile" in out

    def test_missing_keyfile_exits_secret_error(self, handler_mod, monkeypatch, capsys, tmp_path) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(tmp_path / "absent.key"))
        rc = handler_mod.run(["health"])
        err = capsys.readouterr().err
        assert rc == handler_mod.EXIT_SECRET_ERROR
        assert "keyfile" in err.lower()

    def test_world_readable_keyfile_exits_secret_error(self, handler_mod, monkeypatch, capsys, tmp_path) -> None:
        keyfile = _make_keyfile(tmp_path, mode=0o644)
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(keyfile))
        rc = handler_mod.run(["health"])
        err = capsys.readouterr().err
        assert rc == handler_mod.EXIT_SECRET_ERROR
        assert "unsafe permissions" in err

    def test_sidecar_unreachable_exits_with_hint(self, handler_mod, safe_keyfile, monkeypatch, capsys) -> None:
        # Point at an unreachable URL and let the real httpx fail to connect.
        monkeypatch.setenv("BROWSER_USE_SIDECAR_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("BROWSER_USE_SIDECAR_TIMEOUT", "0.5")
        rc = handler_mod.run(["health"])
        err = capsys.readouterr().err
        assert rc == handler_mod.EXIT_SIDECAR_UNREACHABLE
        assert "sidecar unreachable" in err.lower()


class TestHandlerLogScrub:
    """Verify the log filter redacts secret values from agent-visible output."""

    def test_scrub_filter_redacts_known_values(self, handler_mod, secrets_mod) -> None:
        """The filter rewrites record.msg before handlers format it.

        Install a StringIO-backed handler on the root logger BEFORE
        calling ``_install_scrub_filter`` so the filter attaches to it,
        then log a secret-bearing message and assert the StringIO never
        sees the plaintext.
        """
        import io as _io

        buffer = _io.StringIO()
        stream_handler = logging.StreamHandler(buffer)
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(logging.Formatter("%(message)s"))

        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(stream_handler)
        root.setLevel(logging.DEBUG)
        try:
            bundle = secrets_mod.SecretBundle(values={"TOKEN": "topsecret9999"})
            handler_mod._install_scrub_filter(bundle)
            logging.getLogger("test_scrub").warning("got TOKEN=topsecret9999")
        finally:
            root.removeHandler(stream_handler)
            root.setLevel(previous_level)

        emitted = buffer.getvalue()
        assert "topsecret9999" not in emitted, f"secret leaked into log output: {emitted!r}"
        assert secrets_mod.REDACTED in emitted


# ── Repo-wide guards ─────────────────────────────────────────────────


class TestGitignore:
    def test_browser_profiles_and_secrets_dirs_ignored(self) -> None:
        ignored = (REPO_ROOT / ".gitignore").read_text()
        # The acceptance criterion is that these dirs cannot be
        # accidentally tracked — match either with or without trailing
        # slash, but require the prefix.
        assert "config/browser_profiles" in ignored
        assert "config/secrets/browser" in ignored


class TestBootstrapScript:
    def test_script_is_executable(self) -> None:
        script = REPO_ROOT / "scripts" / "bootstrap-browser-secrets.sh"
        assert script.exists()
        assert script.stat().st_mode & 0o100, "bootstrap script must be executable"

    def test_script_mentions_age_and_keyfile(self) -> None:
        text = (REPO_ROOT / "scripts" / "bootstrap-browser-secrets.sh").read_text()
        assert "age-keygen" in text
        assert "AGE_KEYFILE" in text
        assert "/etc/ai-dev-team/age.key" in text


class TestComposeServiceShape:
    def test_browser_use_service_under_profile(self) -> None:
        import yaml

        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        svc = compose["services"].get("browser-use")
        assert svc is not None, "browser-use service missing from rendered compose"
        assert svc.get("profiles") == ["browser"], (
            "browser-use must be gated by the 'browser' compose profile so default `docker compose up` skips it"
        )
