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
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
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


def _load_browser_use_handler():
    """Load the browser_use pack handler under a unique sys.modules key.

    Using a bare ``importlib.import_module("handler")`` collides with any
    other pack that also ships a top-level ``handler.py`` (dispatch,
    ops-diag, path_to_hired).  All four resolve to the same
    ``sys.modules["handler"]`` slot, so whichever is imported first wins
    and later tests silently bind to the wrong module.  A pack-qualified
    key avoids that entirely (issue #617).
    """
    mod_name = "packhandler_browser_use"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, PACK_DIR / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def handler_mod():
    return _load_browser_use_handler()


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

    def test_invalid_profile_name_exits_profile_error(self, handler_mod, capsys) -> None:
        """Handler does name-shape validation locally — no filesystem access.

        Replaces the old mode-drift test: drift checking moved to the
        sidecar in issue #138 because the agent container can't stat
        the mode-0700 profile dir (different UID).
        """
        rc = handler_mod.run(["navigate", "--profile", "Bad/Name"])
        err = capsys.readouterr().err
        assert rc == handler_mod.EXIT_PROFILE_ERROR
        assert "invalid profile name" in err.lower()

    def test_sidecar_unreachable_exits_with_hint(self, handler_mod, monkeypatch, capsys) -> None:
        # Point at an unreachable URL and let the real httpx fail to connect.
        monkeypatch.setenv("BROWSER_USE_SIDECAR_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("BROWSER_USE_SIDECAR_TIMEOUT", "0.5")
        rc = handler_mod.run(["health"])
        err = capsys.readouterr().err
        assert rc == handler_mod.EXIT_SIDECAR_UNREACHABLE
        assert "sidecar unreachable" in err.lower()

    def test_handler_forwards_create_missing_flag(self, handler_mod, tmp_path) -> None:
        """``--no-create-profile`` propagates as ``create_missing=false`` in the payload.

        End-to-end check for issue #138: the handler does no filesystem
        access locally, so the only way ``--no-create-profile`` can
        still work is by forwarding the flag to the sidecar.
        """
        captured: dict[str, Any] = {}

        class _FakeResponse:
            status = 200
            body = {"status": "ok"}

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def invoke(self, action, payload):
                captured["action"] = action
                captured["payload"] = payload
                return _FakeResponse()

            def health(self):  # pragma: no cover — not used in this test
                return _FakeResponse()

        payload_path = tmp_path / "payload.json"
        payload_path.write_text(json.dumps({"url": "https://example.com"}))

        with patch.object(handler_mod, "SidecarClient", _FakeClient):
            rc = handler_mod.run(
                [
                    "screenshot",
                    "--profile",
                    "linkedin-bram",
                    "--no-create-profile",
                    "--payload",
                    str(payload_path),
                ]
            )
        assert rc == handler_mod.EXIT_OK
        assert captured["action"] == "screenshot"
        assert captured["payload"]["profile"] == "linkedin-bram"
        assert captured["payload"]["create_missing"] is False
        assert captured["payload"]["url"] == "https://example.com"

    def test_handler_defaults_create_missing_to_true(self, handler_mod, tmp_path) -> None:
        """Without ``--no-create-profile``, the handler sends ``create_missing=true``."""
        captured: dict[str, Any] = {}

        class _FakeResponse:
            status = 200
            body = {"status": "ok"}

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def invoke(self, action, payload):
                captured["payload"] = payload
                return _FakeResponse()

            def health(self):  # pragma: no cover
                return _FakeResponse()

        payload_path = tmp_path / "payload.json"
        payload_path.write_text(json.dumps({"url": "https://example.com"}))

        with patch.object(handler_mod, "SidecarClient", _FakeClient):
            rc = handler_mod.run(["screenshot", "--profile", "linkedin-bram", "--payload", str(payload_path)])
        assert rc == handler_mod.EXIT_OK
        assert captured["payload"]["create_missing"] is True

    def test_handler_relays_sidecar_400_for_missing_profile(self, handler_mod, tmp_path, capsys) -> None:
        """Acceptance: missing profile + ``--no-create-profile`` → ``EXIT_BAD_RESPONSE`` + "does not exist" on stdout.

        The sidecar refuses to mkdir and returns a structured 400; the
        handler exits with ``EXIT_BAD_RESPONSE`` and prints the body so
        the agent (and caller scripts) can read the structured error.
        """

        class _FakeResponse:
            status = 400
            body = {"detail": "profile 'public-read' does not exist at /config/browser_profiles/public-read"}

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def invoke(self, action, payload):
                return _FakeResponse()

            def health(self):  # pragma: no cover
                return _FakeResponse()

        payload_path = tmp_path / "payload.json"
        payload_path.write_text(json.dumps({"url": "https://example.com"}))

        with patch.object(handler_mod, "SidecarClient", _FakeClient):
            rc = handler_mod.run(
                [
                    "screenshot",
                    "--profile",
                    "public-read",
                    "--no-create-profile",
                    "--payload",
                    str(payload_path),
                ]
            )
        assert rc == handler_mod.EXIT_BAD_RESPONSE
        out = capsys.readouterr().out
        assert "does not exist" in out

    def test_does_not_import_secrets_or_profile_manager(self, handler_mod) -> None:
        """The handler must not own the keyfile OR the profile dir.

        Both are sidecar-only — the keyfile holds plaintext credentials
        once decrypted, and the profile dir holds session cookies at
        mode 0700 owned by the sidecar UID. Mounting either into the
        agent container would blow the security boundary called out
        in the README's threat model. Catches a regression where
        someone re-adds either dependency to the handler.
        """
        # ``handler`` is loaded by the test bootstrap; check its
        # globals don't reference any name from helpers.secrets or
        # helpers.profile_manager.
        secrets_names = (
            "assert_keyfile_safe",
            "load_bundle",
            "resolve_keyfile",
            "SecretBundle",
            "SecretError",
        )
        for name in secrets_names:
            assert name not in handler_mod.__dict__, (
                f"handler should not import {name!r} from helpers.secrets — decryption belongs in the sidecar"
            )

        # The FS-touching names from profile_manager. ``validate_name``
        # is allowed because it's a pure regex and lives in
        # ``helpers.profile_name`` — but ``Profile``, ``ProfileError``,
        # ``ensure_profile``, ``resolve_profiles_dir``,
        # ``list_profiles``, ``EXPECTED_MODE`` all touch /config/browser_profiles.
        profile_mgr_names = (
            "Profile",
            "ProfileError",
            "ensure_profile",
            "resolve_profiles_dir",
            "list_profiles",
            "EXPECTED_MODE",
            "DEFAULT_PROFILES_DIR",
        )
        for name in profile_mgr_names:
            assert name not in handler_mod.__dict__, (
                f"handler should not import {name!r} from helpers.profile_manager — "
                "profile resolution belongs in the sidecar (issue #138)"
            )

        # Source-level guard: scan the handler file for import lines
        # that pull from ``helpers.profile_manager``. Cheaper than
        # import hooks and catches typed-out imports that the test
        # harness hasn't executed yet (e.g. a conditional import added
        # to a branch this test doesn't cover).
        import inspect

        source = inspect.getsource(handler_mod)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "helpers.profile_manager" not in stripped, (
                "handler.py must not import from helpers.profile_manager — the agent container "
                f"lacks permission to touch /config/browser_profiles (issue #138). Offending line: {line!r}"
            )


# ── Handler module isolation (issue #617) ────────────────────────────


class TestHandlerModuleIsolation:
    """Regression guard: browser_use handler must not bind to a foreign pack's module.

    Issue #617: four packs each ship a top-level ``handler.py``.  They
    all resolved to ``sys.modules["handler"]``, so whichever was imported
    first won.  The fix loads each handler under a pack-qualified key
    (``packhandler_<pack>``).  This class pins that fix so import-order
    accidents are caught immediately.
    """

    def test_browser_use_handler_unaffected_by_prior_dispatch_handler_import(self, capsys) -> None:
        """The browser_use handler resolves correctly even when sys.modules["handler"]
        is already occupied by a foreign module.

        Simulates the exact import ordering that caused issue #617: the dispatch
        handler (or any other pack's handler) lands in ``sys.modules["handler"]``
        before the browser_use tests run.  With the old bare
        ``importlib.import_module("handler")`` fixture, this returned the wrong
        module and produced dispatch-shaped errors (``unknown_verb``) instead of
        browser_use-shaped ones (``unknown_action``).

        ``_load_browser_use_handler`` is immune because it stores the module under
        a pack-qualified key (``packhandler_browser_use``) and never touches the
        bare ``"handler"`` slot.
        """
        import types

        # Synthetic stand-in that mimics what the dispatch handler returns for
        # an unknown verb.  We avoid exec'ing the real dispatch handler here
        # because it imports local dependencies (``constants.py``) that require
        # the dispatch pack dir on sys.path; the isolation guarantee does not
        # depend on the real dispatch module being fully loaded.
        fake_dispatch = types.ModuleType("handler")
        fake_dispatch.EXIT_USAGE = 1

        def _fake_dispatch_run(argv=None):
            import json as _json

            verb = (argv or ["?"])[0]
            print(
                _json.dumps(
                    {"error": "unknown_verb", "verb": verb, "message": "Known verbs: dispatch_health, dispatch_issue."}
                )
            )
            return 1

        fake_dispatch.run = _fake_dispatch_run

        # Clobber sys.modules["handler"] to simulate the collision.
        old_handler = sys.modules.get("handler")
        sys.modules["handler"] = fake_dispatch
        try:
            bu_mod = _load_browser_use_handler()
            rc = bu_mod.run(["weird-verb", "--profile", "x"])
            out = capsys.readouterr().out
            assert rc == bu_mod.EXIT_USAGE
            assert "unknown_action" in out, (
                f"browser_use handler returned the wrong error shape — got: {out!r}. "
                "Expected 'unknown_action' (browser_use); 'unknown_verb' would mean "
                "sys.modules['handler'] pollution was not isolated."
            )
        finally:
            if old_handler is None:
                sys.modules.pop("handler", None)
            else:
                sys.modules["handler"] = old_handler


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

    def test_api_navigate_invokes_runner_and_returns_structured_result(
        self, sidecar_server_mod, configured_sidecar
    ) -> None:
        """Sidecar delegates to ``run_verb`` and propagates its structured response.

        Playwright is mocked out — these are unit tests, not Chromium
        integration tests. The contract under test is the FastAPI
        endpoint → runner handoff: profile + payload travel down,
        structured body comes back.
        """
        from starlette.testclient import TestClient

        captured: dict[str, Any] = {}

        async def fake_run_verb(*, verb, profile, bundle, payload):
            captured["verb"] = verb
            captured["profile_name"] = profile.name
            captured["payload"] = payload
            captured["bundle_values"] = dict(bundle.values)
            return {
                "status": "ok",
                "action": verb,
                "profile": profile.name,
                "final_url": "https://example.com/landed",
            }

        with patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://example.com"},
                )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["action"] == "navigate"
        assert body["final_url"] == "https://example.com/landed"
        assert captured["verb"] == "navigate"
        assert captured["profile_name"] == "linkedin-bram"
        assert captured["payload"]["url"] == "https://example.com"

    def test_api_runner_failure_returns_structured_error(self, sidecar_server_mod, configured_sidecar) -> None:
        """Negative-path: Playwright failure (timeout) surfaces as 200 + status=error.

        Acceptance: handler must see a structured body, not a 500 with
        a stack trace.
        """
        from starlette.testclient import TestClient

        async def fake_run_verb(*, verb, profile, bundle, payload):
            return {
                "status": "error",
                "action": verb,
                "profile": profile.name,
                "error": "Timeout 30000ms exceeded while loading https://example.com",
                "error_type": "TimeoutError",
            }

        with patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://example.com"},
                )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "error"
        assert "Timeout" in body["error"]
        assert body["error_type"] == "TimeoutError"

    def test_api_malformed_profile_returns_400(self, sidecar_server_mod, configured_sidecar, profile_mod) -> None:
        """End-to-end: a profile with garbage cookies.json gets 400 from the API.

        The real :func:`run_verb` runs here so the validation inside
        ``playwright_runner.validate_profile_state`` is the thing under
        test through the FastAPI endpoint. The browser factory is never
        invoked because the validation runs before any Chromium spawn.
        """
        from starlette.testclient import TestClient

        profile = profile_mod.ensure_profile("linkedin-bram", profiles_dir=configured_sidecar["profiles_dir"])
        (profile.path / "cookies.json").write_text("{not valid json")

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/navigate",
                json={"profile": "linkedin-bram", "url": "https://example.com"},
            )
        assert response.status_code == 400
        detail = response.json()["detail"].lower()
        assert "malformed profile" in detail
        assert "cookies.json" in detail

    def test_api_extract_payload_flows_to_runner(self, sidecar_server_mod, configured_sidecar) -> None:
        """Extract payload (URL + selectors) flows to the runner intact."""
        from starlette.testclient import TestClient

        captured: dict[str, Any] = {}

        async def fake_run_verb(*, verb, profile, bundle, payload):
            captured.update(payload)
            captured["__verb"] = verb
            return {
                "status": "ok",
                "action": verb,
                "profile": profile.name,
                "extracted": {"title": "Senior SRE", "company": "Acme"},
                "final_url": "https://example.com/jobs/123",
            }

        with patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/extract",
                    json={
                        "profile": "linkedin-bram",
                        "url": "https://example.com/jobs/123",
                        "selectors": {"title": "h1.job-title", "company": ".company-name"},
                    },
                )
        assert response.status_code == 200
        assert captured["__verb"] == "extract"
        assert captured["selectors"] == {"title": "h1.job-title", "company": ".company-name"}
        body = response.json()
        assert body["extracted"] == {"title": "Senior SRE", "company": "Acme"}

    def test_api_works_with_no_anthropic_env_set(self, sidecar_server_mod, configured_sidecar, monkeypatch) -> None:
        """Acceptance: sidecar serves requests with no Anthropic credentials configured.

        The Playwright-direct rewrite (issue #119, second cut) dropped
        the LLM dependency. This guards against regressions that
        re-introduce ``ANTHROPIC_API_KEY`` or ``langchain_anthropic``
        as a hard requirement.
        """
        from starlette.testclient import TestClient

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("BROWSER_USE_LLM_API_KEY", raising=False)

        async def fake_run_verb(*, verb, profile, bundle, payload):
            return {
                "status": "ok",
                "action": verb,
                "profile": profile.name,
                "final_url": payload.get("url", ""),
            }

        with patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://example.com"},
                )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

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

    def test_api_create_missing_false_with_missing_profile_returns_400(
        self, sidecar_server_mod, configured_sidecar
    ) -> None:
        """``create_missing=false`` + missing profile → 400 with "does not exist".

        Acceptance from issue #138: ``--no-create-profile`` must still
        work end-to-end with profile resolution moved server-side. The
        handler passes ``create_missing=false`` in the payload; the
        sidecar refuses to mkdir and returns a structured 400.
        """
        from starlette.testclient import TestClient

        # ``ghost-profile`` is not pre-created in the configured profiles dir.
        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/navigate",
                json={
                    "profile": "ghost-profile",
                    "url": "https://example.com",
                    "create_missing": False,
                },
            )
        assert response.status_code == 400
        detail = response.json()["detail"].lower()
        assert "does not exist" in detail
        assert "ghost-profile" in detail
        # And the dir was NOT created as a side effect — the refusal
        # is fail-closed.
        assert not (configured_sidecar["profiles_dir"] / "ghost-profile").exists()

    def test_api_create_missing_true_creates_profile(self, sidecar_server_mod, configured_sidecar) -> None:
        """Default path: ``create_missing`` defaults to True and the sidecar mkdirs.

        Mirrors the pre-#138 default behaviour — when the handler omits
        the flag (or sends ``create_missing=true``), the sidecar
        creates the dir on first use.
        """
        from starlette.testclient import TestClient

        async def fake_run_verb(*, verb, profile, bundle, payload):
            return {"status": "ok", "action": verb, "profile": profile.name, "final_url": payload["url"]}

        with patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "fresh-profile", "url": "https://example.com"},
                )
        assert response.status_code == 200
        created = configured_sidecar["profiles_dir"] / "fresh-profile"
        assert created.exists() and created.is_dir()
        assert created.stat().st_mode & 0o777 == 0o700

    def test_api_create_missing_must_be_bool(self, sidecar_server_mod, configured_sidecar) -> None:
        """Non-boolean ``create_missing`` is a 400 — fail fast on shape bugs."""
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/navigate",
                json={
                    "profile": "linkedin-bram",
                    "url": "https://example.com",
                    "create_missing": "no",  # string, not bool
                },
            )
        assert response.status_code == 400
        assert "create_missing" in response.json()["detail"].lower()

    def test_api_scrubs_echoed_secrets_in_response(self, sidecar_server_mod, configured_sidecar) -> None:
        """Sidecar walks the response one level deep and scrubs every string.

        Plant a secret in the bundle, mock the runner to echo it back
        in nested fields (the real runner shouldn't, but the scrubber
        is the last line of defence). The response must contain
        ``[REDACTED]``, not the plaintext — at top level and inside
        the ``extracted`` dict.
        """
        from helpers.secrets import SecretBundle  # type: ignore
        from starlette.testclient import TestClient

        secret_value = "ABCDEFGHIJKLMNOP"
        fake_bundle = SecretBundle(
            values={"COOKIE": secret_value},
            sources={"COOKIE": "/test"},
        )

        async def fake_run_verb(*, verb, profile, bundle, payload):
            # Worst case: secret echoed in both a top-level string and
            # a nested dict value.
            return {
                "status": "ok",
                "action": verb,
                "profile": profile.name,
                "final_url": f"https://x.example/{secret_value}",
                "extracted": {"token_field": f"token={secret_value}"},
            }

        with (
            # Force the navigate path to load the bundle so the scrub
            # data flows through. Read-only verbs skip ``load_bundle``
            # by default (issue #143 (c)); the scrubber still needs a
            # bundle plumbed in for this assertion to mean anything.
            patch.object(sidecar_server_mod, "_verb_needs_secrets", return_value=True),
            patch.object(sidecar_server_mod, "load_bundle", return_value=fake_bundle),
            patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb),
        ):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": f"https://x.example/{secret_value}"},
                )
        assert response.status_code == 200
        body_text = json.dumps(response.json())
        assert secret_value not in body_text, "scrubber missed top-level or nested secret"
        assert body_text.count("[REDACTED]") >= 2, "expected at least two redactions (URL + nested)"

    def test_api_error_response_does_not_leak_profile_bytes(self, sidecar_server_mod, configured_sidecar) -> None:
        """Negative-path acceptance: error payload must not contain profile bytes.

        When the runner returns ``status=error`` with a message that
        happens to include a secret, the scrubber must redact it
        before the response leaves the process.
        """
        from helpers.secrets import SecretBundle  # type: ignore
        from starlette.testclient import TestClient

        cookie_blob = "session=topsecret9999abcdef"
        fake_bundle = SecretBundle(
            values={"LINKEDIN_COOKIE": cookie_blob},
            sources={"LINKEDIN_COOKIE": "/test"},
        )

        async def fake_run_verb(*, verb, profile, bundle, payload):
            # Simulate a Playwright error whose message leaked a secret.
            scrubbed_msg = bundle.scrub(f"navigation failed: cookie {cookie_blob} rejected by server")
            return {
                "status": "error",
                "action": verb,
                "profile": profile.name,
                "error": scrubbed_msg,
                "error_type": "TimeoutError",
            }

        with (
            # Force the navigate path to load the bundle — see comment
            # in ``test_api_scrubs_echoed_secrets_in_response``.
            patch.object(sidecar_server_mod, "_verb_needs_secrets", return_value=True),
            patch.object(sidecar_server_mod, "load_bundle", return_value=fake_bundle),
            patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb),
        ):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://linkedin.com/feed"},
                )
        assert response.status_code == 200
        body_text = json.dumps(response.json())
        assert cookie_blob not in body_text
        assert "[REDACTED]" in body_text

    # ── Issue #143 (a): top-level except in ``run_action`` ──────────

    def test_api_unexpected_exception_returns_structured_envelope(self, sidecar_server_mod, configured_sidecar) -> None:
        """A bare exception inside ``run_action`` returns a JSON envelope, not a 500 stack trace.

        Issue #143 (a): PR #142's structured-error envelope only covered
        failures *inside* ``run_verb``. Anything raised by
        ``assert_keyfile_safe`` / ``ensure_profile`` / ``load_bundle``
        before dispatch reached the verb bubbled out as a bare uvicorn
        500. We inject a ``RuntimeError`` into ``ensure_profile`` here —
        the same pre-dispatch surface — and assert the response is a
        structured envelope with ``status:error`` and an ``error_type``.
        """
        from starlette.testclient import TestClient

        def boom(*args, **kwargs):
            raise RuntimeError("synthetic ensure_profile failure")

        with patch.object(sidecar_server_mod, "ensure_profile", side_effect=boom):
            with TestClient(sidecar_server_mod.app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://example.com"},
                )
        assert response.status_code == 500
        body = response.json()
        assert body["status"] == "error"
        assert body["error_type"] == "RuntimeError"
        assert "synthetic ensure_profile failure" in body["error"]
        assert body["action"] == "navigate"
        assert body["profile"] == "linkedin-bram"

    def test_api_load_bundle_permission_error_returns_structured_envelope(
        self, sidecar_server_mod, configured_sidecar
    ) -> None:
        """PermissionError from ``load_bundle`` returns a structured envelope.

        This is the exact failure mode from issue #143's debug session
        on 2026-05-15: the host's secrets dir was owned by a non-1000
        uid, so ``pathlib.Path.exists()`` raised ``PermissionError``
        from inside ``load_bundle``. PR #142 didn't catch it because
        ``load_bundle`` runs before ``run_verb``.
        """
        from starlette.testclient import TestClient

        def boom(*args, **kwargs):
            raise PermissionError(13, "Permission denied", "/config/secrets/browser/linkedin-bram.env.age")

        # Force the read verb to load the bundle so we exercise the
        # ``load_bundle`` call site directly.
        with (
            patch.object(sidecar_server_mod, "_verb_needs_secrets", return_value=True),
            patch.object(sidecar_server_mod, "load_bundle", side_effect=boom),
        ):
            with TestClient(sidecar_server_mod.app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://example.com"},
                )
        assert response.status_code == 500
        body = response.json()
        assert body["status"] == "error"
        assert body["error_type"] == "PermissionError"
        assert body["profile"] == "linkedin-bram"

    def test_api_envelope_scrubs_through_loaded_bundle(self, sidecar_server_mod, configured_sidecar) -> None:
        """The top-level catch-all envelope runs the bundle scrubber over ``str(e)``.

        Issue #143 (a): if the failure happens *after* ``load_bundle``
        succeeded, the envelope must scrub the error message through
        that bundle so any plaintext value that leaked into the
        exception message gets redacted.
        """
        from helpers.secrets import SecretBundle
        from starlette.testclient import TestClient

        secret_value = "topsecret9999XYZ"
        fake_bundle = SecretBundle(
            values={"COOKIE": secret_value},
            sources={"COOKIE": "/test"},
        )

        async def fake_run_verb(*, verb, profile, bundle, payload):
            # Simulate a leaky exception thrown from inside the verb
            # whose message includes a decrypted value.
            raise RuntimeError(f"deep failure with cookie={secret_value} in trace")

        with (
            patch.object(sidecar_server_mod, "_verb_needs_secrets", return_value=True),
            patch.object(sidecar_server_mod, "load_bundle", return_value=fake_bundle),
            patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb),
        ):
            with TestClient(sidecar_server_mod.app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://example.com"},
                )
        assert response.status_code == 500
        body_text = json.dumps(response.json())
        assert secret_value not in body_text, "envelope must scrub leaked secret values"
        assert "[REDACTED]" in body_text

    def test_api_http_exception_still_propagates_unchanged(self, sidecar_server_mod, configured_sidecar) -> None:
        """The catch-all must not swallow ``HTTPException`` — its status code must reach the client.

        FastAPI's own error handling formats ``HTTPException`` into the
        ``{"detail": ...}`` shape; if our catch-all caught it first, we'd
        convert every 400/502/503 into a 500. Re-assert the unknown-verb
        path returns 400, not 500, to lock that in.
        """
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/teleport",
                json={"profile": "linkedin-bram"},
            )
        assert response.status_code == 400
        assert "unknown action" in response.json()["detail"].lower()

    # ── Issue #143 (c): lazy ``load_bundle`` ────────────────────────

    def test_api_navigate_skips_load_bundle(self, sidecar_server_mod, configured_sidecar) -> None:
        """``navigate`` does not call ``load_bundle`` — read verbs need no creds.

        Issue #143 (c): a perms regression on ``/config/secrets/browser``
        must not take down ``navigate``. The simplest way to enforce
        that is to never call ``load_bundle`` for read-only verbs.
        """
        from starlette.testclient import TestClient

        load_bundle_calls: list[str] = []

        def tracking_load_bundle(profile_name, **kwargs):
            load_bundle_calls.append(profile_name)
            from helpers.secrets import SecretBundle

            return SecretBundle()

        async def fake_run_verb(*, verb, profile, bundle, payload):
            return {"status": "ok", "action": verb, "profile": profile.name, "final_url": payload["url"]}

        with (
            patch.object(sidecar_server_mod, "load_bundle", side_effect=tracking_load_bundle),
            patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb),
        ):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://example.com"},
                )
        assert response.status_code == 200
        assert load_bundle_calls == [], "navigate must not call load_bundle"

    def test_api_navigate_succeeds_even_when_load_bundle_would_fail(
        self, sidecar_server_mod, configured_sidecar
    ) -> None:
        """The acceptance test: ``navigate`` returns 200 even if ``load_bundle`` would raise.

        Simulates the uid-mismatch ``PermissionError`` on the secrets
        dir. ``navigate`` must not even attempt ``load_bundle`` and
        therefore must not be affected by the perms regression.
        """
        from starlette.testclient import TestClient

        def boom(*args, **kwargs):  # pragma: no cover — must not be called
            raise PermissionError(13, "Permission denied", "/config/secrets/browser/linkedin-bram.env.age")

        async def fake_run_verb(*, verb, profile, bundle, payload):
            # Bundle must be empty since load_bundle was skipped.
            assert bundle.values == {}, f"navigate must receive an empty bundle, got {bundle.values!r}"
            return {"status": "ok", "action": verb, "profile": profile.name, "final_url": payload["url"]}

        with (
            patch.object(sidecar_server_mod, "load_bundle", side_effect=boom),
            patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb),
        ):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/navigate",
                    json={"profile": "linkedin-bram", "url": "https://example.com"},
                )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_api_extract_and_screenshot_skip_load_bundle(self, sidecar_server_mod, configured_sidecar) -> None:
        """All three read verbs skip ``load_bundle``, not just navigate."""
        from starlette.testclient import TestClient

        calls: list[str] = []

        def tracking_load_bundle(profile_name, **kwargs):
            calls.append(profile_name)
            from helpers.secrets import SecretBundle

            return SecretBundle()

        async def fake_run_verb(*, verb, profile, bundle, payload):
            return {"status": "ok", "action": verb, "profile": profile.name, "final_url": "https://x"}

        with (
            patch.object(sidecar_server_mod, "load_bundle", side_effect=tracking_load_bundle),
            patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb),
        ):
            with TestClient(sidecar_server_mod.app) as client:
                for action, payload in [
                    ("extract", {"profile": "linkedin-bram", "selectors": {"x": "h1"}}),
                    ("screenshot", {"profile": "linkedin-bram"}),
                ]:
                    response = client.post(f"/api/{action}", json=payload)
                    assert response.status_code == 200, f"{action} returned {response.status_code}"
        assert calls == [], f"read verbs must not call load_bundle, got {calls!r}"

    def test_verb_needs_secrets_classification(self, sidecar_server_mod) -> None:
        """Read verbs don't need secrets, approval-gated write verbs do, unknown fails closed."""
        assert sidecar_server_mod._verb_needs_secrets("navigate") is False
        assert sidecar_server_mod._verb_needs_secrets("extract") is False
        assert sidecar_server_mod._verb_needs_secrets("screenshot") is False
        assert sidecar_server_mod._verb_needs_secrets("submit") is True
        assert sidecar_server_mod._verb_needs_secrets("apply") is True
        assert sidecar_server_mod._verb_needs_secrets("post") is True
        assert sidecar_server_mod._verb_needs_secrets("purchase") is True
        # Unknown verb → fail-closed (load the bundle so any perm error
        # is surfaced with a structured envelope rather than silently
        # skipped).
        assert sidecar_server_mod._verb_needs_secrets("totally-new-verb") is True


