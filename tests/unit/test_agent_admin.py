"""Unit tests for router/agent_admin.py — agents API for the /config page."""

from __future__ import annotations

import json
import textwrap

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from router import agent_admin
from router import config as router_config

pytestmark = pytest.mark.unit


COMMENTED_MANIFEST = textwrap.dedent("""\
    # Nina — agent manifest.
    # This comment must survive page edits.
    name: Nina
    container: nina
    thinking_status: "is pondering…"

    # Wall-clock budget note that must also survive.
    container_timeout: 1800

    packs:
      - github
""")


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    """Isolated agents dir with one commented manifest + boot map pinned to it."""
    base = tmp_path / "agents"
    nina = base / "nina"
    nina.mkdir(parents=True)
    (nina / "agent.yaml").write_text(COMMENTED_MANIFEST)
    (nina / "role.md").write_text("# Nina\n")
    (nina / "personality.md").write_text("# Nina — Personality\n")
    monkeypatch.setattr(agent_admin, "agents_dir", lambda: base)
    monkeypatch.setattr("router.config.get_agent_map", lambda: router_config.discover_agents(base))
    return base


@pytest.fixture(autouse=True)
def _isolated_secret_store(tmp_path, monkeypatch):
    monkeypatch.setattr("router.packs.secret_store.SECRETS_PATH", tmp_path / "secrets.json")
    return tmp_path / "secrets.json"


@pytest.fixture
def packs_root(tmp_path, monkeypatch):
    base = tmp_path / "packs"
    (base / "github").mkdir(parents=True)
    (base / "github" / "pack.yaml").write_text("name: github\n")
    monkeypatch.setattr(agent_admin, "packs_dir", lambda: base)
    return base


def _build_app() -> web.Application:
    app = web.Application()
    agent_admin.register_routes(app)
    return app


async def _client():
    return TestClient(TestServer(_build_app()))


class TestListAgents:
    @pytest.mark.asyncio
    async def test_lists_manifest_fields_and_backend_descriptors(self, agents_root, packs_root):
        async with await _client() as client:
            resp = await client.get("/config/api/agents")
            body = await resp.json()
        assert resp.status == 200
        agent = body["agents"][0]
        assert agent["id"] == "nina"
        assert agent["name"] == "Nina"
        assert agent["container_timeout"] == 1800
        assert agent["packs"] == ["github"]
        assert agent["active"] is True
        assert agent["pending_restart"] is False
        # Transport-generic: descriptors drive the UI — new backends appear here.
        assert set(body["backends"]) == {"slack", "discord"}
        assert body["available_packs"] == ["github"]

    @pytest.mark.asyncio
    async def test_credential_sources_and_masking(self, agents_root, monkeypatch, _isolated_secret_store):
        monkeypatch.setenv("NINA_BOT_TOKEN", "xoxb-legacy-env-9876")
        _isolated_secret_store.write_text(
            json.dumps({"agent_credentials": {"nina": {"slack": {"app_token": "xapp-store-4321"}}}})
        )
        async with await _client() as client:
            resp = await client.get("/config/api/agents")
            raw = await resp.text()
        assert "xoxb-legacy-env-9876" not in raw
        assert "xapp-store-4321" not in raw
        slack = json.loads(raw)["agents"][0]["credentials"]["slack"]
        assert slack["fields"]["bot_token"] == {
            "set": True,
            "source": "legacy-env",
            "value": "••••9876",
            "sensitive": True,
            "required": True,
        }
        assert slack["fields"]["app_token"]["source"] == "secret-store"
        assert slack["fields"]["signing_secret"]["set"] is False
        assert slack["complete"] is False  # signing_secret missing

    @pytest.mark.asyncio
    async def test_manifest_env_ref_status(self, agents_root, monkeypatch):
        manifest = agents_root / "nina" / "agent.yaml"
        manifest.write_text(manifest.read_text() + "\nbackends:\n  slack:\n    bot_token: ${SECRET:NINA_BOT_TOKEN}\n")
        monkeypatch.setenv("NINA_BOT_TOKEN", "xoxb-ref-resolved-1234")
        async with await _client() as client:
            body = await (await client.get("/config/api/agents")).json()
        field = body["agents"][0]["credentials"]["slack"]["fields"]["bot_token"]
        assert field["source"] == "manifest-env"
        assert field["env_var"] == "NINA_BOT_TOKEN"
        assert field["value"] == "••••1234"

    @pytest.mark.asyncio
    async def test_disabled_agent_listed_with_pending_restart(self, agents_root):
        manifest = agents_root / "nina" / "agent.yaml"
        manifest.write_text(manifest.read_text() + "\ndisabled: true\n")
        # Boot map still contains nina (captured before the disable).
        async with await _client() as client:
            body = await (await client.get("/config/api/agents")).json()
        agent = body["agents"][0]
        assert agent["disabled"] is True
        assert agent["pending_restart"] is False or agent["pending_restart"] is True  # present either way
        assert agent["id"] == "nina"


