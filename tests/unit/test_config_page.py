"""Unit tests for router/config_page.py — the /config page + REST API (#576)."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from router import config_page
from router import settings as settings_mod
from router.packs.secret_store import SecretStore
from router.settings import REGISTRY, RuntimeSettings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in REGISTRY:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    """Point the settings singleton at tmp files; restore afterwards."""
    rs = RuntimeSettings(
        path=tmp_path / "runtime.json",
        ttl=0.0,
        secret_store=SecretStore(path=tmp_path / "secrets.json"),
    )
    settings_mod.reset_settings_for_tests(rs)
    yield rs
    settings_mod.reset_settings_for_tests(None)


@pytest.fixture(autouse=True)
def _isolated_secrets_dir(tmp_path, monkeypatch):
    """Token files go to a tmp dir instead of the repo's config/secrets."""
    tok_dir = tmp_path / "token-files"
    monkeypatch.setattr(config_page, "secrets_dir", lambda: tok_dir)
    yield tok_dir


@pytest.fixture(autouse=True)
def _no_channel_validator():
    config_page.set_channel_validator(None)
    yield
    config_page.set_channel_validator(None)


@pytest.fixture(autouse=True)
def _stub_agent_map(monkeypatch):
    """Deterministic agent map for the boot-env section."""
    monkeypatch.setattr("router.config.get_agent_map", lambda: {"lisa": {}})


def _build_app() -> web.Application:
    app = web.Application()
    config_page.register_routes(app)
    return app


async def _client():
    return TestClient(TestServer(_build_app()))


class TestGetSettings:
    @pytest.mark.asyncio
    async def test_lists_registry_with_values_and_sources(self, _isolated_settings, monkeypatch):
        monkeypatch.setenv("SESSION_TIMEOUT", "600")
        _isolated_settings.set("AUTO_DISPATCH_CHANNEL", "C123ABC456")
        async with await _client() as client:
            resp = await client.get("/config/api/settings")
            assert resp.status == 200
            body = await resp.json()

        by_key = {s["key"]: s for s in body["settings"]}
        assert by_key["SESSION_TIMEOUT"]["value"] == 600
        assert by_key["SESSION_TIMEOUT"]["source"] == "env"
        assert by_key["AUTO_DISPATCH_CHANNEL"]["value"] == "C123ABC456"
        assert by_key["AUTO_DISPATCH_CHANNEL"]["source"] == "runtime"
        assert by_key["ATTACHMENTS_ENABLED"]["source"] == "default"
        assert body["runtime_path"] == str(_isolated_settings.path)

    @pytest.mark.asyncio
    async def test_secrets_are_masked_never_raw(self, _isolated_settings):
        _isolated_settings.set("WORKERS_BOT_TOKEN", "xoxb-super-secret-value-1234")
        async with await _client() as client:
            resp = await client.get("/config/api/settings")
            raw = await resp.text()
        assert "xoxb-super-secret-value-1234" not in raw
        by_key = {s["key"]: s for s in json.loads(raw)["settings"]}
        assert by_key["WORKERS_BOT_TOKEN"]["value"] == "••••1234"
        assert by_key["WORKERS_BOT_TOKEN"]["set"] is True

    @pytest.mark.asyncio
    async def test_boot_entries_read_only_and_masked(self, monkeypatch):
        monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "internal-token-abcd1234")
        monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-lisa-bot-token-9876")
        async with await _client() as client:
            resp = await client.get("/config/api/settings")
            raw = await resp.text()
        assert "internal-token-abcd1234" not in raw
        assert "xoxb-lisa-bot-token-9876" not in raw
        by_key = {s["key"]: s for s in json.loads(raw)["settings"]}
        assert by_key["ROUTER_INTERNAL_TOKEN"]["editable"] is False
        assert by_key["ROUTER_INTERNAL_TOKEN"]["value"].startswith("••••")
        # Per-agent credentials moved to the Agents section (/config/api/agents)
        # — they no longer clutter the global settings list.
        assert "LISA_BOT_TOKEN" not in by_key