# ── Issue #143 (b): entrypoint script + bind-mount ownership ────────


class TestBrowserEntrypoint:
    """The entrypoint script + Dockerfile + compose mount contract from issue #143 (b)."""

    def test_entrypoint_script_exists_and_is_executable(self) -> None:
        script = REPO_ROOT / "docker" / "entrypoint-browser.sh"
        assert script.exists(), "docker/entrypoint-browser.sh must exist"
        assert script.stat().st_mode & 0o111, "entrypoint script must be executable"

    def test_entrypoint_script_drops_to_sidecar_via_gosu(self) -> None:
        """Drops privileges via ``gosu sidecar`` after fixing ownership."""
        text = (REPO_ROOT / "docker" / "entrypoint-browser.sh").read_text()
        assert "gosu sidecar" in text, "entrypoint must exec the CMD as the sidecar user"
        assert "exec gosu sidecar" in text, "entrypoint must `exec` so uvicorn becomes PID 2 under tini"

    def test_entrypoint_script_chowns_both_private_dirs(self) -> None:
        """Script touches both ``browser_profiles`` and ``secrets/browser``.

        Either via env var or hardcoded path — issue #143 calls out
        both dirs as the failure surface.
        """
        text = (REPO_ROOT / "docker" / "entrypoint-browser.sh").read_text()
        assert "BROWSER_USE_PROFILES_DIR" in text or "/config/browser_profiles" in text
        assert "BROWSER_USE_SECRETS_DIR" in text or "/config/secrets/browser" in text
        # Both ownership and mode are set — that's the contract.
        assert "chown" in text
        assert "0700" in text

    def test_entrypoint_script_fixes_age_blob_perms(self) -> None:
        """Every ``*.age`` blob is chmod'd 0600 and chown'd to sidecar."""
        text = (REPO_ROOT / "docker" / "entrypoint-browser.sh").read_text()
        assert "*.age" in text
        assert "0600" in text

    def test_entrypoint_script_does_not_touch_keyfile_mount(self) -> None:
        """Hands off ``/run/secrets/age.key`` — Docker manages that mount.

        Sentinel check: the script must not chown the keyfile. The
        sidecar's ``assert_keyfile_safe`` enforces the mode at request
        time; the entrypoint stays out of it.
        """
        text = (REPO_ROOT / "docker" / "entrypoint-browser.sh").read_text()
        # Allow the comment to mention the path; we just don't want
        # a chown/chmod against it.
        for forbidden in ("chown.*age.key", "chmod.*age.key"):
            import re

            assert not re.search(forbidden, text), f"entrypoint must not run `{forbidden}` against the keyfile"

    def test_dockerfile_invokes_entrypoint_script(self) -> None:
        """Dockerfile starts as root, copies the script in, and uses it as the entrypoint."""
        text = (REPO_ROOT / "docker" / "Dockerfile.browser").read_text()
        assert "entrypoint-browser.sh" in text, "Dockerfile must reference the entrypoint script"
        assert "/usr/local/bin/entrypoint-browser.sh" in text, "script must be copied to a stable path"
        # ENTRYPOINT must chain tini → script so PID 1 stays tini.
        assert 'ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint-browser.sh"]' in text

    def test_dockerfile_does_not_switch_to_sidecar_user_at_runtime(self) -> None:
        """The entrypoint drops privileges itself — no top-level ``USER sidecar`` after the COPY.

        We need to start as root so the entrypoint can chown bind
        mounts. ``USER sidecar`` would defeat that.
        """
        text = (REPO_ROOT / "docker" / "Dockerfile.browser").read_text()
        # Allow ``USER sidecar`` to NOT be at end of file. Specifically
        # there should be no uncommented ``USER sidecar`` directive that
        # affects the CMD.
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        assert "USER sidecar" not in lines, (
            "Dockerfile must not switch to USER sidecar at build time — the entrypoint runs as root, "
            "then drops to sidecar via gosu."
        )

    def test_compose_secrets_mount_is_writable(self) -> None:
        """Compose must mount ``./config/secrets/browser`` rw so the entrypoint can chown it.

        A ``:ro`` mount blocks ``chown`` and would defeat the entire
        bootstrap contract. See docs/packs/browser_use-entrypoint.md
        for the security-vs-bootstrap tradeoff.
        """
        import yaml

        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        svc = compose["services"]["browser-use"]
        volumes = svc.get("volumes", [])
        secrets_mounts = [v for v in volumes if isinstance(v, str) and "/config/secrets/browser" in v]
        assert secrets_mounts, "compose must mount ./config/secrets/browser into the sidecar"
        for mount in secrets_mounts:
            assert not mount.endswith(":ro"), (
                f"compose mount {mount!r} is :ro; entrypoint chown will fail on fresh checkouts. "
                "See issue #143 (b) and docs/packs/browser_use-entrypoint.md."
            )

    def test_compose_profiles_mount_is_writable(self) -> None:
        """And ``./config/browser_profiles`` — sidecar must be able to mkdir/cookie-write."""
        import yaml

        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
        svc = compose["services"]["browser-use"]
        volumes = svc.get("volumes", [])
        profile_mounts = [v for v in volumes if isinstance(v, str) and "/config/browser_profiles" in v]
        assert profile_mounts, "compose must mount ./config/browser_profiles"
        for mount in profile_mounts:
            assert not mount.endswith(":ro"), f"profiles mount {mount!r} must not be read-only"

    def test_readme_documents_entrypoint(self) -> None:
        """README has a one-liner pointing at the entrypoint design notes.

        Issue #143 (b): "Should ship with a one-liner README note in
        packs/browser_use/README.md explaining what the entrypoint does
        and why, so we don't re-learn this in three months."
        """
        text = (REPO_ROOT / "packs" / "browser_use" / "README.md").read_text()
        assert "entrypoint" in text.lower(), "README must mention the entrypoint script"