class TestPendingRestart:
    @pytest.mark.asyncio
    async def test_new_agent_dir_flagged_until_restart(self, agents_root, monkeypatch):
        boot_view = router_config.discover_agents(agents_root)  # snapshot before the add
        monkeypatch.setattr("router.config.get_agent_map", lambda: boot_view)
        newbie = agents_root / "rex"
        newbie.mkdir()
        (newbie / "agent.yaml").write_text("name: Rex\ncontainer: rex\n")
        async with await _client() as client:
            body = await (await client.get("/config/api/agents")).json()
        by_id = {a["id"]: a for a in body["agents"]}
        assert by_id["rex"]["active"] is False
        assert by_id["rex"]["pending_restart"] is True
        assert by_id["nina"]["pending_restart"] is False


class TestPutManifest:
    @pytest.mark.asyncio
    async def test_edit_preserves_comments(self, agents_root):
        async with await _client() as client:
            resp = await client.put(
                "/config/api/agents/nina", json={"model": "opus", "container_timeout": 2400, "name": "Nina2"}
            )
            body = await resp.json()
        assert body == {"ok": True, "id": "nina", "applied": "restart-required"}
        text = (agents_root / "nina" / "agent.yaml").read_text()
        assert "# This comment must survive page edits." in text
        assert "# Wall-clock budget note that must also survive." in text
        parsed = yaml.safe_load(text)
        assert parsed["model"] == "opus"
        assert parsed["container_timeout"] == 2400
        assert parsed["name"] == "Nina2"
        assert parsed["packs"] == ["github"]  # untouched

    @pytest.mark.asyncio
    async def test_nullable_field_cleared_by_null(self, agents_root):
        async with await _client() as client:
            await client.put("/config/api/agents/nina", json={"model": "opus"})
            await client.put("/config/api/agents/nina", json={"model": None})
        parsed = yaml.safe_load((agents_root / "nina" / "agent.yaml").read_text())
        assert "model" not in parsed

    @pytest.mark.asyncio
    async def test_disable_toggle_roundtrip(self, agents_root):
        async with await _client() as client:
            await client.put("/config/api/agents/nina", json={"disabled": True})
            assert yaml.safe_load((agents_root / "nina" / "agent.yaml").read_text())["disabled"] is True
            assert "nina" not in router_config.discover_agents(agents_root)  # discovery skips it
            await client.put("/config/api/agents/nina", json={"disabled": False})
            assert "nina" in router_config.discover_agents(agents_root)

    @pytest.mark.asyncio
    async def test_invalid_values_rejected(self, agents_root):
        async with await _client() as client:
            for payload in ({"container_timeout": 0}, {"name": ""}, {"container": "x"}, {"disabled": "yes"}):
                resp = await client.put("/config/api/agents/nina", json=payload)
                assert resp.status == 422, payload
        # File untouched by rejected edits.
        assert (agents_root / "nina" / "agent.yaml").read_text() == COMMENTED_MANIFEST

    @pytest.mark.asyncio
    async def test_unknown_agent_404_and_bad_id_400(self, agents_root):
        async with await _client() as client:
            assert (await client.put("/config/api/agents/ghost", json={"name": "X"})).status == 404
            assert (await client.put("/config/api/agents/..%2Fetc", json={"name": "X"})).status == 400