class TestPutSetting:
    @pytest.mark.asyncio
    async def test_var_roundtrip_hot(self, _isolated_settings):
        async with await _client() as client:
            resp = await client.put("/config/api/settings/SESSION_TIMEOUT", json={"value": 900})
            body = await resp.json()
        assert resp.status == 200
        assert body["applied"] == "hot"
        assert _isolated_settings.get("SESSION_TIMEOUT") == 900

    @pytest.mark.asyncio
    async def test_restart_class_flagged(self, _isolated_settings):
        async with await _client() as client:
            resp = await client.put("/config/api/settings/SLASH_COMMAND_PREFIX", json={"value": "dev-"})
            body = await resp.json()
        assert body["applied"] == "restart-required"

    @pytest.mark.asyncio
    async def test_invalid_value_422_with_message(self, _isolated_settings):
        async with await _client() as client:
            resp = await client.put("/config/api/settings/SESSION_TIMEOUT", json={"value": "soon"})
            body = await resp.json()
        assert resp.status == 422
        assert "integer" in body["error"]
        assert _isolated_settings.get("SESSION_TIMEOUT") == 600  # unchanged

    @pytest.mark.asyncio
    async def test_channel_format_rejected(self):
        async with await _client() as client:
            resp = await client.put("/config/api/settings/AUTO_DISPATCH_CHANNEL", json={"value": "general"})
        assert resp.status == 422

    @pytest.mark.asyncio
    async def test_unknown_key_404(self):
        async with await _client() as client:
            resp = await client.put("/config/api/settings/NOPE", json={"value": 1})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_boot_key_not_writable(self):
        async with await _client() as client:
            resp = await client.put("/config/api/settings/ROUTER_INTERNAL_TOKEN", json={"value": "x"})
            body = await resp.json()
        assert resp.status == 422
        assert "boot environment" in body["error"]

    @pytest.mark.asyncio
    async def test_missing_value_field_400(self):
        async with await _client() as client:
            resp = await client.put("/config/api/settings/SESSION_TIMEOUT", json={"val": 900})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_secret_saved_to_secret_store_and_masked_in_response(self, _isolated_settings):
        async with await _client() as client:
            resp = await client.put("/config/api/settings/WORKERS_BOT_TOKEN", json={"value": "xoxb-brand-new-9999"})
            body = await resp.json()
        assert body["ok"] is True
        assert "xoxb-brand-new-9999" not in json.dumps(body)
        assert body["value"] == "••••9999"
        assert _isolated_settings._secret_store.get_str("workers_bot_token") == "xoxb-brand-new-9999"


