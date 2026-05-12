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

    def test_close_does_not_close_injected_client(self, sidecar_mod) -> None:
        """When an httpx.Client is injected, the SidecarClient does not own it."""
        stub = _StubHttpClient(response=_ok_response())
        client = sidecar_mod.SidecarClient(client=stub)
        with client:
            client.health()
        # The stub wasn't created by SidecarClient (it's injected), so
        # _owned_client is False and the stub stays open.
        assert stub.closed is False

    def test_close_closes_owned_client(self, sidecar_mod) -> None:
        """When no client is injected, the SidecarClient owns its httpx.Client.

        Acceptance: PR review caller verifies the owned-client close path
        (``self._client.close()`` runs when ``_owned_client`` is True).
        """
        # Sub in a fake httpx.Client by patching the constructor — we
        # want to observe close() on the SidecarClient's *own* client.
        observed = MagicMock()
        observed.request = MagicMock(return_value=_ok_response())
        observed.close = MagicMock()
        with patch.object(httpx, "Client", return_value=observed):
            client = sidecar_mod.SidecarClient(base_url="http://browser-use:8080")
            with client:
                client.health()
        observed.close.assert_called_once()


# ── Handler ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def handler_mod():
    return importlib.import_module("handler")


class TestHandler:
    """Handler is the agent-facing CLI — no decryption, no keyfile checks."""

    def test_unknown_action_exits_usage(self, handler_mod, capsys) -> None:
        rc = handler_mod.run(["weird-verb", "--profile", "x"])
        assert rc == handler_mod.EXIT_USAGE
        out = capsys.readouterr().out
        assert "unknown_action" in out

    def test_write_action_refused_without_approval(self, handler_mod, capsys) -> None:
        """Approve list is read from pack.yaml — no drift with the manifest."""
        rc = handler_mod.run(["submit", "--profile", "linkedin-bram"])
        assert rc == handler_mod.EXIT_USAGE
        out = capsys.readouterr().out
        assert "approval_required" in out

    def test_each_approved_verb_is_refused(self, handler_mod, capsys) -> None:
        """Cross-check every verb declared in pack.yaml's `approve:` list."""
        approve = handler_mod._load_approve_list()
        assert approve, "pack.yaml's approve list is empty — drift check meaningless"
        for verb in approve:
            rc = handler_mod.run([verb, "--profile", "x"])
            assert rc == handler_mod.EXIT_USAGE, f"verb {verb!r} should refuse without approval"
            out = capsys.readouterr().out
            assert "approval_required" in out, f"verb {verb!r} did not produce approval_required"

    def test_missing_profile_for_non_health_exits_usage(self, handler_mod, capsys) -> None:
        rc = handler_mod.run(["navigate"])
        out = capsys.readouterr().out
        assert rc == handler_mod.EXIT_USAGE
        assert "missing_profile" in out

    def test_drifted_profile_mode_exits_profile_error(
        self, handler_mod, profile_mod, monkeypatch, tmp_path, capsys
    ) -> None:
        profiles_root = tmp_path / "profiles"
        monkeypatch.setenv("BROWSER_USE_PROFILES_DIR", str(profiles_root))
        # Pre-create with mode 0700, then chmod to 0755 to simulate drift.
        profile = profile_mod.ensure_profile("linkedin-bram", profiles_dir=profiles_root)
        os.chmod(profile.path, 0o755)
        rc = handler_mod.run(["navigate", "--profile", "linkedin-bram"])
        err = capsys.readouterr().err
        assert rc == handler_mod.EXIT_PROFILE_ERROR
        assert "mode" in err.lower()

    def test_sidecar_unreachable_exits_with_hint(self, handler_mod, monkeypatch, capsys) -> None:
        # Point at an unreachable URL and let the real httpx fail to connect.
        monkeypatch.setenv("BROWSER_USE_SIDECAR_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("BROWSER_USE_SIDECAR_TIMEOUT", "0.5")
        rc = handler_mod.run(["health"])
        err = capsys.readouterr().err
        assert rc == handler_mod.EXIT_SIDECAR_UNREACHABLE
        assert "sidecar unreachable" in err.lower()

    def test_does_not_import_secrets_helper(self, handler_mod) -> None:
        """The handler must not own the keyfile — keyfile is sidecar-only.

        Catches a regression where someone re-adds keyfile decryption to
        the handler (which would require mounting the keyfile into the
        agent container, blowing the security boundary).
        """
        # ``handler`` is loaded by the test bootstrap; check its
        # globals don't reference any name from helpers.secrets.
        for name in (
            "assert_keyfile_safe",
            "load_bundle",
            "resolve_keyfile",
            "SecretBundle",
            "SecretError",
        ):
            assert name not in handler_mod.__dict__, (
                f"handler should not import {name!r} from helpers.secrets — decryption belongs in the sidecar"
            )


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

    def test_browser_use_restart_policy_caps_retries(self) -> None:
        """`unless-stopped` would crashloop forever on a bad keyfile mount.

        Pin the policy to a bounded ``on-failure:N`` so the operator
        sees a quick crash + readable docker logs instead of a silent
        retry storm.
        """
        import yaml

        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        svc = compose["services"]["browser-use"]
        restart = svc.get("restart", "")
        assert restart.startswith("on-failure"), f"browser-use restart policy must cap retries; got {restart!r}"

    def test_browser_use_mounts_pack_at_opt_pack(self) -> None:
        """Sidecar imports ``browser_use_sidecar.server`` from the pack mount."""
        import yaml

        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        svc = compose["services"]["browser-use"]
        volumes = [v if isinstance(v, str) else v.get("target") for v in svc.get("volumes", [])]
        assert any("/opt/pack" in (v or "") for v in volumes), (
            "browser-use must mount the pack dir at /opt/pack for PYTHONPATH to resolve"
        )


# ── Sidecar FastAPI app ─────────────────────────────────────────────


def _have_fastapi() -> bool:
    try:
        import fastapi  # noqa: F401
        import starlette  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.fixture(scope="module")
def sidecar_server_mod():
    """Import the sidecar's server module.

    The server module imports ``helpers.*`` via the pack root that the
    test bootstrap already added to sys.path. The sidecar subpackage
    itself lives under that root.
    """
    if not _have_fastapi():
        pytest.skip("fastapi not installed in the test environment")
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    return importlib.import_module("browser_use_sidecar.server")


@pytest.fixture
def configured_sidecar(sidecar_server_mod, tmp_path, monkeypatch):
    """Point the sidecar's env knobs at a controlled tmp tree."""
    keyfile = _make_keyfile(tmp_path)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(mode=0o700)
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(mode=0o700)
    monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(keyfile))
    monkeypatch.setenv("BROWSER_USE_PROFILES_DIR", str(profiles_dir))
    monkeypatch.setenv("BROWSER_USE_SECRETS_DIR", str(bundles_dir))
    return {
        "keyfile": keyfile,
        "profiles_dir": profiles_dir,
        "bundles_dir": bundles_dir,
    }