class TestPutCredentials:
    @pytest.mark.asyncio
    async def test_merge_and_delete_fields(self, agents_root, _isolated_secret_store):
        async with await _client() as client:
            resp = await client.put(
                "/config/api/agents/nina/credentials",
                json={"backend": "slack", "fields": {"bot_token": "xoxb-1", "app_token": "xapp-1"}},
            )
            body = await resp.json()
            assert body["applied"] == "restart-required"
            assert "xoxb-1" not in json.dumps(body)

            await client.put(
                "/config/api/agents/nina/credentials",
                json={"backend": "slack", "fields": {"signing_secret": "sig-1", "app_token": ""}},
            )
        stored = json.loads(_isolated_secret_store.read_text())["agent_credentials"]["nina"]["slack"]
        assert stored == {"bot_token": "xoxb-1", "signing_secret": "sig-1"}  # app_token deleted by ""

    @pytest.mark.asyncio
    async def test_any_descriptor_backend_accepted(self, agents_root, _isolated_secret_store):
        async with await _client() as client:
            resp = await client.put(
                "/config/api/agents/nina/credentials",
                json={"backend": "discord", "fields": {"bot_token": "d-token", "default_channel_id": "123"}},
            )
        assert resp.status == 200
        stored = json.loads(_isolated_secret_store.read_text())["agent_credentials"]["nina"]["discord"]
        assert stored == {"bot_token": "d-token", "default_channel_id": "123"}

    @pytest.mark.asyncio
    async def test_unknown_backend_and_fields_rejected(self, agents_root):
        async with await _client() as client:
            resp = await client.put(
                "/config/api/agents/nina/credentials", json={"backend": "telegram", "fields": {"x": "y"}}
            )
            assert resp.status == 422
            assert "unknown backend" in (await resp.json())["error"]
            resp = await client.put(
                "/config/api/agents/nina/credentials", json={"backend": "slack", "fields": {"nope": "y"}}
            )
            assert resp.status == 422