class TestChannelValidation:
    @pytest.mark.asyncio
    async def test_live_validation_rejects_bad_channel(self):
        async def validator(channel_id: str) -> str | None:
            return "channel_not_found"

        config_page.set_channel_validator(validator)
        async with await _client() as client:
            resp = await client.put("/config/api/settings/AUTO_DISPATCH_CHANNEL", json={"value": "C0AGZC613NKK"})
            body = await resp.json()
        assert resp.status == 422
        assert body["validation"] is True
        assert "channel_not_found" in body["error"]

    @pytest.mark.asyncio
    async def test_force_skips_live_validation(self, _isolated_settings):
        async def validator(channel_id: str) -> str | None:
            return "channel_not_found"

        config_page.set_channel_validator(validator)
        async with await _client() as client:
            resp = await client.put(
                "/config/api/settings/AUTO_DISPATCH_CHANNEL?force=1", json={"value": "C0AGZC613NKK"}
            )
        assert resp.status == 200
        assert _isolated_settings.get("AUTO_DISPATCH_CHANNEL") == "C0AGZC613NKK"

    @pytest.mark.asyncio
    async def test_validator_outage_does_not_block_save(self, _isolated_settings):
        async def validator(channel_id: str) -> str | None:
            raise RuntimeError("slack is down")

        config_page.set_channel_validator(validator)
        async with await _client() as client:
            resp = await client.put("/config/api/settings/AUTO_DISPATCH_CHANNEL", json={"value": "C0AGZC613NKK"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_valid_channel_passes(self, _isolated_settings):
        seen: list[str] = []

        async def validator(channel_id: str) -> str | None:
            seen.append(channel_id)
            return None

        config_page.set_channel_validator(validator)
        async with await _client() as client:
            resp = await client.put("/config/api/settings/AUTO_DISPATCH_CHANNEL", json={"value": "C0AGZC613NKK"})
        assert resp.status == 200
        assert seen == ["C0AGZC613NKK"]


class TestDeleteSetting:
    @pytest.mark.asyncio
    async def test_delete_unsets_and_reports_new_source(self, _isolated_settings, monkeypatch):
        monkeypatch.setenv("AUTO_DISPATCH_CHANNEL", "CENVENVENV")
        _isolated_settings.set("AUTO_DISPATCH_CHANNEL", "CFILEFILE1")
        async with await _client() as client:
            resp = await client.delete("/config/api/settings/AUTO_DISPATCH_CHANNEL")
            body = await resp.json()
        assert body == {"ok": True, "key": "AUTO_DISPATCH_CHANNEL", "existed": True, "source": "env"}
        assert _isolated_settings.get("AUTO_DISPATCH_CHANNEL") == "CENVENVENV"

    @pytest.mark.asyncio
    async def test_delete_unknown_key_404(self):
        async with await _client() as client:
            resp = await client.delete("/config/api/settings/NOPE")
        assert resp.status == 404


class TestTokenFiles:
    @pytest.mark.asyncio
    async def test_put_list_delete_roundtrip(self, _isolated_secrets_dir):
        async with await _client() as client:
            resp = await client.put("/config/api/tokenfiles/gh-aidt-merge.token", json={"content": "ghp_abc123"})
            assert resp.status == 200
            assert (_isolated_secrets_dir / "gh-aidt-merge.token").read_text() == "ghp_abc123\n"

            resp = await client.get("/config/api/tokenfiles")
            body = await resp.json()
            assert [f["name"] for f in body["files"]] == ["gh-aidt-merge.token"]
            # contents are never listed
            assert "ghp_abc123" not in json.dumps(body)

            resp = await client.delete("/config/api/tokenfiles/gh-aidt-merge.token")
            assert resp.status == 200
            assert not (_isolated_secrets_dir / "gh-aidt-merge.token").exists()

    @pytest.mark.asyncio
    async def test_written_file_has_owner_only_permissions(self, _isolated_secrets_dir):
        async with await _client() as client:
            await client.put("/config/api/tokenfiles/x.token", json={"content": "t"})
        mode = (_isolated_secrets_dir / "x.token").stat().st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.asyncio
    async def test_traversal_and_bad_names_rejected(self, _isolated_secrets_dir):
        bad = ["..%2F..%2Fetc%2Fpasswd.token", "no-suffix", ".hidden.token", "a%2Fb.token"]
        async with await _client() as client:
            for name in bad:
                resp = await client.put(f"/config/api/tokenfiles/{name}", json={"content": "x"})
                assert resp.status in (400, 404), name
        assert not _isolated_secrets_dir.exists() or not list(_isolated_secrets_dir.iterdir())

    @pytest.mark.asyncio
    async def test_empty_content_rejected(self):
        async with await _client() as client:
            resp = await client.put("/config/api/tokenfiles/x.token", json={"content": "   "})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_delete_missing_404(self):
        async with await _client() as client:
            resp = await client.delete("/config/api/tokenfiles/nope.token")
        assert resp.status == 404


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_short_secret_masked_without_suffix(self, _isolated_settings):
        _isolated_settings.set("WORKERS_BOT_TOKEN", "tiny")
        async with await _client() as client:
            body = await (await client.get("/config/api/settings")).json()
        by_key = {s["key"]: s for s in body["settings"]}
        assert by_key["WORKERS_BOT_TOKEN"]["value"] == "••••"

    @pytest.mark.asyncio
    async def test_non_json_body_400(self):
        async with await _client() as client:
            resp = await client.put(
                "/config/api/settings/SESSION_TIMEOUT",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversized_body_400(self):
        async with await _client() as client:
            resp = await client.put(
                "/config/api/settings/SESSION_TIMEOUT",
                data=json.dumps({"value": "x" * (70 * 1024)}),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_agent_discovery_failure_omits_boot_agents_but_serves(self, monkeypatch):
        def _boom():
            raise RuntimeError("discovery broken")

        monkeypatch.setattr("router.config.get_agent_map", _boom)
        async with await _client() as client:
            resp = await client.get("/config/api/settings")
            body = await resp.json()
        assert resp.status == 200
        assert not any(s["key"] == "LISA_BOT_TOKEN" for s in body["settings"])

    @pytest.mark.asyncio
    async def test_delete_tokenfile_bad_name_400(self):
        async with await _client() as client:
            resp = await client.delete("/config/api/tokenfiles/not-a-token")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_ui_asset_500(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config_page, "STATIC_DIR", tmp_path / "nowhere")
        async with await _client() as client:
            resp = await client.get("/config")
        assert resp.status == 500


class TestPage:
    @pytest.mark.asyncio
    async def test_page_served(self):
        async with await _client() as client:
            resp = await client.get("/config")
            text = await resp.text()
        assert resp.status == 200
        assert "Runtime Config" in text
        assert "ssh -L 8080:127.0.0.1:8080" in text

    @pytest.mark.asyncio
    async def test_healthz_app_mounts_config_routes(self):
        from router import healthz

        async with TestClient(TestServer(healthz.build_app())) as client:
            resp = await client.get("/config/api/settings")
            assert resp.status == 200
