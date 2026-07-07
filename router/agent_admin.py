"""Agents admin API — per-agent section of the /config page (configurable-agents work).

Endpoints (registered onto the health server's app by router/config_page.py;
same loopback-only / SSH-tunnel trust model):

    GET  /config/api/agents                    — list agents: manifest fields,
                                                 credential status per backend
                                                 (masked), active vs pending-restart
    POST /config/api/agents                    — create an agent from the page
    PUT  /config/api/agents/{id}               — edit manifest scalars / disabled
    PUT  /config/api/agents/{id}/credentials   — store credentials in data/secrets.json

Design notes
------------
* **Transport-generic**: the :data:`BACKENDS` descriptor drives credential
  validation, the GET response shape, and the UI form rendering. Adding a
  transport (Telegram, …) is one descriptor entry plus its router adapter —
  no new endpoint or form code.
* **Boot-cached agent map stays boot-cached**: handlers scan the agents dir
  fresh for display and diff against :func:`router.config.get_agent_map`
  (the boot view) to compute ``pending_restart``. Sockets and routing are
  boot-bound regardless, so agent changes are honestly restart-class.
* **Manifest edits preserve comments**: scalar-key text surgery + re-parse
  validation + atomic replace (generalized from router/packs/grants.py) —
  never a ``yaml.safe_dump`` round-trip of a hand-commented file.
* **Disabled agents stay listed** here (unlike discovery, which skips them)
  — otherwise a disabled agent would vanish from the page with no way back.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from aiohttp import web

from router import config as router_config
from router import config_web
from router.agent_scaffold import (
    NAME_RE,
    AgentSpec,
    personality_template,
    role_template,
    slack_manifest_yaml,
    write_agent_files,
)
from router.atomic_io import atomic_write_text
from router.config_web import mask as _mask
from router.packs.secret_store import SecretStore

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Single source of truth for the ${SECRET:NAME} syntax lives in router.config.
_SECRET_REF_RE = router_config._SECRET_REF_RE
_MAX_BODY_BYTES = 256 * 1024

SLACK_MANIFEST_FILENAME = "slack-app-manifest.yaml"

# ── transport descriptor ─────────────────────────────────────────────────────
# One entry per chat backend. Drives credential validation, the GET response,
# and the UI form. Adding a transport (e.g. "telegram") = one entry here plus
# its adapter under router/chat/adapters/.
BACKENDS: dict[str, dict[str, dict[str, Any]]] = {
    "slack": {
        "bot_token": {"sensitive": True, "required": True, "legacy_env_suffix": "_BOT_TOKEN"},
        "app_token": {"sensitive": True, "required": True, "legacy_env_suffix": "_APP_TOKEN"},
        "signing_secret": {"sensitive": True, "required": True, "legacy_env_suffix": "_SIGNING_SECRET"},
    },
    "discord": {
        "bot_token": {"sensitive": True, "required": True, "legacy_env_suffix": "_DISCORD_BOT_TOKEN"},
        "default_channel_id": {"sensitive": False, "required": False, "legacy_env_suffix": "_DISCORD_CHANNEL_ID"},
    },
}

# Manifest scalars editable from the page: key → (type, nullable).
EDITABLE_MANIFEST_FIELDS: dict[str, tuple[str, bool]] = {
    "name": ("str", False),
    "model": ("str", True),
    "thinking_status": ("str", False),
    "container_timeout": ("int", True),
    "disabled": ("bool", False),
}


def agents_dir() -> Path:
    """The live agents directory (same resolution order as discovery)."""
    if router_config.DEFAULT_AGENTS_DIR.exists():
        return router_config.DEFAULT_AGENTS_DIR
    return router_config.CONFIG_DIR / "agents"


def packs_dir() -> Path:
    """Pack definitions (image copy in the container, repo dir in dev/CI)."""
    mounted = Path("/app/packs")
    if mounted.is_dir():
        return mounted
    return REPO_ROOT / "packs"


def _available_pack_names() -> list[str]:
    base = packs_dir()
    if not base.is_dir():
        return []
    return sorted(
        p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("_") and (p / "pack.yaml").exists()
    )


# ── credential status ────────────────────────────────────────────────────────


def _field_status(agent_id: str, backend: str, field: str, meta: dict, manifest: dict) -> dict[str, Any]:
    """Resolve one credential field's status: set?, winning source, masked value.

    Source order mirrors router.config's loaders: secret-store → manifest
    backends block (${SECRET:X} env ref or literal) → legacy env convention.
    """
    stored = router_config.agent_store_credentials(agent_id, backend).get(field)
    if isinstance(stored, str) and stored:
        return {"set": True, "source": "secret-store", "value": _mask(stored) if meta["sensitive"] else stored}

    backends_block = manifest.get("backends") or {}
    manifest_val = (backends_block.get(backend) or {}).get(field) if isinstance(backends_block, dict) else None
    if isinstance(manifest_val, str) and manifest_val:
        m = _SECRET_REF_RE.match(manifest_val)
        if m:
            env_val = os.environ.get(m.group(1), "")
            return {
                "set": bool(env_val),
                "source": "manifest-env" if env_val else "unset",
                "value": (_mask(env_val) if meta["sensitive"] else env_val) if env_val else "",
                "env_var": m.group(1),
            }
        return {
            "set": True,
            "source": "manifest-literal",
            "value": _mask(manifest_val) if meta["sensitive"] else manifest_val,
        }

    legacy = os.environ.get(f"{agent_id.upper()}{meta['legacy_env_suffix']}", "")
    if legacy:
        return {"set": True, "source": "legacy-env", "value": _mask(legacy) if meta["sensitive"] else legacy}
    return {"set": False, "source": "unset", "value": ""}


def _credentials_summary(agent_id: str, manifest: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for backend, fields in BACKENDS.items():
        field_states = {f: _field_status(agent_id, backend, f, meta, manifest) for f, meta in fields.items()}
        complete = all(field_states[f]["set"] for f, meta in fields.items() if meta["required"])
        out[backend] = {
            "complete": complete,
            "fields": {
                f: {**state, "sensitive": fields[f]["sensitive"], "required": fields[f]["required"]}
                for f, state in field_states.items()
            },
        }
    return out


# ── GET /config/api/agents ───────────────────────────────────────────────────


def _agent_entry(agent_dir: Path, boot_map: dict[str, dict], fresh_map: dict[str, dict]) -> dict[str, Any]:
    agent_id = agent_dir.name
    manifest_path = agent_dir / "agent.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
        if not isinstance(manifest, dict):
            raise ValueError("agent.yaml is not a YAML mapping")
        parse_error = None
    except Exception as exc:
        manifest = {}
        parse_error = str(exc)

    active = agent_id in boot_map
    in_fresh = agent_id in fresh_map
    # Restart pending when boot view and fresh (on-disk) view disagree for
    # this agent — newly added, newly disabled/enabled, or edited manifest.
    pending_restart = (active != in_fresh) or (active and in_fresh and boot_map[agent_id] != fresh_map[agent_id])

    return {
        "id": agent_id,
        "error": parse_error,
        "name": manifest.get("name") or agent_id.capitalize(),
        "container": manifest.get("container") or agent_id,
        "model": manifest.get("model"),
        "container_timeout": manifest.get("container_timeout"),
        "thinking_status": manifest.get("thinking_status", ""),
        "dispatch_workspace": bool(manifest.get("dispatch_workspace", False)),
        "disabled": bool(manifest.get("disabled", False)),
        "packs": list(manifest.get("packs") or []),
        "scheduled_tasks": len(manifest.get("scheduled_tasks") or []),
        "files": {
            "role": (agent_dir / "role.md").exists(),
            "personality": (agent_dir / "personality.md").exists(),
            "dockerfile": (agent_dir / "Dockerfile").exists(),
            "slack_manifest": (agent_dir / SLACK_MANIFEST_FILENAME).exists(),
        },
        "active": active,
        "pending_restart": pending_restart,
        "credentials": _credentials_summary(agent_id, manifest),
    }


async def _handle_list_agents(request: web.Request) -> web.Response:
    base = agents_dir()
    boot_map = router_config.get_agent_map()
    fresh_map = router_config.discover_agents(base)
    entries = []
    if base.is_dir():
        for agent_dir in sorted(p for p in base.iterdir() if p.is_dir() and (p / "agent.yaml").exists()):
            entries.append(_agent_entry(agent_dir, boot_map, fresh_map))
    return web.json_response(
        {
            "agents": entries,
            "agents_dir": str(base),
            "backends": {
                b: {f: {k: v for k, v in meta.items() if k != "legacy_env_suffix"} for f, meta in fields.items()}
                for b, fields in BACKENDS.items()
            },
            "available_packs": _available_pack_names(),
        }
    )


# ── manifest editing (comment-preserving) ────────────────────────────────────


def _scalar_line(key: str, value: Any) -> str:
    """Render ``key: value`` as a single YAML line."""
    return yaml.safe_dump({key: value}, default_flow_style=False, allow_unicode=True, width=100000).rstrip("\n")


def _replace_or_append_scalar(text: str, key: str, value: Any, *, remove: bool = False) -> str:
    """Replace a top-level scalar ``key:`` line in ``text``, preserving comments.

    Appends the line when the key is absent; ``remove=True`` drops the line.
    Same technique as grants.py's packs-block surgery, restricted to
    single-line scalar keys.
    """
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*:[^\n]*\n?")
    if remove:
        return pattern.sub("", text, count=1)
    line = _scalar_line(key, value) + "\n"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line


def _atomic_write_validated(path: Path, new_text: str, expect: dict[str, Any]) -> None:
    """Write only if the result parses AND carries the expected values."""
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"refusing to write malformed manifest {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"refusing to write malformed manifest {path}: not a mapping")
    for key, value in expect.items():
        if parsed.get(key) != value:
            raise RuntimeError(f"manifest edit for {key!r} did not round-trip (block scalar in the way?)")
    atomic_write_text(path, new_text)


def _coerce_manifest_value(key: str, raw: Any) -> Any:
    ftype, nullable = EDITABLE_MANIFEST_FIELDS[key]
    if raw is None or raw == "":
        if nullable:
            return None
        raise ValueError(f"{key} must not be empty")
    if ftype == "int":
        try:
            value = int(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{key}: expected an integer, got {raw!r}") from exc
        if value < 1:
            raise ValueError(f"{key}: must be >= 1")
        return value
    if ftype == "bool":
        if isinstance(raw, bool):
            return raw
        raise ValueError(f"{key}: expected a boolean")
    if not isinstance(raw, str):
        raise ValueError(f"{key}: expected a string")
    return raw


async def _read_json_body(request: web.Request) -> dict[str, Any] | None:
    return await config_web.read_json_body(request, max_bytes=_MAX_BODY_BYTES)


def _safe_agent_dir(agent_id: str) -> Path | None:
    if not NAME_RE.match(agent_id):
        return None
    return agents_dir() / agent_id


async def _handle_put_agent(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    agent_dir = _safe_agent_dir(agent_id)
    if agent_dir is None:
        return web.json_response({"error": "invalid agent id"}, status=400)
    manifest_path = agent_dir / "agent.yaml"
    if not manifest_path.exists():
        return web.json_response({"error": f"unknown agent {agent_id!r}"}, status=404)

    body = await _read_json_body(request)
    if not body:
        return web.json_response({"error": "body must be a JSON object of manifest fields"}, status=400)
    unknown = set(body) - set(EDITABLE_MANIFEST_FIELDS)
    if unknown:
        return web.json_response(
            {
                "error": (
                    f"non-editable fields: {', '.join(sorted(unknown))} "
                    f"(editable: {', '.join(EDITABLE_MANIFEST_FIELDS)})"
                )
            },
            status=422,
        )

    try:
        coerced = {key: _coerce_manifest_value(key, raw) for key, raw in body.items()}
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)

    text = manifest_path.read_text()
    expect: dict[str, Any] = {}
    for key, value in coerced.items():
        remove = value is None
        text = _replace_or_append_scalar(text, key, value, remove=remove)
        if not remove:
            expect[key] = value
    try:
        _atomic_write_validated(manifest_path, text, expect)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=422)

    logger.info("config page: agent %s manifest updated (%s)", agent_id, ", ".join(coerced))
    return web.json_response({"ok": True, "id": agent_id, "applied": "restart-required"})


# ── credentials ──────────────────────────────────────────────────────────────


def _store_credentials(agent_id: str, backend: str, fields: dict[str, str]) -> None:
    """Merge ``fields`` into agent_credentials.<id>.<backend>; '' deletes a field."""
    store = SecretStore()
    block = store.get(router_config.AGENT_CREDENTIALS_KEY)
    agent_block = dict(block.get(agent_id) or {})
    backend_block = dict(agent_block.get(backend) or {})
    for key, value in fields.items():
        if value == "":
            backend_block.pop(key, None)
        else:
            backend_block[key] = value
    if backend_block:
        agent_block[backend] = backend_block
    else:
        agent_block.pop(backend, None)
    if agent_block:
        block[agent_id] = agent_block
    else:
        block.pop(agent_id, None)
    store.set(router_config.AGENT_CREDENTIALS_KEY, block)


def _validate_credential_fields(backend: str, fields: Any) -> dict[str, str] | str:
    """Return normalized fields, or an error string."""
    descriptor = BACKENDS.get(backend)
    if descriptor is None:
        return f"unknown backend {backend!r} (known: {', '.join(BACKENDS)})"
    if not isinstance(fields, dict) or not fields:
        return "fields must be a non-empty object"
    unknown = set(fields) - set(descriptor)
    if unknown:
        return f"unknown {backend} fields: {', '.join(sorted(unknown))} (known: {', '.join(descriptor)})"
    normalized: dict[str, str] = {}
    for key, value in fields.items():
        if not isinstance(value, str):
            return f"{key}: credential values must be strings"
        normalized[key] = value.strip()
    return normalized


async def _handle_put_credentials(request: web.Request) -> web.Response:
    agent_id = request.match_info["id"]
    agent_dir = _safe_agent_dir(agent_id)
    if agent_dir is None:
        return web.json_response({"error": "invalid agent id"}, status=400)
    if not (agent_dir / "agent.yaml").exists():
        return web.json_response({"error": f"unknown agent {agent_id!r}"}, status=404)

    body = await _read_json_body(request)
    if body is None:
        return web.json_response({"error": "body must be JSON: {backend, fields}"}, status=400)
    backend = body.get("backend", "")
    fields = _validate_credential_fields(backend, body.get("fields"))
    if isinstance(fields, str):
        return web.json_response({"error": fields}, status=422)

    _store_credentials(agent_id, backend, fields)
    logger.info("config page: %s credentials updated for agent %s (%s)", backend, agent_id, ", ".join(fields))
    return web.json_response({"ok": True, "id": agent_id, "backend": backend, "applied": "restart-required"})


# ── POST /config/api/agents (add-agent from the page) ────────────────────────


def _next_steps(agent_id: str, has_slack_creds: bool) -> list[str]:
    steps = [
        "Create the Slack app: paste the manifest below at https://api.slack.com/apps "
        f"(also saved as config/agents/{agent_id}/{SLACK_MANIFEST_FILENAME}), install it to the workspace",
    ]
    if not has_slack_creds:
        steps.append(f"Paste the 3 Slack tokens into {agent_id}'s credentials card on this page")
    steps += [
        f"On the host: python -m scripts.render_compose && docker compose up -d {agent_id}",
        f"On the host (credentials auth mode): docker exec -it {agent_id} claude auth login",
        "docker restart router — activates routing, /wakeup, and drain for the new agent",
    ]
    return steps


async def _handle_post_agent(request: web.Request) -> web.Response:
    body = await _read_json_body(request)
    if body is None:
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    agent_id = str(body.get("id") or "").strip()
    if not NAME_RE.match(agent_id):
        return web.json_response({"error": f"invalid agent id (must match {NAME_RE.pattern})"}, status=400)
    base = agents_dir()
    if (base / agent_id).exists():
        return web.json_response({"error": f"agent {agent_id!r} already exists"}, status=409)

    packs = body.get("packs") or []
    if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
        return web.json_response({"error": "packs must be a list of pack names"}, status=422)
    available = set(_available_pack_names())
    unknown_packs = [p for p in packs if p not in available]
    if unknown_packs:
        return web.json_response({"error": f"unknown packs: {', '.join(unknown_packs)}"}, status=422)

    credentials = body.get("credentials") or {}
    if not isinstance(credentials, dict):
        return web.json_response({"error": "credentials must be an object keyed by backend"}, status=422)
    normalized_creds: dict[str, dict[str, str]] = {}
    for backend, fields in credentials.items():
        if not fields:
            continue
        normalized = _validate_credential_fields(backend, fields)
        if isinstance(normalized, str):
            return web.json_response({"error": normalized}, status=422)
        normalized_creds[backend] = {k: v for k, v in normalized.items() if v}

    display_name = str(body.get("name") or agent_id.capitalize())
    role_text = str(body.get("role") or "").strip()
    personality_text = str(body.get("personality") or "").strip()
    spec = AgentSpec(
        id=agent_id,
        name=display_name,
        container=agent_id,
        thinking_status=str(body.get("thinking_status") or "is thinking…"),
        # Short one-liners get wrapped in the standard templates; full
        # markdown (anything multi-line or starting with '#') is kept as-is,
        # normalized to end with exactly one newline.
        role=role_text + "\n"
        if role_text.startswith("#") or "\n" in role_text
        else role_template(display_name, role_text or "Role description placeholder."),
        personality=personality_text + "\n"
        if personality_text.startswith("#") or "\n" in personality_text
        else personality_template(display_name, personality_text or "Personality placeholder."),
        packs=list(packs),
    )

    try:
        write_agent_files(spec, base)
    except FileExistsError:
        return web.json_response({"error": f"agent {agent_id!r} already exists"}, status=409)

    model = body.get("model")
    if isinstance(model, str) and model.strip():
        manifest_path = base / agent_id / "agent.yaml"
        text = _replace_or_append_scalar(manifest_path.read_text(), "model", model.strip())
        _atomic_write_validated(manifest_path, text, {"model": model.strip()})

    manifest_yaml = slack_manifest_yaml(spec)
    (base / agent_id / SLACK_MANIFEST_FILENAME).write_text(manifest_yaml)

    for backend, fields in normalized_creds.items():
        if fields:
            _store_credentials(agent_id, backend, fields)

    logger.info("config page: agent %s created (packs=%s, creds=%s)", agent_id, packs, sorted(normalized_creds))
    return web.json_response(
        {
            "ok": True,
            "id": agent_id,
            "pending_restart": True,
            "slack_app_manifest": manifest_yaml,
            "next_steps": _next_steps(agent_id, has_slack_creds=bool(normalized_creds.get("slack"))),
        },
        status=201,
    )


def register_routes(app: web.Application) -> None:
    app.router.add_get("/config/api/agents", _handle_list_agents)
    app.router.add_post("/config/api/agents", _handle_post_agent)
    app.router.add_put("/config/api/agents/{id}", _handle_put_agent)
    app.router.add_put("/config/api/agents/{id}/credentials", _handle_put_credentials)
