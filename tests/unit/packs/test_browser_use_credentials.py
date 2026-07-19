"""Unit tests for the issue #147 credential / login surface.

Covers (in order):

- ``helpers/credentials.py`` — encrypt / decrypt round-trip,
  shape validation, leak guards.
- ``helpers/profile_meta.py`` — ``meta.json`` round-trip + the
  belt-and-braces strip of credential fields from cached login configs.
- Sidecar runner — ``login`` / ``session_status`` verbs, MFA
  detection, opt-in gating, auto-retry on 401, storage_state
  persistence.
- Sidecar HTTP layer — ``/api/add_credential`` /
  ``/api/revoke_credential`` and the no-leak / no-log policy.
- Router-side Slack grant flow — DM-only check, two-prompt cycle,
  zero-leak in say messages.

The real ``age`` binary is used wherever it gives us a meaningful
guarantee (encrypt-then-decrypt round-trip, "the bytes on disk are
not the plaintext"). HTTP / Playwright are mocked.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "browser_use"

if str(PACK_DIR) not in sys.path:
    sys.path.insert(0, str(PACK_DIR))


def _load_browser_use_handler():
    """Load the browser_use pack handler under a unique sys.modules key (issue #617)."""
    mod_name = "packhandler_browser_use"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, PACK_DIR / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Real age keyfile fixture ────────────────────────────────────────


def _have_age() -> bool:
    return shutil.which("age") is not None and shutil.which("age-keygen") is not None


@pytest.fixture
def real_keyfile(tmp_path: Path) -> Path:
    """Generate an actual age keyfile so encrypt/decrypt round-trips work.

    Skips the test when neither ``age`` nor ``age-keygen`` is on PATH —
    the same CI environment the sidecar image bundles them into.
    """
    if not _have_age():
        pytest.skip("age / age-keygen not installed in the test environment")
    keyfile = tmp_path / "age.key"
    proc = subprocess.run(
        ["age-keygen", "-o", str(keyfile)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    os.chmod(keyfile, 0o400)
    return keyfile


@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    """Tight-mode profile dir that ``credentials.age`` will live inside."""
    p = tmp_path / "profiles" / "pathtohired-bram"
    p.mkdir(parents=True, mode=0o700)
    os.chmod(p, 0o700)
    return p


@pytest.fixture(scope="module")
def credentials_mod():
    return importlib.import_module("helpers.credentials")


@pytest.fixture(scope="module")
def profile_meta_mod():
    return importlib.import_module("helpers.profile_meta")


# ── helpers/credentials.py ──────────────────────────────────────────


class TestCredentialsRoundTrip:
    def test_extract_public_key_returns_age1_recipient(self, credentials_mod, real_keyfile) -> None:
        pubkey = credentials_mod.extract_public_key(real_keyfile)
        assert pubkey.startswith("age1")
        # Same recipient as the keyfile's own header line.
        text = real_keyfile.read_text()
        for line in text.splitlines():
            if line.startswith("# public key:"):
                assert line.split(":", 1)[1].strip() == pubkey
                break

    def test_extract_public_key_rejects_keyfile_with_no_header(self, credentials_mod, tmp_path) -> None:
        bad = tmp_path / "no-header.key"
        bad.write_text("AGE-SECRET-KEY-NOT-VALID\n")
        os.chmod(bad, 0o400)
        with pytest.raises(credentials_mod.CredentialError, match="public key"):
            credentials_mod.extract_public_key(bad)

    def test_set_then_load_round_trip(self, credentials_mod, real_keyfile, profile_dir) -> None:
        credentials_mod.set_credential(
            profile_dir, "pathtohired", "bram@example.com", "hunter2-very-long", keyfile=real_keyfile
        )
        creds = credentials_mod.load_credentials(profile_dir, keyfile=real_keyfile)
        assert set(creds.keys()) == {"pathtohired"}
        assert creds["pathtohired"].username == "bram@example.com"
        assert creds["pathtohired"].password == "hunter2-very-long"

    def test_credentials_file_perms_are_0600(self, credentials_mod, real_keyfile, profile_dir) -> None:
        credentials_mod.set_credential(profile_dir, "pathtohired", "u", "p-something-long", keyfile=real_keyfile)
        blob = profile_dir / "credentials.age"
        mode = blob.stat().st_mode & 0o777
        assert mode == 0o600, f"credentials.age mode drifted to {mode:#o}"

    def test_disk_blob_is_not_plaintext(self, credentials_mod, real_keyfile, profile_dir) -> None:
        """Acceptance: the bytes on disk must not contain the plaintext password.

        Catches a future regression where a 'fast path' writes the raw
        JSON without encrypting it, or where a temp-file is left
        behind unencrypted.
        """
        password = "topsecret-9999-distinctive"
        credentials_mod.set_credential(profile_dir, "pathtohired", "u", password, keyfile=real_keyfile)
        blob = profile_dir / "credentials.age"
        on_disk = blob.read_bytes()
        assert password.encode() not in on_disk
        # And no leftover .tmp file.
        assert not (profile_dir / "credentials.age.tmp").exists()

    def test_set_credential_preserves_other_keys(self, credentials_mod, real_keyfile, profile_dir) -> None:
        credentials_mod.set_credential(profile_dir, "pathtohired", "u1", "p1-distinct-long", keyfile=real_keyfile)
        credentials_mod.set_credential(profile_dir, "linkedin", "u2", "p2-different-long", keyfile=real_keyfile)
        creds = credentials_mod.load_credentials(profile_dir, keyfile=real_keyfile)
        assert set(creds.keys()) == {"pathtohired", "linkedin"}
        assert creds["pathtohired"].password == "p1-distinct-long"
        assert creds["linkedin"].password == "p2-different-long"

    def test_remove_credential_leaves_others_intact(self, credentials_mod, real_keyfile, profile_dir) -> None:
        """Acceptance: ``revoke`` removes the named key but leaves
        other credentials in the file intact."""
        credentials_mod.set_credential(profile_dir, "a", "u-a", "p-a-long-12345", keyfile=real_keyfile)
        credentials_mod.set_credential(profile_dir, "b", "u-b", "p-b-long-67890", keyfile=real_keyfile)
        removed = credentials_mod.remove_credential(profile_dir, "a", keyfile=real_keyfile)
        assert removed is True
        creds = credentials_mod.load_credentials(profile_dir, keyfile=real_keyfile)
        assert set(creds.keys()) == {"b"}
        assert creds["b"].username == "u-b"

    def test_remove_last_credential_deletes_blob(self, credentials_mod, real_keyfile, profile_dir) -> None:
        credentials_mod.set_credential(profile_dir, "only", "u", "p-something-long", keyfile=real_keyfile)
        credentials_mod.remove_credential(profile_dir, "only", keyfile=real_keyfile)
        assert not (profile_dir / "credentials.age").exists()

    def test_remove_missing_key_returns_false(self, credentials_mod, real_keyfile, profile_dir) -> None:
        credentials_mod.set_credential(profile_dir, "a", "u", "p-something-long", keyfile=real_keyfile)
        assert credentials_mod.remove_credential(profile_dir, "ghost", keyfile=real_keyfile) is False
        # 'a' is still there
        assert "a" in credentials_mod.load_credentials(profile_dir, keyfile=real_keyfile)

    def test_remove_when_no_blob_returns_false(self, credentials_mod, real_keyfile, profile_dir) -> None:
        assert credentials_mod.remove_credential(profile_dir, "ghost", keyfile=real_keyfile) is False

    def test_load_when_no_blob_returns_empty_dict(self, credentials_mod, real_keyfile, profile_dir) -> None:
        assert credentials_mod.load_credentials(profile_dir, keyfile=real_keyfile) == {}

    def test_get_credential_raises_for_missing_key(self, credentials_mod, real_keyfile, profile_dir) -> None:
        credentials_mod.set_credential(profile_dir, "a", "u", "p-something-long", keyfile=real_keyfile)
        with pytest.raises(credentials_mod.CredentialError, match="not found"):
            credentials_mod.get_credential(profile_dir, "ghost", keyfile=real_keyfile)

    def test_credential_repr_does_not_leak(self, credentials_mod) -> None:
        """Defensive: ``repr(Credential(...))`` must mask the password.

        A stray ``logger.info("got %r", cred)`` would otherwise log
        plaintext verbatim — repr is the lowest-friction leak surface.
        """
        c = credentials_mod.Credential(username="bram@example.com", password="topsecret-9999")
        text = repr(c)
        assert "topsecret-9999" not in text
        assert "bram@example.com" not in text
        assert "redacted" in text.lower()

    def test_set_credential_validates_inputs(self, credentials_mod, real_keyfile, profile_dir) -> None:
        for bad in (None, "", 123):
            with pytest.raises(credentials_mod.CredentialError):
                credentials_mod.set_credential(profile_dir, "k", bad, "p-long", keyfile=real_keyfile)
            with pytest.raises(credentials_mod.CredentialError):
                credentials_mod.set_credential(profile_dir, "k", "u", bad, keyfile=real_keyfile)
            with pytest.raises(credentials_mod.CredentialError):
                credentials_mod.set_credential(profile_dir, bad, "u", "p-long", keyfile=real_keyfile)


# ── helpers/profile_meta.py ─────────────────────────────────────────


class TestProfileMeta:
    def test_load_meta_when_missing_returns_empty(self, profile_meta_mod, profile_dir) -> None:
        assert profile_meta_mod.load_meta(profile_dir) == {}

    def test_save_then_load_round_trip(self, profile_meta_mod, profile_dir) -> None:
        profile_meta_mod.save_meta(profile_dir, {"login_enabled": True, "x": 1})
        assert profile_meta_mod.load_meta(profile_dir) == {"login_enabled": True, "x": 1}

    def test_meta_perms_are_0600(self, profile_meta_mod, profile_dir) -> None:
        profile_meta_mod.save_meta(profile_dir, {"login_enabled": True})
        mode = (profile_dir / "meta.json").stat().st_mode & 0o777
        assert mode == 0o600

    def test_login_enabled_default_false(self, profile_meta_mod, profile_dir) -> None:
        assert profile_meta_mod.login_enabled(profile_dir) is False

    def test_login_enabled_reflects_meta(self, profile_meta_mod, profile_dir) -> None:
        profile_meta_mod.save_meta(profile_dir, {"login_enabled": True})
        assert profile_meta_mod.login_enabled(profile_dir) is True

    def test_set_login_config_strips_credential_fields(self, profile_meta_mod, profile_dir) -> None:
        """Belt-and-braces: even if a caller passes username/password by
        mistake, ``set_login_config`` must not persist them.

        The acceptance criterion is that meta.json stores no
        credential material — only the lookup ``credential_key``.
        """
        profile_meta_mod.set_login_config(
            profile_dir,
            {
                "url": "https://x/login",
                "selectors": {"username": "input"},
                "credential_key": "pathtohired",
                "username": "bram@example.com",  # accidental
                "password": "topsecret-9999",  # accidental
            },
        )
        cfg = profile_meta_mod.get_login_config(profile_dir)
        assert cfg is not None
        assert "username" not in cfg
        assert "password" not in cfg
        assert cfg["credential_key"] == "pathtohired"

    def test_get_login_config_when_unset_returns_none(self, profile_meta_mod, profile_dir) -> None:
        profile_meta_mod.save_meta(profile_dir, {"login_enabled": True})
        assert profile_meta_mod.get_login_config(profile_dir) is None

    def test_malformed_meta_json_is_ignored_not_raised(self, profile_meta_mod, profile_dir) -> None:
        (profile_dir / "meta.json").write_text("{not json")
        assert profile_meta_mod.load_meta(profile_dir) == {}
        # And login_enabled defaults False — never crashes the verb path.
        assert profile_meta_mod.login_enabled(profile_dir) is False


# ── Playwright runner: login / session_status / auto-retry ──────────


def _have_fastapi() -> bool:
    try:
        import fastapi  # noqa: F401
        import starlette  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.fixture(scope="module")
def runner_mod():
    if not _have_fastapi():
        pytest.skip("fastapi not installed in the test environment")
    return importlib.import_module("browser_use_sidecar.playwright_runner")


@pytest.fixture(scope="module")
def profile_mod():
    return importlib.import_module("helpers.profile_manager")


@pytest.fixture(scope="module")
def secrets_mod():
    return importlib.import_module("helpers.secrets")


class _FakeLocator:
    """Stand-in for a Playwright locator with a controllable success/MFA story."""

    def __init__(self, page: "_FakeLoginPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def fill(self, value, *, timeout=None):
        self._page.fills.append((self._selector, value))
        if self._selector in self._page.fill_raises:
            raise self._page.fill_raises[self._selector]

    async def click(self, *, timeout=None):
        self._page.clicks.append(self._selector)
        if self._selector in self._page.click_raises:
            raise self._page.click_raises[self._selector]

    async def wait_for(self, *, timeout=None):
        self._page.wait_for_calls.append(self._selector)
        if self._selector in self._page.wait_raises:
            raise self._page.wait_raises[self._selector]

    async def is_visible(self, *, timeout=None):
        return self._selector in self._page.visible_selectors


class _FakeLoginPage:
    """Page fake that records every login interaction.

    The fill_raises / wait_raises / visible_selectors knobs let one
    test assert success while another asserts an MFA detection or a
    success-selector timeout. Plus a goto-callable seam so we can
    simulate auto-retry loops.
    """

    def __init__(self) -> None:
        self.url = "about:blank"
        self.closed = False
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.wait_for_calls: list[str] = []
        self.fill_raises: dict[str, Exception] = {}
        self.click_raises: dict[str, Exception] = {}
        self.wait_raises: dict[str, Exception] = {}
        self.visible_selectors: set[str] = set()
        # ``goto_responses`` is a list of (url -> {url, status}) dicts
        # yielded in order. Each call consumes one entry; if the list
        # runs out we stick on the last one.
        self.goto_responses: list[dict[str, Any]] = []
        self.goto_calls: list[str] = []
        self.screenshot_calls: list[dict[str, Any]] = []

    async def goto(self, url, *, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        next_response = self.goto_responses[len(self.goto_calls) - 1] if self.goto_responses else None
        if next_response is not None:
            self.url = next_response.get("url", url)
            return _FakeResponse(status=next_response.get("status", 200))
        self.url = url
        return _FakeResponse(status=200)

    def locator(self, selector: str):
        return _FakeLocator(self, selector)

    async def screenshot(self, *, full_page=True):
        self.screenshot_calls.append({"full_page": full_page})
        return b"\x89PNG\r\nfake"

    async def close(self) -> None:
        self.closed = True


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self._cookies: list[dict[str, Any]] = []
        self.add_cookies_calls: list[list[dict[str, Any]]] = []
        self.storage_state_value: dict[str, Any] = {"cookies": [], "origins": []}

    async def add_cookies(self, cookies):
        self.add_cookies_calls.append(list(cookies))
        self._cookies.extend(cookies)

    async def cookies(self):
        return list(self._cookies)

    async def storage_state(self):
        return self.storage_state_value

    async def new_page(self):
        return _FakeLoginPage()

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def new_context(self, **kwargs):
        return _FakeContext()

    async def close(self) -> None:
        self.closed = True


async def _async_return(value):
    return value


@pytest.fixture
def login_setup(profile_mod, secrets_mod, profile_meta_mod, real_keyfile, tmp_path):
    """Profile + opted-in meta + a stored credential, ready for login()."""
    base = tmp_path / "profiles"
    base.mkdir(mode=0o700)
    profile = profile_mod.ensure_profile("pathtohired-bram", profiles_dir=base)
    profile_meta_mod.save_meta(profile.path, {"login_enabled": True})

    creds_mod = importlib.import_module("helpers.credentials")
    creds_mod.set_credential(
        profile.path,
        "pathtohired",
        "bram@example.com",
        "topsecret-9999",
        keyfile=real_keyfile,
    )

    bundle = secrets_mod.SecretBundle(values={}, sources={})
    return {
        "profile": profile,
        "bundle": bundle,
        "profiles_dir": base,
        "keyfile": real_keyfile,
    }


def _login_payload(**overrides):
    payload = {
        "url": "https://pathtohired.com/login",
        "selectors": {
            "username": "input[name=email]",
            "password": "input[name=password]",
            "submit": "button[type=submit]",
            "success": "[data-testid=user-menu]",
        },
        "credential_key": "pathtohired",
        "timeout_ms": 5000,
    }
    payload.update(overrides)
    return payload


class TestLoginVerb:
    @pytest.mark.asyncio
    async def test_login_happy_path(self, runner_mod, login_setup, monkeypatch) -> None:
        """Positive path: login fills both fields, clicks submit, waits for success."""
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        result = await runner_mod.run_verb(
            verb="login",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload=_login_payload(),
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        assert result["status"] == "ok"
        assert result["action"] == "login"
        assert result["profile"] == "pathtohired-bram"
        assert result["session_persisted"] is True

        # The fields were filled in the right order with the decrypted
        # username + password.
        username_fills = [v for sel, v in page.fills if sel == "input[name=email]"]
        password_fills = [v for sel, v in page.fills if sel == "input[name=password]"]
        assert username_fills == ["bram@example.com"]
        assert password_fills == ["topsecret-9999"]
        assert "button[type=submit]" in page.clicks

    @pytest.mark.asyncio
    async def test_login_persists_storage_state_to_profile(
        self, runner_mod, login_setup, monkeypatch, profile_meta_mod
    ) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        ctx = _FakeContext()
        ctx.storage_state_value = {
            "cookies": [{"name": "sid", "value": "abc", "domain": "x", "path": "/"}],
            "origins": [],
        }

        await runner_mod.run_verb(
            verb="login",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload=_login_payload(),
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(ctx),
            page_factory=lambda c: _async_return(page),
        )

        # storage_state.json holds the full session blob; cookies.json
        # holds the cookies for backwards compatibility.
        storage = login_setup["profile"].path / "storage_state.json"
        assert storage.exists()
        on_disk = json.loads(storage.read_text())
        assert on_disk["cookies"][0]["name"] == "sid"

        cookies_file = login_setup["profile"].path / "cookies.json"
        assert cookies_file.exists()
        cookies = json.loads(cookies_file.read_text())
        assert cookies[0]["name"] == "sid"

        # And the login config was cached for auto-retry, with NO
        # credential material persisted.
        cfg = profile_meta_mod.get_login_config(login_setup["profile"].path)
        assert cfg is not None
        assert cfg["url"] == "https://pathtohired.com/login"
        assert cfg["credential_key"] == "pathtohired"
        assert "password" not in cfg
        assert "username" not in cfg
        # The cached config stays out of the storage state too.
        assert "topsecret-9999" not in storage.read_text()

    @pytest.mark.asyncio
    async def test_login_refused_when_not_opted_in(
        self, runner_mod, login_setup, monkeypatch, profile_meta_mod
    ) -> None:
        """Profile without ``login_enabled: true`` → LoginNotEnabledError."""
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        # Disable opt-in by overwriting meta.
        profile_meta_mod.save_meta(login_setup["profile"].path, {"login_enabled": False})
        with pytest.raises(runner_mod.LoginNotEnabledError):
            await runner_mod.run_verb(
                verb="login",
                profile=login_setup["profile"],
                bundle=login_setup["bundle"],
                payload=_login_payload(),
                browser_factory=lambda: _async_return(_FakeBrowser()),
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=lambda c: _async_return(_FakeLoginPage()),
            )

    @pytest.mark.asyncio
    async def test_login_missing_credential_key_is_verb_run_error(self, runner_mod, login_setup, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        with pytest.raises(runner_mod.VerbRunError, match="credential_key"):
            await runner_mod.run_verb(
                verb="login",
                profile=login_setup["profile"],
                bundle=login_setup["bundle"],
                payload=_login_payload(credential_key="ghost"),
                browser_factory=lambda: _async_return(_FakeBrowser()),
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=lambda c: _async_return(_FakeLoginPage()),
            )

    @pytest.mark.asyncio
    async def test_login_required_selectors_validated(self, runner_mod, login_setup, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        for missing in ("username", "password", "submit", "success"):
            payload = _login_payload()
            payload["selectors"].pop(missing)
            with pytest.raises(runner_mod.VerbRunError, match=missing):
                await runner_mod.run_verb(
                    verb="login",
                    profile=login_setup["profile"],
                    bundle=login_setup["bundle"],
                    payload=payload,
                    browser_factory=lambda: _async_return(_FakeBrowser()),
                    context_factory=lambda b, p: _async_return(_FakeContext()),
                    page_factory=lambda c: _async_return(_FakeLoginPage()),
                )

    @pytest.mark.asyncio
    async def test_login_bad_password_returns_structured_error_no_creds(
        self, runner_mod, login_setup, monkeypatch
    ) -> None:
        """Acceptance: bad password → structured error containing zero credential material.

        We simulate "wrong creds" the same way Playwright does — the
        success selector never appears within timeout, so wait_for
        raises. The runner must:

        - Not echo the password in the error message.
        - Not take a screenshot (locked decision #5).
        - Surface a clean ``status: error`` envelope.
        """
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        # Success selector never resolves.
        page.wait_raises["[data-testid=user-menu]"] = TimeoutError("Timeout 5000ms exceeded")

        result = await runner_mod.run_verb(
            verb="login",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload=_login_payload(),
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        assert result["status"] == "error"
        assert result["action"] == "login"
        body = json.dumps(result)
        # Acceptance: zero credential material in the response.
        assert "topsecret-9999" not in body
        assert "bram@example.com" not in body
        # And: locked decision #5 — no screenshot of any kind.
        assert "screenshot_b64" not in result
        assert page.screenshot_calls == []

    @pytest.mark.asyncio
    async def test_login_stale_username_selector_no_screenshot(self, runner_mod, login_setup, monkeypatch) -> None:
        """Acceptance: stale selectors produce a structured error.

        When the site's HTML changes and the username selector no
        longer matches, ``locator.fill`` raises pre-submit (before any
        creds are typed). Locked decision #5 reverses the issue body's
        original "screenshot of pre-fill state" — we skip screenshots
        on login failure entirely. This test pins that down: zero
        screenshot calls regardless of *which* selector goes stale.
        """
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        # Username selector throws on fill (selector no longer matches).
        page.fill_raises["input[name=email]"] = TimeoutError("Timeout 5000ms exceeded waiting for input[name=email]")
        result = await runner_mod.run_verb(
            verb="login",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload=_login_payload(),
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        assert result["status"] == "error"
        assert result["action"] == "login"
        # Zero credential material reached the response.
        body = json.dumps(result)
        assert "topsecret-9999" not in body
        assert "bram@example.com" not in body
        # And no screenshot was attempted, regardless of when in the
        # flow the failure landed.
        assert page.screenshot_calls == []
        # The error_type reflects the underlying Playwright failure.
        assert result["error_type"] == "TimeoutError"

    @pytest.mark.asyncio
    async def test_login_mfa_detected_after_submit_returns_mfa_required(
        self, runner_mod, login_setup, monkeypatch
    ) -> None:
        """Acceptance: MFA challenge produces ``error_type: mfa_required``."""
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        # Success selector never resolves. MFA selector resolves
        # immediately after submit (asyncio race below picks it).
        page.wait_raises["[data-testid=user-menu]"] = TimeoutError("never visible")
        page.wait_for_calls = []

        # Simulate "MFA selector wins the race" by making it return
        # immediately while success raises after a short await.
        original_wait = _FakeLocator.wait_for

        async def patched_wait(self, *, timeout=None):
            self._page.wait_for_calls.append(self._selector)
            if self._selector == "input[name=otp]":
                return  # resolves immediately
            raise TimeoutError("timeout")

        with patch.object(_FakeLocator, "wait_for", patched_wait):
            try:
                result = await runner_mod.run_verb(
                    verb="login",
                    profile=login_setup["profile"],
                    bundle=login_setup["bundle"],
                    payload=_login_payload(
                        selectors={
                            "username": "input[name=email]",
                            "password": "input[name=password]",
                            "submit": "button[type=submit]",
                            "success": "[data-testid=user-menu]",
                            "mfa": "input[name=otp]",
                        },
                    ),
                    browser_factory=lambda: _async_return(_FakeBrowser()),
                    context_factory=lambda b, p: _async_return(_FakeContext()),
                    page_factory=lambda c: _async_return(page),
                )
            finally:
                _FakeLocator.wait_for = original_wait

        assert result["status"] == "error"
        assert result["error_type"] == "mfa_required"
        body = json.dumps(result)
        assert "topsecret-9999" not in body

    @pytest.mark.asyncio
    async def test_login_does_not_log_credentials(self, runner_mod, login_setup, monkeypatch, caplog) -> None:
        """Negative-test acceptance: nothing logged during login contains the password.

        Guards against a future ``logger.debug("filling password=%s", …)``
        regression. Captures every log record at DEBUG and greps the
        formatted message for the plaintext.
        """
        import logging

        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        with caplog.at_level(logging.DEBUG):
            await runner_mod.run_verb(
                verb="login",
                profile=login_setup["profile"],
                bundle=login_setup["bundle"],
                payload=_login_payload(),
                browser_factory=lambda: _async_return(_FakeBrowser()),
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=lambda c: _async_return(page),
            )
        for record in caplog.records:
            text = record.getMessage()
            assert "topsecret-9999" not in text, f"password leaked in log: {text!r}"
            assert "bram@example.com" not in text, f"username leaked in log: {text!r}"


class TestSessionStatusVerb:
    @pytest.mark.asyncio
    async def test_session_status_authed(self, runner_mod, login_setup, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        page.goto_responses = [{"url": "https://pathtohired.com/api/me", "status": 200}]
        result = await runner_mod.run_verb(
            verb="session_status",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload={"probe_url": "https://pathtohired.com/api/me"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        # The verb's own ``status`` field overwrites the envelope's
        # default — that's intentional, so the agent reads one field.
        assert result["status"] == "authed"
        assert result["action"] == "session_status"

    @pytest.mark.asyncio
    async def test_session_status_unauthed_via_401(self, runner_mod, login_setup, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        page.goto_responses = [{"url": "https://pathtohired.com/api/me", "status": 401}]
        result = await runner_mod.run_verb(
            verb="session_status",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload={"probe_url": "https://pathtohired.com/api/me"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        # Envelope's "status" gets overwritten by the verb's own
        # "status" (the runner does **verb_result), so the field
        # reflects the auth state directly.
        assert result["status"] == "unauthed"

    @pytest.mark.asyncio
    async def test_session_status_unauthed_via_login_redirect(self, runner_mod, login_setup, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        page.goto_responses = [{"url": "https://pathtohired.com/login?return=/api/me", "status": 200}]
        result = await runner_mod.run_verb(
            verb="session_status",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload={
                "probe_url": "https://pathtohired.com/api/me",
                "login_url": "https://pathtohired.com/login",
            },
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        assert result["status"] == "unauthed"

    @pytest.mark.asyncio
    async def test_session_status_unknown_on_network_failure(self, runner_mod, login_setup, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()

        async def boom(url, **kw):
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

        page.goto = boom
        result = await runner_mod.run_verb(
            verb="session_status",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload={"probe_url": "https://no.such.host"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        # Envelope's "status" overwritten by verb's "unknown".
        assert result["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_session_status_refused_when_not_opted_in(
        self, runner_mod, login_setup, monkeypatch, profile_meta_mod
    ) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        profile_meta_mod.save_meta(login_setup["profile"].path, {"login_enabled": False})
        with pytest.raises(runner_mod.LoginNotEnabledError):
            await runner_mod.run_verb(
                verb="session_status",
                profile=login_setup["profile"],
                bundle=login_setup["bundle"],
                payload={"probe_url": "https://x"},
                browser_factory=lambda: _async_return(_FakeBrowser()),
                context_factory=lambda b, p: _async_return(_FakeContext()),
                page_factory=lambda c: _async_return(_FakeLoginPage()),
            )


class TestAutoRetryOn401:
    @pytest.mark.asyncio
    async def test_navigate_retries_login_on_401(self, runner_mod, login_setup, monkeypatch, profile_meta_mod) -> None:
        """Locked decision #4: any verb that hits a 401 silently re-runs
        ``login`` once with the cached credentials and retries the verb."""
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        # Cache a login config so the auto-retry path activates.
        profile_meta_mod.set_login_config(
            login_setup["profile"].path,
            {
                "url": "https://pathtohired.com/login",
                "selectors": {
                    "username": "input[name=email]",
                    "password": "input[name=password]",
                    "submit": "button[type=submit]",
                    "success": "[data-testid=user-menu]",
                },
                "credential_key": "pathtohired",
                "timeout_ms": 5000,
            },
        )
        page = _FakeLoginPage()
        # First navigate → 401; auto-retry path runs login (which ALSO
        # calls goto, then expects success-selector wait_for to succeed
        # — default fake doesn't raise so it succeeds); then second
        # navigate → 200.
        page.goto_responses = [
            {"url": "https://pathtohired.com/dashboard", "status": 401},
            {"url": "https://pathtohired.com/login", "status": 200},  # login goto
            {"url": "https://pathtohired.com/dashboard", "status": 200},  # retry
        ]

        result = await runner_mod.run_verb(
            verb="navigate",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload={"url": "https://pathtohired.com/dashboard"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        assert result["status"] == "ok"
        assert result["final_url"] == "https://pathtohired.com/dashboard"
        # Three goto calls: original + login + retry. (The login goto
        # is the middle one.)
        assert len(page.goto_calls) == 3
        # The password was filled during the auto-retry login.
        assert any(v == "topsecret-9999" for sel, v in page.fills if sel == "input[name=password]")

    @pytest.mark.asyncio
    async def test_navigate_no_retry_when_no_login_config_cached(self, runner_mod, login_setup, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(login_setup["keyfile"]))
        page = _FakeLoginPage()
        page.goto_responses = [{"url": "https://x/dashboard", "status": 401}]
        result = await runner_mod.run_verb(
            verb="navigate",
            profile=login_setup["profile"],
            bundle=login_setup["bundle"],
            payload={"url": "https://x/dashboard"},
            browser_factory=lambda: _async_return(_FakeBrowser()),
            context_factory=lambda b, p: _async_return(_FakeContext()),
            page_factory=lambda c: _async_return(page),
        )
        assert result["status"] == "ok"
        # Only one goto call — no retry attempted.
        assert len(page.goto_calls) == 1


# ── Sidecar HTTP layer ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def sidecar_server_mod():
    if not _have_fastapi():
        pytest.skip("fastapi not installed")
    return importlib.import_module("browser_use_sidecar.server")


@pytest.fixture
def configured_sidecar(sidecar_server_mod, real_keyfile, tmp_path, monkeypatch):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(mode=0o700)
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(mode=0o700)
    monkeypatch.setenv("BROWSER_USE_AGE_KEYFILE", str(real_keyfile))
    monkeypatch.setenv("BROWSER_USE_PROFILES_DIR", str(profiles_dir))
    monkeypatch.setenv("BROWSER_USE_SECRETS_DIR", str(bundles_dir))
    return {"keyfile": real_keyfile, "profiles_dir": profiles_dir}


class TestAddCredentialEndpoint:
    def test_happy_path_writes_credentials_age(self, sidecar_server_mod, configured_sidecar, credentials_mod) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/add_credential",
                json={
                    "profile": "pathtohired-bram",
                    "credential_key": "pathtohired",
                    "username": "bram@example.com",
                    "password": "topsecret-9999",
                },
            )
        assert response.status_code == 200, response.text
        body = response.json()
        # Confirmation includes only the credential_key — never the
        # username or password (locked decision #3).
        assert body == {
            "status": "ok",
            "action": "add_credential",
            "profile": "pathtohired-bram",
            "credential_key": "pathtohired",
        }
        assert "topsecret-9999" not in response.text
        assert "bram@example.com" not in response.text

        # And the on-disk blob is real, decrypts back to the cred.
        profile_path = configured_sidecar["profiles_dir"] / "pathtohired-bram"
        creds = credentials_mod.load_credentials(profile_path, keyfile=configured_sidecar["keyfile"])
        assert creds["pathtohired"].password == "topsecret-9999"

    def test_missing_field_returns_400(self, sidecar_server_mod, configured_sidecar) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/add_credential",
                json={"profile": "p", "credential_key": "k", "username": "u"},  # no password
            )
        assert response.status_code == 400
        assert "password" in response.json()["detail"]

    def test_no_log_of_payload_fields(self, sidecar_server_mod, configured_sidecar, caplog) -> None:
        """Acceptance: router-side logs scrubbed for password.

        The sidecar-side equivalent: zero log records emitted during
        ``/api/add_credential`` may contain the plaintext password.
        """
        import logging

        from starlette.testclient import TestClient

        password = "absurdly-distinctive-passw0rd-XXXXX"
        with caplog.at_level(logging.DEBUG):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/add_credential",
                    json={
                        "profile": "pathtohired-bram",
                        "credential_key": "pathtohired",
                        "username": "bram@example.com",
                        "password": password,
                    },
                )
        assert response.status_code == 200
        for record in caplog.records:
            text = record.getMessage()
            assert password not in text, f"password leaked in log: {text!r}"

    def test_invalid_profile_name_returns_400(self, sidecar_server_mod, configured_sidecar) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/add_credential",
                json={
                    "profile": "Bad/Name",
                    "credential_key": "k",
                    "username": "u",
                    "password": "p-long-string",
                },
            )
        assert response.status_code == 400


class TestRevokeCredentialEndpoint:
    def test_happy_path_removes_only_named_key(self, sidecar_server_mod, configured_sidecar, credentials_mod) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            client.post(
                "/api/add_credential",
                json={
                    "profile": "pathtohired-bram",
                    "credential_key": "pathtohired",
                    "username": "u1",
                    "password": "p1-long-distinct",
                },
            )
            client.post(
                "/api/add_credential",
                json={
                    "profile": "pathtohired-bram",
                    "credential_key": "linkedin",
                    "username": "u2",
                    "password": "p2-long-distinct",
                },
            )
            response = client.post(
                "/api/revoke_credential",
                json={"profile": "pathtohired-bram", "credential_key": "pathtohired"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["removed"] is True
        # Other key intact.
        profile_path = configured_sidecar["profiles_dir"] / "pathtohired-bram"
        creds = credentials_mod.load_credentials(profile_path, keyfile=configured_sidecar["keyfile"])
        assert set(creds.keys()) == {"linkedin"}

    def test_revoke_missing_profile_returns_400(self, sidecar_server_mod, configured_sidecar) -> None:
        from starlette.testclient import TestClient

        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/revoke_credential",
                json={"profile": "ghost-profile", "credential_key": "k"},
            )
        # ``ensure_profile(create_missing=False)`` raises → 400.
        assert response.status_code == 400


# ── Sidecar API: login / session_status routes ─────────────────────


class TestLoginRoute:
    def test_login_endpoint_invokes_runner(
        self, sidecar_server_mod, configured_sidecar, credentials_mod, profile_meta_mod
    ) -> None:
        from helpers.profile_manager import ensure_profile
        from starlette.testclient import TestClient

        # Set up the profile + credential + opt-in.
        profile = ensure_profile(
            "pathtohired-bram",
            profiles_dir=configured_sidecar["profiles_dir"],
            create_missing=True,
        )
        profile_meta_mod.save_meta(profile.path, {"login_enabled": True})
        credentials_mod.set_credential(
            profile.path,
            "pathtohired",
            "bram@example.com",
            "topsecret-9999",
            keyfile=configured_sidecar["keyfile"],
        )

        captured: dict[str, Any] = {}

        async def fake_run_verb(*, verb, profile, bundle, payload):
            captured["verb"] = verb
            captured["payload"] = payload
            return {
                "status": "ok",
                "action": verb,
                "profile": profile.name,
                "session_persisted": True,
            }

        with patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/login",
                    json={
                        "profile": "pathtohired-bram",
                        "credential_key": "pathtohired",
                        "url": "https://pathtohired.com/login",
                        "selectors": {
                            "username": "input[name=email]",
                            "password": "input[name=password]",
                            "submit": "button[type=submit]",
                            "success": "[data-testid=user-menu]",
                        },
                    },
                )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["action"] == "login"
        assert body["session_persisted"] is True
        assert captured["verb"] == "login"
        # The route did NOT echo the credential_key in any error path
        # (success body carries it intentionally — the agent posted it
        # in the first place).

    def test_login_endpoint_refuses_profile_without_opt_in(self, sidecar_server_mod, configured_sidecar) -> None:
        from helpers.profile_manager import ensure_profile
        from starlette.testclient import TestClient

        # Profile exists but no meta.json → opt-in is False.
        ensure_profile(
            "no-opt-in",
            profiles_dir=configured_sidecar["profiles_dir"],
            create_missing=True,
        )
        with TestClient(sidecar_server_mod.app) as client:
            response = client.post(
                "/api/login",
                json={
                    "profile": "no-opt-in",
                    "credential_key": "k",
                    "url": "https://x",
                    "selectors": {
                        "username": "u",
                        "password": "p",
                        "submit": "s",
                        "success": "ok",
                    },
                },
            )
        assert response.status_code == 400
        assert "login not enabled" in response.json()["detail"].lower()

    def test_session_status_route_round_trip(self, sidecar_server_mod, configured_sidecar, profile_meta_mod) -> None:
        from helpers.profile_manager import ensure_profile
        from starlette.testclient import TestClient

        profile = ensure_profile(
            "pathtohired-bram",
            profiles_dir=configured_sidecar["profiles_dir"],
            create_missing=True,
        )
        profile_meta_mod.save_meta(profile.path, {"login_enabled": True})

        async def fake_run_verb(*, verb, profile, bundle, payload):
            return {"status": "authed", "action": verb, "profile": profile.name, "final_url": "x"}

        with patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb):
            with TestClient(sidecar_server_mod.app) as client:
                response = client.post(
                    "/api/session_status",
                    json={
                        "profile": "pathtohired-bram",
                        "probe_url": "https://pathtohired.com/api/me",
                    },
                )
        assert response.status_code == 200
        # Verb classification surfaces directly in "status".
        assert response.json()["status"] == "authed"


# ── Handler: login / session_status known actions ──────────────────


class TestHandlerKnowsLoginVerbs:
    def test_login_is_a_known_action(self) -> None:
        handler_mod = _load_browser_use_handler()
        assert "login" in handler_mod._KNOWN_READ_ACTIONS

    def test_session_status_is_a_known_action(self) -> None:
        handler_mod = _load_browser_use_handler()
        assert "session_status" in handler_mod._KNOWN_READ_ACTIONS

    def test_login_is_not_approval_gated(self) -> None:
        """Locked decision #1: high-level ``login`` keeps creds in the
        sidecar. It is NOT approval-gated — no ``draft-approval`` block."""
        handler_mod = _load_browser_use_handler()
        approve = handler_mod._load_approve_list()
        assert "login" not in approve
        assert "session_status" not in approve


# ── Router-side Slack grant flow ───────────────────────────────────


@pytest.fixture(scope="module")
def browser_credentials_mod():
    return importlib.import_module("router.packs.browser_credentials")


class _CapturingPrompt:
    """An InputPrompt-shaped fake that returns canned replies."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[str] = []
        self.channel = "DXXXX"
        self.thread_ts = "1.0"

    async def __call__(self, text: str) -> None:
        self.calls.append(text)

    async def prompt(self, text: str, *, timeout: float = 600) -> str:
        self.calls.append(text)
        return self._replies.pop(0)


class TestParseCredentialCommand:
    def test_grant_credentials_basic(self, browser_credentials_mod) -> None:
        cmd = browser_credentials_mod.parse_credential_command("grant pathtohired-bram credentials pathtohired")
        assert isinstance(cmd, browser_credentials_mod.CredentialGrantCommand)
        assert cmd.profile == "pathtohired-bram"
        assert cmd.credential_key == "pathtohired"

    def test_revoke_credentials_basic(self, browser_credentials_mod) -> None:
        cmd = browser_credentials_mod.parse_credential_command("revoke pathtohired-bram credentials pathtohired")
        assert isinstance(cmd, browser_credentials_mod.CredentialRevokeCommand)
        assert cmd.profile == "pathtohired-bram"
        assert cmd.credential_key == "pathtohired"

    def test_case_insensitive_and_extra_whitespace(self, browser_credentials_mod) -> None:
        cmd = browser_credentials_mod.parse_credential_command(
            "  Grant   pathtohired-bram   Credentials   pathtohired   "
        )
        assert isinstance(cmd, browser_credentials_mod.CredentialGrantCommand)
        assert cmd.profile == "pathtohired-bram"

    def test_non_matching_returns_none(self, browser_credentials_mod) -> None:
        for text in (
            "",
            "grant sam github",  # generic pack-grant — not ours
            "grant",
            "grant foo",
            "grant foo credentials",  # missing key
            "credentials foo",
        ):
            assert browser_credentials_mod.parse_credential_command(text) is None


class TestHandleCredentialGrant:
    @pytest.mark.asyncio
    async def test_dm_only_check_blocks_public_channel(self, browser_credentials_mod) -> None:
        prompt = _CapturingPrompt(replies=[])
        await browser_credentials_mod.handle_credential_grant(
            browser_credentials_mod.CredentialGrantCommand(profile="p", credential_key="k"),
            prompt,
            channel="C123PUBLIC",  # ``C`` = public/group
        )
        # Single error message, no prompts, no sidecar call.
        assert len(prompt.calls) == 1
        assert "DM" in prompt.calls[0]
        assert "only works in a DM" in prompt.calls[0]

    @pytest.mark.asyncio
    async def test_happy_path_posts_to_sidecar(self, browser_credentials_mod) -> None:
        prompt = _CapturingPrompt(replies=["bram@example.com", "topsecret-9999"])

        captured: dict = {}

        async def fake_post(path, payload, *, sidecar_url, timeout=30.0):
            captured["path"] = path
            captured["payload"] = payload
            return {"status": "ok", "credential_key": payload["credential_key"]}

        with patch.object(browser_credentials_mod, "_sidecar_post", side_effect=fake_post):
            await browser_credentials_mod.handle_credential_grant(
                browser_credentials_mod.CredentialGrantCommand(
                    profile="pathtohired-bram", credential_key="pathtohired"
                ),
                prompt,
                channel="DXYZ123",
                sidecar_url="http://test:8080",
            )

        assert captured["path"] == "/api/add_credential"
        assert captured["payload"]["profile"] == "pathtohired-bram"
        assert captured["payload"]["credential_key"] == "pathtohired"
        assert captured["payload"]["username"] == "bram@example.com"
        assert captured["payload"]["password"] == "topsecret-9999"

        # Confirmation message contains the credential_key but NOT the
        # username or password.
        all_messages = "\n".join(prompt.calls)
        assert "pathtohired" in all_messages
        assert "topsecret-9999" not in all_messages
        assert "bram@example.com" not in all_messages
        # And it reminds the user to delete the messages.
        assert "Delete" in all_messages or "delete" in all_messages

    @pytest.mark.asyncio
    async def test_empty_username_aborts_without_sidecar_call(self, browser_credentials_mod) -> None:
        prompt = _CapturingPrompt(replies=["   ", "ignored"])
        called: dict = {}

        async def fake_post(*a, **kw):
            called["yes"] = True

        with patch.object(browser_credentials_mod, "_sidecar_post", side_effect=fake_post):
            await browser_credentials_mod.handle_credential_grant(
                browser_credentials_mod.CredentialGrantCommand(profile="p", credential_key="k"),
                prompt,
                channel="DXYZ",
            )
        assert "yes" not in called
        assert any("Empty username" in m for m in prompt.calls)

    @pytest.mark.asyncio
    async def test_empty_password_aborts_without_sidecar_call(self, browser_credentials_mod) -> None:
        prompt = _CapturingPrompt(replies=["bram@example.com", "   "])
        called: dict = {}

        async def fake_post(*a, **kw):
            called["yes"] = True

        with patch.object(browser_credentials_mod, "_sidecar_post", side_effect=fake_post):
            await browser_credentials_mod.handle_credential_grant(
                browser_credentials_mod.CredentialGrantCommand(profile="p", credential_key="k"),
                prompt,
                channel="DXYZ",
            )
        assert "yes" not in called
        assert any("Empty password" in m for m in prompt.calls)

    @pytest.mark.asyncio
    async def test_sidecar_failure_message_does_not_leak_creds(self, browser_credentials_mod) -> None:
        prompt = _CapturingPrompt(replies=["bram@example.com", "topsecret-9999"])

        async def fake_post(*a, **kw):
            raise RuntimeError("sidecar returned 503: keyfile unsafe")

        with patch.object(browser_credentials_mod, "_sidecar_post", side_effect=fake_post):
            await browser_credentials_mod.handle_credential_grant(
                browser_credentials_mod.CredentialGrantCommand(profile="p", credential_key="k"),
                prompt,
                channel="DXYZ",
            )
        all_messages = "\n".join(prompt.calls)
        assert "topsecret-9999" not in all_messages
        assert "bram@example.com" not in all_messages
        assert "failed" in all_messages.lower()

    @pytest.mark.asyncio
    async def test_no_log_of_password_anywhere(self, browser_credentials_mod, caplog) -> None:
        """Acceptance: router-side log scrub — grep for the test
        password in every log record must return zero hits."""
        import logging

        prompt = _CapturingPrompt(replies=["bram@example.com", "topsecret-9999-distinctive"])

        async def fake_post(*a, **kw):
            return {"status": "ok", "credential_key": "pathtohired"}

        with caplog.at_level(logging.DEBUG):
            with patch.object(browser_credentials_mod, "_sidecar_post", side_effect=fake_post):
                await browser_credentials_mod.handle_credential_grant(
                    browser_credentials_mod.CredentialGrantCommand(profile="p", credential_key="pathtohired"),
                    prompt,
                    channel="DXYZ",
                )
        for record in caplog.records:
            text = record.getMessage()
            assert "topsecret-9999-distinctive" not in text


class TestHandleCredentialRevoke:
    @pytest.mark.asyncio
    async def test_dm_only_check_blocks_public_channel(self, browser_credentials_mod) -> None:
        prompt = _CapturingPrompt(replies=[])
        await browser_credentials_mod.handle_credential_revoke(
            browser_credentials_mod.CredentialRevokeCommand(profile="p", credential_key="k"),
            prompt,
            channel="C123PUBLIC",
        )
        assert any("DM" in m for m in prompt.calls)

    @pytest.mark.asyncio
    async def test_happy_path_calls_sidecar(self, browser_credentials_mod) -> None:
        prompt = _CapturingPrompt(replies=[])

        async def fake_post(path, payload, *, sidecar_url, timeout=30.0):
            assert path == "/api/revoke_credential"
            assert payload == {"profile": "p", "credential_key": "k"}
            return {"status": "ok", "removed": True}

        with patch.object(browser_credentials_mod, "_sidecar_post", side_effect=fake_post):
            await browser_credentials_mod.handle_credential_revoke(
                browser_credentials_mod.CredentialRevokeCommand(profile="p", credential_key="k"),
                prompt,
                channel="DXYZ",
            )
        assert any("removed" in m.lower() for m in prompt.calls)


# ── grants.maybe_handle_pack_command routing ────────────────────────


# ── End-to-end smoke: grant → login → extract ───────────────────────


class TestSmokeFlowEndToEnd:
    """Cover the full grant→login→extract handshake against the sidecar HTTP API.

    Substitutes for the operator-driven smoke test against pathtohired.com
    (locked decision #1) — exercises the same code paths with a fake
    Playwright runner so it can run in CI on every pack-touching PR.
    """

    @pytest.mark.asyncio
    async def test_grant_then_login_then_navigate(
        self,
        sidecar_server_mod,
        configured_sidecar,
        credentials_mod,
        profile_meta_mod,
    ) -> None:
        from helpers.profile_manager import ensure_profile
        from starlette.testclient import TestClient

        password = "smoke-flow-password-XXXXX"

        # Step 1: pre-create the profile + flip on the opt-in. (In
        # production the sidecar mkdirs on demand and the operator
        # writes meta.json; we do both up front.)
        profile = ensure_profile(
            "smoke-login",
            profiles_dir=configured_sidecar["profiles_dir"],
            create_missing=True,
        )
        profile_meta_mod.save_meta(profile.path, {"login_enabled": True})

        # Step 2: store the credential via the sidecar HTTP API (the
        # path the router's grant flow takes).
        with TestClient(sidecar_server_mod.app) as client:
            grant = client.post(
                "/api/add_credential",
                json={
                    "profile": "smoke-login",
                    "credential_key": "smoke",
                    "username": "u",
                    "password": password,
                },
            )
        assert grant.status_code == 200, grant.text
        # The sidecar's response carries only the credential_key.
        assert password not in grant.text

        # Step 3: ``login`` against a faked Playwright runner and
        # confirm it picks up the credential we just stored. We let
        # the real login/runner code run (it calls into our fake page).
        captured: dict = {}

        async def fake_run_verb(*, verb, profile, bundle, payload):
            # Mirror the runner's "decrypt and use" path so we can
            # assert the password actually round-tripped through the
            # encrypted blob — without running Chromium.
            from helpers.credentials import get_credential

            captured["verb"] = verb
            cred = get_credential(profile.path, payload["credential_key"])
            captured["password_seen_in_runner"] = cred.password
            return {
                "status": "ok",
                "action": verb,
                "profile": profile.name,
                "session_persisted": True,
            }

        with patch.object(sidecar_server_mod, "run_verb", side_effect=fake_run_verb):
            with TestClient(sidecar_server_mod.app) as client:
                login = client.post(
                    "/api/login",
                    json={
                        "profile": "smoke-login",
                        "credential_key": "smoke",
                        "url": "https://example.com/login",
                        "selectors": {
                            "username": "input",
                            "password": "input",
                            "submit": "button",
                            "success": "ok",
                        },
                    },
                )
        assert login.status_code == 200
        assert login.json()["status"] == "ok"
        assert captured["password_seen_in_runner"] == password
        # And the login response did NOT echo the password back.
        assert password not in login.text

        # Step 4: revoke removes the credential cleanly.
        with TestClient(sidecar_server_mod.app) as client:
            revoke = client.post(
                "/api/revoke_credential",
                json={"profile": "smoke-login", "credential_key": "smoke"},
            )
        assert revoke.status_code == 200
        assert revoke.json()["removed"] is True
        # And the on-disk blob is gone (last credential).
        assert not (profile.path / "credentials.age").exists()


class TestGrantsRouting:
    @pytest.mark.asyncio
    async def test_grant_credentials_routes_to_browser_credentials_handler(self, browser_credentials_mod) -> None:
        """The pre-existing ``grant <agent> <pack>`` regex must NOT
        eat ``grant <profile> credentials <key>``. The new credentials
        check runs first in ``maybe_handle_pack_command``."""
        from router.packs.grants import maybe_handle_pack_command

        say = AsyncMock()

        called: dict = {}

        async def fake_handle(cmd, prompt, *, channel, sidecar_url=None):
            called["got_grant"] = (cmd.profile, cmd.credential_key)

        with patch.object(browser_credentials_mod, "handle_credential_grant", side_effect=fake_handle):
            handled = await maybe_handle_pack_command(
                "grant pathtohired-bram credentials pathtohired",
                say,
                channel="DXYZ",
            )
        assert handled is True
        assert called["got_grant"] == ("pathtohired-bram", "pathtohired")