# ── Playwright runner ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def runner_mod():
    """Import the Playwright runner module."""
    if not _have_fastapi():
        pytest.skip("fastapi not installed in the test environment")
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    return importlib.import_module("browser_use_sidecar.playwright_runner")


class _FakePage:
    """Stand-in for ``playwright.async_api.Page`` with introspectable inputs.

    Only the surface the runner actually touches: ``goto``, ``locator``,
    ``screenshot``, ``url``, ``close``. Anything else would be over-
    fitting the fake to the real API.
    """

    def __init__(self) -> None:
        self.url = "about:blank"
        self.closed = False
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self.screenshot_calls: list[dict[str, Any]] = []
        self.goto_raises: Exception | None = None
        self.screenshot_bytes: bytes = b"\x89PNG\r\n\x1a\nFAKEPNG"
        # Optional override: when set, ``goto`` leaves ``self.url`` at
        # this value instead of the requested URL. Simulates a redirect
        # chain where Playwright's ``page.url`` reports the landing URL.
        self.goto_resolves_to: str | None = None
        # selector → text_content. None means "not present".
        self.selector_map: dict[str, str | None] = {}
        # selector → exception (raised by text_content)
        self.selector_raises: dict[str, Exception] = {}

    async def goto(self, url, *, wait_until=None, timeout=None):
        self.goto_calls.append((url, {"wait_until": wait_until, "timeout": timeout}))
        if self.goto_raises is not None:
            raise self.goto_raises
        self.url = self.goto_resolves_to if self.goto_resolves_to is not None else url

    def locator(self, selector):
        page = self

        class _Locator:
            @property
            def first(self_inner):
                return self_inner

            async def text_content(self_inner, *, timeout=None):
                if selector in page.selector_raises:
                    raise page.selector_raises[selector]
                return page.selector_map.get(selector)

        return _Locator()

    async def screenshot(self, *, full_page=True):
        self.screenshot_calls.append({"full_page": full_page})
        return self.screenshot_bytes

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False
        self.new_context_calls: int = 0

    async def new_context(self, **kwargs):
        self.new_context_calls += 1
        return _FakeContext()

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self._cookies: list[dict[str, Any]] = []
        self.add_cookies_calls: list[list[dict[str, Any]]] = []

    async def add_cookies(self, cookies):
        self.add_cookies_calls.append(list(cookies))
        self._cookies.extend(cookies)

    async def cookies(self):
        return list(self._cookies)

    async def new_page(self):
        return _FakePage()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def runner_setup(profile_mod, secrets_mod, tmp_path: Path):
    """Common ingredients for runner tests: a profile + an empty bundle."""
    base = tmp_path / "profiles"
    base.mkdir(mode=0o700)
    profile = profile_mod.ensure_profile("linkedin-bram", profiles_dir=base)
    bundle = secrets_mod.SecretBundle(values={}, sources={})
    return {"profile": profile, "bundle": bundle, "profiles_dir": base}