class TestSidecarServer:
    def test_health_ok_with_safe_keyfile(self, sidecar_server_mod, configured_sidecar) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["keyfile"] == str(configured_sidecar["keyfile"])
        assert body["profiles_dir"] == str(configured_sidecar["profiles_dir"])

    def test_health_503_when_keyfile_missing(self, sidecar_server_mod, monkeypatch, tmp_path) -> None:
        from starlette.testclient import TestClient

        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(tmp_path / "absent.key"))
        with TestClient(sidecar_server_mod.app) as client:
            response = client.get("/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert "not found" in body["reason"].lower()

    def test_health_503_when_keyfile_world_readable(self, sidecar_server_mod, monkeypatch, tmp_path) -> None:
        from starlette.testclient import TestClient

        keyfile = _make_keyfile(tmp_path, mode=0o644)
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(keyfile))
        with TestClient(sidecar_server_mod.app) as client:
            response = client.get("/health")
        assert response.status_code == 503

    def test_api_missing_profile_returns_400(self, sidecar_server_mod, configured_sidecar) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post("/api/navigate", json={})
        assert response.status_code == 400
        assert "profile" in response.json()["detail"].lower()

    def test_api_navigate_returns_not_implemented_stub(self, sidecar_server_mod, configured_sidecar) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/navigate",
                json={"profile": "linkedin-bram", "url": "https://example.com"},
            )
        assert response.status_code == 200
        body = response.json()
        # Sidecar action wiring is stubbed in this PR — confirm the
        # placeholder shape so a future "actually drive Chromium"
        # change has an explicit failing test to remove.
        assert body["status"] == "not_implemented"
        assert body["action"] == "navigate"
        assert body["profile"] == "linkedin-bram"

    def test_api_unknown_action_returns_400(self, sidecar_server_mod, configured_sidecar) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/teleport",
                json={"profile": "linkedin-bram"},
            )
        assert response.status_code == 400
        assert "unknown action" in response.json()["detail"].lower()

    def test_api_drifted_profile_mode_returns_400(self, sidecar_server_mod, configured_sidecar, profile_mod) -> None:
        from starlette.testclient import TestClient

        profile = profile_mod.ensure_profile("linkedin-bram", profiles_dir=configured_sidecar["profiles_dir"])
        os.chmod(profile.path, 0o755)
        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/navigate",
                json={"profile": "linkedin-bram", "url": "https://example.com"},
            )
        assert response.status_code == 400
        assert "mode" in response.json()["detail"].lower()

    def test_api_scrubs_echoed_secrets_in_response(self, sidecar_server_mod, configured_sidecar) -> None:
        """Sidecar runs the response through bundle.scrub() before returning.

        Plant a secret in the bundle and write an action handler that
        echoes it back; the response we receive must contain
        [REDACTED], not the plaintext.
        """
        # Drop a fake decrypted bundle by stubbing load_bundle.
        from helpers.secrets import SecretBundle  # type: ignore
        from starlette.testclient import TestClient

        # The handler stub for "navigate" echoes the URL — so put the
        # secret in the URL field and see if scrub catches it.
        secret_value = "ABCDEFGHIJKLMNOP"
        fake_bundle = SecretBundle(
            values={"COOKIE": secret_value},
            sources={"COOKIE": "/test"},
        )

        def fake_load(_profile, **_kwargs):
            return fake_bundle

        with patch.object(sidecar_server_mod, "load_bundle", side_effect=fake_load):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": f"https://x.example/{secret_value}"},
                )
        assert response.status_code == 200
        body_text = json.dumps(response.json())
        assert secret_value not in body_text
        assert "[REDACTED]" in body_text


class TestPackTopology:
    """Architectural guards — keep the sidecar / handler split honest."""

    def test_sidecar_subpackage_exists(self) -> None:
        assert (PACK_DIR / "browser_use_sidecar" / "server.py").exists()
        assert (PACK_DIR / "browser_use_sidecar" / "__init__.py").exists()

    def test_dockerfile_sets_pythonpath_and_uvicorn_target(self) -> None:
        dockerfile = (REPO_ROOT / "docker" / "Dockerfile.browser").read_text()
        assert "PYTHONPATH=/opt/pack" in dockerfile, (
            "Dockerfile.browser must set PYTHONPATH=/opt/pack so the sidecar module resolves"
        )
        # CMD line points at the actual module we ship.
        assert "browser_use_sidecar.server:app" in dockerfile