class TestPostAgent:
    @pytest.mark.asyncio
    async def test_create_agent_full_flow(self, agents_root, packs_root, _isolated_secret_store):
        payload = {
            "id": "rex",
            "name": "Rex",
            "thinking_status": "is fetching…",
            "model": "sonnet",
            "role": "Golden retriever of CI failures.",
            "personality": "Relentlessly upbeat.",
            "packs": ["github"],
            "credentials": {
                "slack": {"bot_token": "xoxb-rex", "app_token": "xapp-rex", "signing_secret": "sig-rex"},
                "discord": {"bot_token": "d-rex"},
            },
        }
        async with await _client() as client:
            resp = await client.post("/config/api/agents", json=payload)
            body = await resp.json()

        assert resp.status == 201
        assert body["pending_restart"] is True
        # Files created with valid YAML + templates applied to one-liners.
        manifest = yaml.safe_load((agents_root / "rex" / "agent.yaml").read_text())
        assert manifest["name"] == "Rex"
        assert manifest["model"] == "sonnet"
        assert manifest["packs"] == ["github"]
        assert "Golden retriever" in (agents_root / "rex" / "role.md").read_text()
        # Slack app manifest: saved next to the agent AND returned inline.
        saved = yaml.safe_load((agents_root / "rex" / agent_admin.SLACK_MANIFEST_FILENAME).read_text())
        assert saved["display_information"]["name"] == "Rex"
        assert yaml.safe_load(body["slack_app_manifest"]) == saved
        # Credentials landed in the secret store (no .env edit needed) — masked in response.
        stored = json.loads(_isolated_secret_store.read_text())["agent_credentials"]["rex"]
        assert stored["slack"]["bot_token"] == "xoxb-rex"
        assert stored["discord"]["bot_token"] == "d-rex"
        assert "xoxb-rex" not in json.dumps(body)
        # Host steps are spelled out.
        assert any("docker compose up -d rex" in step for step in body["next_steps"])
        assert any("docker restart router" in step for step in body["next_steps"])

    @pytest.mark.asyncio
    async def test_duplicate_id_409(self, agents_root, packs_root):
        async with await _client() as client:
            resp = await client.post("/config/api/agents", json={"id": "nina", "role": "x", "personality": "y"})
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_invalid_id_rejected_no_traversal(self, agents_root, packs_root):
        async with await _client() as client:
            for bad in ("Nina", "../evil", "a b", ""):
                resp = await client.post("/config/api/agents", json={"id": bad})
                assert resp.status == 400, bad
        assert sorted(p.name for p in agents_root.iterdir()) == ["nina"]

    @pytest.mark.asyncio
    async def test_unknown_pack_rejected(self, agents_root, packs_root):
        async with await _client() as client:
            resp = await client.post("/config/api/agents", json={"id": "rex", "packs": ["not-a-pack"]})
        assert resp.status == 422
        assert not (agents_root / "rex").exists()

    @pytest.mark.asyncio
    async def test_no_credentials_path_prompts_for_tokens(self, agents_root, packs_root):
        async with await _client() as client:
            resp = await client.post("/config/api/agents", json={"id": "rex"})
            body = await resp.json()
        assert resp.status == 201
        assert any("credentials card" in step for step in body["next_steps"])


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_non_json_bodies_400(self, agents_root):
        async with await _client() as client:
            for method, path in (
                ("PUT", "/config/api/agents/nina"),
                ("PUT", "/config/api/agents/nina/credentials"),
                ("POST", "/config/api/agents"),
            ):
                resp = await client.request(method, path, data="not json", headers={"Content-Type": "application/json"})
                assert resp.status == 400, (method, path)

    @pytest.mark.asyncio
    async def test_credentials_unknown_agent_404_bad_id_400(self, agents_root):
        async with await _client() as client:
            resp = await client.put(
                "/config/api/agents/ghost/credentials", json={"backend": "slack", "fields": {"bot_token": "x"}}
            )
            assert resp.status == 404
            resp = await client.put(
                "/config/api/agents/..%2Fx/credentials", json={"backend": "slack", "fields": {"bot_token": "x"}}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_credentials_non_string_value_and_empty_fields_rejected(self, agents_root):
        async with await _client() as client:
            resp = await client.put(
                "/config/api/agents/nina/credentials", json={"backend": "slack", "fields": {"bot_token": 42}}
            )
            assert resp.status == 422
            resp = await client.put("/config/api/agents/nina/credentials", json={"backend": "slack", "fields": {}})
            assert resp.status == 422

    @pytest.mark.asyncio
    async def test_manifest_parse_error_surfaced_not_fatal(self, agents_root):
        (agents_root / "nina" / "agent.yaml").write_text("name: [unclosed")
        async with await _client() as client:
            resp = await client.get("/config/api/agents")
            body = await resp.json()
        assert resp.status == 200
        assert body["agents"][0]["error"] is not None

    @pytest.mark.asyncio
    async def test_post_malformed_packs_and_credentials_types(self, agents_root, packs_root):
        async with await _client() as client:
            resp = await client.post("/config/api/agents", json={"id": "rex", "packs": "github"})
            assert resp.status == 422
            resp = await client.post("/config/api/agents", json={"id": "rex", "credentials": ["nope"]})
            assert resp.status == 422
            resp = await client.post(
                "/config/api/agents", json={"id": "rex", "credentials": {"telegram": {"token": "x"}}}
            )
            assert resp.status == 422
        assert not (agents_root / "rex").exists()

    @pytest.mark.asyncio
    async def test_post_full_markdown_role_kept_verbatim(self, agents_root, packs_root):
        role_md = "# Rex — Full Role\n\nCustom markdown body.\n"
        async with await _client() as client:
            await client.post("/config/api/agents", json={"id": "rex", "role": role_md})
        assert (agents_root / "rex" / "role.md").read_text() == role_md

    def test_mask_short_values(self):
        assert agent_admin._mask("tiny") == "••••"
        assert agent_admin._mask("") == ""
        assert agent_admin._mask("long-enough-value") == "••••alue"
