"""Governance ratchets for the runtime-config contract (#576).

These tests exist to stop the pre-#576 mess from growing back. The old
failure mode: every new setting became (1) a scattered ``os.environ`` read,
(2) a docker-compose ``environment:`` line, and (3) an ``.env`` edit — so a
one-character change meant patching files and recreating containers.

The contract (docs/runtime-config.md): new settings are declared ONCE in the
registry (``router/settings.py``) and read via ``settings.get()``. Direct env
reads and compose plumbing are reserved for boot-tier credentials.

Both tests are ratchets — the frozen lists below may SHRINK as call sites
migrate, but growing them requires deliberately editing this file, which is
the review checkpoint. If you got here because a test failed on your new
code: add a registry entry in ``router/settings.py`` and call
``settings.get("YOUR_KEY")`` instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from router.settings import REGISTRY

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ROUTER_DIR = REPO_ROOT / "router"

_ENV_READ_RE = re.compile(r"os\.(environ|getenv)")

# Frozen allowlist of direct-env-read counts per router file. Every entry is
# boot-tier (credentials, paths resolved at process start) or a documented
# store-first fallback. Counts may go DOWN freely; going UP fails the ratchet.
ALLOWED_ENV_READS: dict[str, int] = {
    # The settings layer itself — env is its fallback tier.
    "settings.py": 3,
    # Legacy per-agent Slack/Discord credential fallback + ${SECRET:...}
    # resolution (boot) + the store-vs-env conflict warning probe.
    "config.py": 7,
    # ROUTER_INTERNAL_TOKEN (boot, fail-fast).
    "internal_api.py": 2,
    # Store-first workers-token fallbacks (documented store-over-env sites).
    "app.py": 2,
    "packs/dispatch_hook.py": 2,
    # Boot-tier paths / credentials.
    "scheduled_tasks/bootstrap.py": 1,  # SCHEDULED_TASKS_DB
    "scheduled_tasks/wakeup_verbs.py": 2,  # per-dispatch context injected by the supervisor
    "packs/oauth_devicecode.py": 2,  # GITHUB_CLIENT_ID (boot)
    "memory_identity.py": 1,  # alias-map path
    "healthz.py": 1,  # slack-token presence probe for readiness
    "dispatch/state.py": 1,  # dispatch root path
    "dispatch/drain.py": 1,  # drain webhook (one-shot ops tooling)
    "config_page.py": 1,  # boot-env display (read-only section of the UI)
}


def _count_env_reads() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(ROUTER_DIR.rglob("*.py")):
        rel = path.relative_to(ROUTER_DIR).as_posix()
        n = len(_ENV_READ_RE.findall(path.read_text(encoding="utf-8")))
        if n:
            counts[rel] = n
    return counts


class TestNoNewDirectEnvReads:
    def test_router_env_reads_do_not_grow(self):
        """New ``os.environ``/``os.getenv`` in router/ fails here by design.

        Add a registry entry in router/settings.py and read the value via
        ``settings.get()`` instead — that makes it editable on the /config
        page and hot-reloadable where possible. Only genuinely boot-tier
        reads belong in the allowlist above (and adding one is a reviewed,
        deliberate edit to this file).
        """
        offenders = {}
        for rel, n in _count_env_reads().items():
            allowed = ALLOWED_ENV_READS.get(rel, 0)
            if n > allowed:
                offenders[rel] = (n, allowed)
        assert not offenders, (
            "Direct env reads grew in router/ (found > allowed): "
            f"{offenders}. New settings must go through the registry in "
            "router/settings.py + settings.get() — see docs/runtime-config.md. "
            "If this read is genuinely boot-tier, raise its allowlist entry in "
            "tests/unit/test_config_governance.py with a comment saying why."
        )

    def test_allowlist_has_no_stale_entries(self):
        """Keep the ratchet honest: when a file drops its env reads, shrink the allowlist."""
        actual = _count_env_reads()
        stale = {rel: allowed for rel, allowed in ALLOWED_ENV_READS.items() if actual.get(rel, 0) < allowed}
        assert not stale, (
            f"Allowlist entries exceed actual env-read counts: {stale}. "
            "Lower them in tests/unit/test_config_governance.py so the ratchet only turns one way."
        )


# Per-agent credential suffixes the compose renderer emits for each agent —
# boot-tier by definition (Socket Mode / Discord gateway credentials).
_PER_AGENT_SUFFIXES = (
    "_BOT_TOKEN",
    "_APP_TOKEN",
    "_SIGNING_SECRET",
    "_DISCORD_BOT_TOKEN",
    "_DISCORD_CHANNEL_ID",
)


class TestComposeEnvIsFrozen:
    def _router_env_keys(self) -> set[str]:
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        env_lines = compose["services"]["router"]["environment"]
        return {line.split("=", 1)[0] for line in env_lines}

    def test_router_compose_env_only_carries_registry_known_vars(self):
        """Every static env var plumbed into the router via compose must be a
        registry key. A var that compose knows but the registry doesn't means
        someone is routing config around the settings layer again."""
        keys = self._router_env_keys()
        static = {k for k in keys if not any(k.endswith(s) for s in _PER_AGENT_SUFFIXES)}
        unknown = static - set(REGISTRY)
        assert not unknown, (
            f"docker-compose.yml routes env vars the settings registry does not know: {sorted(unknown)}. "
            "Declare them in router/settings.py (and ask whether they need compose plumbing at all — "
            "runtime vars are served from config/runtime.json and need NO compose line)."
        )

    def test_router_compose_env_does_not_grow(self):
        """The compose env block is legacy fallback plumbing — frozen as of #576.

        New runtime vars are read from config/runtime.json via the registry
        and need NO compose line, no .env edit, and no container recreate.
        Only a genuinely boot-tier value (a credential the process must hold
        before it can read anything) justifies growing this set — grow it
        here deliberately, with review.
        """
        keys = self._router_env_keys()
        static = {k for k in keys if not any(k.endswith(s) for s in _PER_AGENT_SUFFIXES)}
        frozen = {
            "WORKERS_BOT_TOKEN",
            "WORKERS_DISCORD_TOKEN",
            "SESSION_TIMEOUT",
            "LOG_LEVEL",
            "SLASH_COMMAND_PREFIX",
            "WORKER_MENTION_HANDOFF",
            "DISPATCH_MILESTONE_FEED",
            "ATTACHMENTS_ENABLED",
            "DISCORD_ENABLED",
            "MERGE_QUEUE_REPO",
            "MERGE_QUEUE_CHANNEL",
            "AUTO_DISPATCH_CHANNEL",
            "AUTO_DISPATCH_REPO",
            "ROUTER_INTERNAL_TOKEN",
        }
        new = static - frozen
        assert not new, (
            f"New env vars appeared in the router compose block: {sorted(new)}. "
            "Runtime settings do not need compose plumbing — add a registry entry in "
            "router/settings.py and read via settings.get(). See docs/runtime-config.md."
        )