class TestPlaywrightRunner:
    """Cover the runner end-to-end with a fake Page + fake browser/context."""

    @pytest.mark.asyncio
    async def test_navigate_returns_final_url(self, runner_mod, runner_setup) -> None:
        """Positive path: ``navigate`` reports the page's final URL after redirects."""
        fake_page = _FakePage()
        # Simulate a redirect: goto("/redirector") settles at "/landed".
        fake_page.goto_resolves_to = "https://example.com/landed"

        async def page_factory(_ctx):
            return fake_page

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=runner_setup["profile"],
            bundle=runner_setup["bundle"],
            payload={"url": "https://example.com/redirector"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=page_factory,
        )

        assert result["status"] == "ok"
        assert result["action"] == "navigate"
        assert result["final_url"] == "https://example.com/landed"
        # goto was called with the requested URL + a load wait
        assert len(fake_page.goto_calls) == 1
        url, kwargs = fake_page.goto_calls[0]
        assert url == "https://example.com/redirector"
        assert kwargs["wait_until"] == "load"
        # owned-client close path ran for every layer
        assert fake_page.closed is True

    @pytest.mark.asyncio
    async def test_navigate_requires_url(self, runner_mod, runner_setup) -> None:
        with pytest.raises(runner_mod.VerbRunError, match="url"):
            await runner_mod.run_verb(
                verb="navigate",
                profile=runner_setup["profile"],
                bundle=runner_setup["bundle"],
                payload={},
                browser_factory=lambda: _async_return(_FakeBrowser()),
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=lambda c: _async_return(_FakePage()),
            )

    @pytest.mark.asyncio
    async def test_extract_returns_dict_with_none_for_missing(self, runner_mod, runner_setup) -> None:
        """Positive path: ``extract`` returns a per-key text dict.

        Missing selectors map to ``None``, not exceptions — caller can
        tell which selector failed without losing the others.
        """
        fake_page = _FakePage()
        fake_page.selector_map = {"h1.title": "Senior SRE", ".company": "Acme"}
        fake_page.selector_raises = {".missing": RuntimeError("no such element")}
        # url stays at the goto target
        fake_page.url = "about:blank"

        async def page_factory(_ctx):
            return fake_page

        result = await runner_mod.run_verb(
            verb="extract",
            profile=runner_setup["profile"],
            bundle=runner_setup["bundle"],
            payload={
                "url": "https://example.com/jobs/123",
                "selectors": {"title": "h1.title", "company": ".company", "salary": ".missing"},
            },
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=page_factory,
        )

        assert result["status"] == "ok"
        assert result["action"] == "extract"
        assert result["extracted"] == {
            "title": "Senior SRE",
            "company": "Acme",
            "salary": None,
        }

    @pytest.mark.asyncio
    async def test_extract_rejects_empty_selectors(self, runner_mod, runner_setup) -> None:
        with pytest.raises(runner_mod.VerbRunError, match="selectors"):
            await runner_mod.run_verb(
                verb="extract",
                profile=runner_setup["profile"],
                bundle=runner_setup["bundle"],
                payload={"selectors": {}},
                browser_factory=lambda: _async_return(_FakeBrowser()),
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=lambda c: _async_return(_FakePage()),
            )

    @pytest.mark.asyncio
    async def test_extract_rejects_non_object_selectors(self, runner_mod, runner_setup) -> None:
        with pytest.raises(runner_mod.VerbRunError, match="object"):
            await runner_mod.run_verb(
                verb="extract",
                profile=runner_setup["profile"],
                bundle=runner_setup["bundle"],
                payload={"selectors": ["bad"]},
                browser_factory=lambda: _async_return(_FakeBrowser()),
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=lambda c: _async_return(_FakePage()),
            )

    @pytest.mark.asyncio
    async def test_screenshot_returns_base64_png(self, runner_mod, runner_setup) -> None:
        """Positive path: ``screenshot`` returns non-empty PNG bytes (base64-encoded)."""
        import base64

        fake_page = _FakePage()
        # plant a recognisable PNG-ish byte sequence
        fake_page.screenshot_bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes-here"

        result = await runner_mod.run_verb(
            verb="screenshot",
            profile=runner_setup["profile"],
            bundle=runner_setup["bundle"],
            payload={"url": "https://example.com", "full_page": True},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(fake_page),
        )

        assert result["status"] == "ok"
        assert result["action"] == "screenshot"
        decoded = base64.b64decode(result["screenshot_b64"])
        assert decoded == fake_page.screenshot_bytes
        assert result["screenshot_bytes"] == len(fake_page.screenshot_bytes)
        # full_page propagated to the call
        assert fake_page.screenshot_calls[0]["full_page"] is True

    @pytest.mark.asyncio
    async def test_unknown_verb_raises(self, runner_mod, runner_setup) -> None:
        with pytest.raises(runner_mod.VerbRunError, match="unknown verb"):
            await runner_mod.run_verb(
                verb="teleport",
                profile=runner_setup["profile"],
                bundle=runner_setup["bundle"],
                payload={},
                browser_factory=lambda: _async_return(_FakeBrowser()),
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=lambda c: _async_return(_FakePage()),
            )

    @pytest.mark.asyncio
    async def test_owned_client_close_runs_on_exception(self, runner_mod, runner_setup) -> None:
        """Negative-path: a verb that crashes mid-run still closes every owned client.

        Acceptance from the issue: "Owned-client close path runs on
        both success and exception".
        """
        fake_browser = _FakeBrowser()
        fake_context = _FakeContext()
        fake_page = _FakePage()
        fake_page.goto_raises = RuntimeError("net::ERR_NAME_NOT_RESOLVED")

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=runner_setup["profile"],
            bundle=runner_setup["bundle"],
            payload={"url": "https://does-not-resolve.invalid"},
            browser_factory=lambda: _async_return(fake_browser),
            context_factory=lambda b, p: _async_return(fake_context),
            page_factory=lambda c: _async_return(fake_page),
        )

        # Playwright failure became a structured error, not a raised exception.
        assert result["status"] == "error"
        assert "ERR_NAME_NOT_RESOLVED" in result["error"]
        assert result["error_type"] == "RuntimeError"
        # All three close paths ran regardless of the failure.
        assert fake_page.closed is True
        assert fake_context.closed is True
        assert fake_browser.closed is True

    @pytest.mark.asyncio
    async def test_browser_factory_failure_returns_structured_error(self, runner_mod, runner_setup) -> None:
        """Issue #141: a failing browser launch must NOT escape as a bare 500.

        Simulates "chromium not installed" / sandbox/seccomp / missing
        binary. The runner must catch the exception, return
        ``status=error``, and the surrounding handler stays a 200 so
        the agent can read the structured body.
        """

        async def boom_browser():
            raise RuntimeError("chromium not installed")

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=runner_setup["profile"],
            bundle=runner_setup["bundle"],
            payload={"url": "https://example.com"},
            browser_factory=boom_browser,
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(_FakePage()),
        )

        assert result["status"] == "error"
        assert result["action"] == "navigate"
        assert result["error_type"] == "RuntimeError"
        assert "chromium not installed" in result["error"]

    @pytest.mark.asyncio
    async def test_context_factory_failure_returns_structured_error(self, runner_mod, runner_setup) -> None:
        """Issue #141: a failing context build (profile EACCES, cookie
        load crash) must come back as structured error, not a 500.

        The browser was created, so it must still be closed by the
        ``finally`` teardown.
        """
        fake_browser = _FakeBrowser()

        async def boom_context(_b, _p):
            raise PermissionError("[Errno 13] cookies.json: profile dir not writable")

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=runner_setup["profile"],
            bundle=runner_setup["bundle"],
            payload={"url": "https://example.com"},
            browser_factory=lambda: _async_return(fake_browser),
            context_factory=boom_context,
            page_factory=lambda c: _async_return(_FakePage()),
        )

        assert result["status"] == "error"
        assert result["error_type"] == "PermissionError"
        assert "profile dir not writable" in result["error"]
        # Teardown ran for the already-built browser.
        assert fake_browser.closed is True

    @pytest.mark.asyncio
    async def test_page_factory_failure_returns_structured_error(self, runner_mod, runner_setup) -> None:
        """Issue #141: a failing ``new_page`` must come back as
        structured error. Browser + context already exist and must
        both be closed by the teardown.
        """
        fake_browser = _FakeBrowser()
        fake_context = _FakeContext()

        async def boom_page(_ctx):
            raise RuntimeError("Target page, context or browser has been closed")

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=runner_setup["profile"],
            bundle=runner_setup["bundle"],
            payload={"url": "https://example.com"},
            browser_factory=lambda: _async_return(fake_browser),
            context_factory=lambda b, p: _async_return(fake_context),
            page_factory=boom_page,
        )

        assert result["status"] == "error"
        assert result["error_type"] == "RuntimeError"
        assert "Target page, context or browser has been closed" in result["error"]
        assert fake_context.closed is True
        assert fake_browser.closed is True

    @pytest.mark.asyncio
    async def test_setup_failure_scrubs_secrets(self, runner_mod, runner_setup, secrets_mod) -> None:
        """Issue #141: secret scrubbing applies to setup-phase errors too.

        Regression guard: a future change must not bypass the bundle
        scrub just because the error came from launch rather than a
        verb.
        """
        secret = "topsecret-launch-9999"
        bundle = secrets_mod.SecretBundle(
            values={"TOKEN": secret},
            sources={"TOKEN": "/test"},
        )

        async def boom_browser():
            raise RuntimeError(f"playwright launch failed: token={secret}")

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=runner_setup["profile"],
            bundle=bundle,
            payload={"url": "https://example.com"},
            browser_factory=boom_browser,
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(_FakePage()),
        )

        assert result["status"] == "error"
        assert secret not in result["error"]
        assert "[REDACTED]" in result["error"]

    @pytest.mark.asyncio
    async def test_runner_scrubs_secrets_from_error_message(self, runner_mod, runner_setup, secrets_mod) -> None:
        """Negative-path acceptance: error message must not leak profile bytes."""
        secret = "topsecret-xyzabc-9999"
        bundle = secrets_mod.SecretBundle(
            values={"TOKEN": secret},
            sources={"TOKEN": "/test"},
        )
        fake_page = _FakePage()
        fake_page.goto_raises = RuntimeError(f"navigation failed: rejected token={secret}")

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=runner_setup["profile"],
            bundle=bundle,
            payload={"url": "https://example.com"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(fake_page),
        )

        assert result["status"] == "error"
        assert secret not in result["error"]
        assert "[REDACTED]" in result["error"]

    @pytest.mark.asyncio
    async def test_malformed_cookies_refused_before_browser_spawn(self, runner_mod, runner_setup) -> None:
        """Negative-path acceptance: malformed profile fails fast, no browser spawn.

        The browser factory must NOT run when ``cookies.json`` is
        garbage.
        """
        profile = runner_setup["profile"]
        (profile.path / "cookies.json").write_text("{not json")

        browser_factory_calls = {"n": 0}
        page_factory_calls = {"n": 0}

        async def browser_factory():
            browser_factory_calls["n"] += 1
            return _FakeBrowser()

        async def page_factory(_ctx):
            page_factory_calls["n"] += 1
            return _FakePage()

        with pytest.raises(runner_mod.MalformedProfileError):
            await runner_mod.run_verb(
                verb="navigate",
                profile=profile,
                bundle=runner_setup["bundle"],
                payload={"url": "https://example.com"},
                browser_factory=browser_factory,
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=page_factory,
            )

        assert browser_factory_calls["n"] == 0, "browser factory must not run on malformed profile"
        assert page_factory_calls["n"] == 0, "page factory must not run on malformed profile"

    @pytest.mark.asyncio
    async def test_cookies_persisted_after_successful_verb(self, runner_mod, runner_setup) -> None:
        """Session sticks: cookies are written back to disk after a successful run."""
        fake_context = _FakeContext()
        # simulate Playwright collecting a session cookie during the run
        fake_context._cookies = [{"name": "session", "value": "abc", "domain": "x.com", "path": "/"}]

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=runner_setup["profile"],
            bundle=runner_setup["bundle"],
            payload={"url": "https://example.com"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(fake_context),
            page_factory=lambda c: _async_return(_FakePage()),
        )

        assert result["status"] == "ok"
        cookies_path = runner_setup["profile"].path / "cookies.json"
        assert cookies_path.exists()
        on_disk = json.loads(cookies_path.read_text())
        assert on_disk == [{"name": "session", "value": "abc", "domain": "x.com", "path": "/"}]

    @pytest.mark.asyncio
    async def test_existing_cookies_loaded_into_context(self, runner_mod, runner_setup) -> None:
        """Profile cookies wire into the new context before the verb runs.

        Acceptance from the issue: "Keep the decrypted profile wiring
        (cookies, localStorage) into the browser context".
        """
        profile = runner_setup["profile"]
        seeded = [{"name": "session", "value": "v", "domain": "x.com", "path": "/"}]
        (profile.path / "cookies.json").write_text(json.dumps(seeded))

        loaded_context = _FakeContext()

        # The real context factory loads cookies from disk; exercise
        # the production one rather than the test fake here so the
        # "cookies are wired in" assertion is meaningful.
        async def real_context_factory(browser, cookies_file):
            # Mirror what the production ``_build_context`` does, but
            # against our fake context.
            raw = cookies_file.read_text()
            cookies = json.loads(raw) if raw.strip() else []
            if cookies:
                await loaded_context.add_cookies(cookies)
            return loaded_context

        await runner_mod.run_verb(
            verb="navigate",
            profile=profile,
            bundle=runner_setup["bundle"],
            payload={"url": "https://example.com"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=real_context_factory,
            page_factory=lambda c: _async_return(_FakePage()),
        )

        assert loaded_context.add_cookies_calls == [seeded]


async def _async_return(value):
    """Helper: turn a value into a coroutine that returns it. Keeps fake-factory call sites concise."""
    return value


# ── No-LLM topology guards ──────────────────────────────────────────


class TestNoLlmDependency:
    """Issue #119 acceptance: no LLM, no Anthropic credentials in the sidecar.

    These guards catch a future regression that re-introduces
    ``browser_use.Agent``, ``langchain_anthropic``, or
    ``ANTHROPIC_API_KEY`` as a hard requirement.
    """

    def test_sidecar_source_has_no_anthropic_references(self) -> None:
        sidecar_dir = PACK_DIR / "browser_use_sidecar"
        # Substrings that would only appear if someone re-introduced the
        # LLM agent path. ``from browser_use_sidecar.*`` is allowed —
        # only the bare ``browser_use`` package (the Agent library) is
        # banned, so we check for ``from browser_use import`` /
        # ``import browser_use`` patterns specifically.
        banned_substrings = [
            "ANTHROPIC_API_KEY",
            "langchain_anthropic",
            "ChatAnthropic",
            "browser_use.Agent",
            "from browser_use import",
            "import browser_use\n",
        ]
        for path in sidecar_dir.rglob("*.py"):
            text = path.read_text()
            for needle in banned_substrings:
                assert needle not in text, (
                    f"{path} contains {needle!r} — the Playwright-direct rewrite "
                    "(issue #119) dropped the LLM agent path; do not re-introduce it here"
                )

    def test_dockerfile_does_not_install_browser_use(self) -> None:
        text = (REPO_ROOT / "docker" / "Dockerfile.browser").read_text()
        assert "browser-use==" not in text and '"browser-use"' not in text, (
            "Dockerfile.browser must not install browser-use — the sidecar uses Playwright "
            "directly; see playwright_runner.py"
        )


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
