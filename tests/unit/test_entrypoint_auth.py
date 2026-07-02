"""Unit tests for the CLAUDE_AUTH_MODE dispatch block in docker/entrypoint.sh.

The entrypoint uses gosu and docker-specific paths that aren't available in a
unit-test environment, so we extract only the auth-dispatch block and drive it
in a subshell with a synthetic environment.  The extracted snippet is portable
bash — no gosu, no filesystem side-effects.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"

# Probe script that runs after the auth block to report which vars survived.
# We echo a machine-readable line so tests can assert without parsing stderr.
_PROBE = """
echo "AUTH_MODE_RESULT=${_AUTH_MODE}"
echo "HAS_API_KEY=${ANTHROPIC_API_KEY:-}"
echo "HAS_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}"
"""


def _extract_auth_block(script_body: str) -> str:
    """Pull the CLAUDE_AUTH_MODE case block out of the entrypoint source.

    The block starts at the comment line containing 'CLAUDE_AUTH_MODE' and ends
    at the closing 'esac'.  Returns the raw block text.
    """
    start = script_body.find("_AUTH_MODE=")
    assert start != -1, "auth-mode block not found in entrypoint.sh"
    esac_idx = script_body.find("esac", start)
    assert esac_idx != -1, "closing 'esac' not found in entrypoint.sh"
    end = script_body.index("\n", esac_idx) + 1
    return script_body[start:end]


def _run_auth_block(env_vars: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the entrypoint auth block in a clean subshell with the given env."""
    body = ENTRYPOINT.read_text()
    auth_block = _extract_auth_block(body)

    # Stub out the credentials-file check so tests don't need a real filesystem.
    # We replace the -f check with a simple false so the WARNING is emitted but
    # the block still falls through (exit 0 for credentials mode).
    auth_block_stubbed = auth_block.replace(
        "[ ! -f /home/claude/.claude/.credentials.json ]",
        "false",  # pretend credentials.json always exists → no WARNING
    )

    snippet = "\n".join(
        [
            "set -euo pipefail",
            auth_block_stubbed,
            _PROBE,
        ]
    )

    env_export = "\n".join(f"export {k}={v!r}" for k, v in env_vars.items())
    full_script = env_export + "\n" + snippet

    return subprocess.run(
        ["bash", "-c", full_script],
        capture_output=True,
        text=True,
    )


def _parse_result(stdout: str) -> dict[str, str]:
    """Parse the probe KEY=VALUE lines into a dict."""
    result = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            result[k] = v
    return result


class TestOauthTokenMode:
    def test_succeeds_when_token_present(self):
        result = _run_auth_block(
            {
                "CLAUDE_AUTH_MODE": "oauth_token",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-secret",
                "ANTHROPIC_API_KEY": "sk-ant-leftover",
            }
        )
        assert result.returncode == 0, result.stderr

    def test_exports_oauth_token(self):
        result = _run_auth_block(
            {
                "CLAUDE_AUTH_MODE": "oauth_token",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-secret",
            }
        )
        assert result.returncode == 0
        parsed = _parse_result(result.stdout)
        assert parsed["HAS_OAUTH_TOKEN"] == "tok-secret"

    def test_unsets_api_key(self):
        """oauth_token mode must actively unset ANTHROPIC_API_KEY."""
        result = _run_auth_block(
            {
                "CLAUDE_AUTH_MODE": "oauth_token",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-secret",
                "ANTHROPIC_API_KEY": "sk-ant-stale",
            }
        )
        assert result.returncode == 0
        parsed = _parse_result(result.stdout)
        assert parsed["HAS_API_KEY"] == "", "ANTHROPIC_API_KEY must be unset in oauth_token mode"

    def test_hard_fails_when_token_missing(self):
        result = _run_auth_block({"CLAUDE_AUTH_MODE": "oauth_token"})
        assert result.returncode != 0

    def test_error_message_mentions_oauth_token_var(self):
        result = _run_auth_block({"CLAUDE_AUTH_MODE": "oauth_token"})
        assert "CLAUDE_CODE_OAUTH_TOKEN" in result.stderr


class TestApiKeyMode:
    def test_succeeds_when_key_present(self):
        result = _run_auth_block(
            {
                "CLAUDE_AUTH_MODE": "api_key",
                "ANTHROPIC_API_KEY": "sk-ant-real",
            }
        )
        assert result.returncode == 0, result.stderr

    def test_exports_api_key(self):
        result = _run_auth_block(
            {
                "CLAUDE_AUTH_MODE": "api_key",
                "ANTHROPIC_API_KEY": "sk-ant-real",
            }
        )
        assert result.returncode == 0
        parsed = _parse_result(result.stdout)
        assert parsed["HAS_API_KEY"] == "sk-ant-real"

    def test_unsets_oauth_token(self):
        """api_key mode must actively unset CLAUDE_CODE_OAUTH_TOKEN."""
        result = _run_auth_block(
            {
                "CLAUDE_AUTH_MODE": "api_key",
                "ANTHROPIC_API_KEY": "sk-ant-real",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-stale",
            }
        )
        assert result.returncode == 0
        parsed = _parse_result(result.stdout)
        assert parsed["HAS_OAUTH_TOKEN"] == "", "CLAUDE_CODE_OAUTH_TOKEN must be unset in api_key mode"

    def test_hard_fails_when_key_missing(self):
        result = _run_auth_block({"CLAUDE_AUTH_MODE": "api_key"})
        assert result.returncode != 0

    def test_error_message_mentions_api_key_var(self):
        result = _run_auth_block({"CLAUDE_AUTH_MODE": "api_key"})
        assert "ANTHROPIC_API_KEY" in result.stderr


class TestCredentialsMode:
    def test_succeeds_with_explicit_mode(self):
        result = _run_auth_block({"CLAUDE_AUTH_MODE": "credentials"})
        assert result.returncode == 0, result.stderr

    def test_succeeds_when_mode_unset(self):
        """Unset CLAUDE_AUTH_MODE must default to credentials (backward-compat)."""
        result = _run_auth_block({})
        assert result.returncode == 0, result.stderr

    def test_unsets_both_secrets(self):
        """credentials mode must not leak stale api_key or oauth_token to the CLI."""
        result = _run_auth_block(
            {
                "CLAUDE_AUTH_MODE": "credentials",
                "ANTHROPIC_API_KEY": "sk-ant-stale",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-stale",
            }
        )
        assert result.returncode == 0
        parsed = _parse_result(result.stdout)
        assert parsed["HAS_API_KEY"] == "", "ANTHROPIC_API_KEY must be unset in credentials mode"
        assert parsed["HAS_OAUTH_TOKEN"] == "", "CLAUDE_CODE_OAUTH_TOKEN must be unset in credentials mode"

    def test_default_mode_is_credentials(self):
        result = _run_auth_block({})
        assert result.returncode == 0
        parsed = _parse_result(result.stdout)
        assert parsed["AUTH_MODE_RESULT"] == "credentials"


class TestInvalidMode:
    def test_hard_fails_on_unknown_mode(self):
        result = _run_auth_block({"CLAUDE_AUTH_MODE": "magic_beans"})
        assert result.returncode != 0

    def test_error_lists_valid_values(self):
        result = _run_auth_block({"CLAUDE_AUTH_MODE": "bogus"})
        assert "oauth_token" in result.stderr
        assert "api_key" in result.stderr
        assert "credentials" in result.stderr

    def test_error_shows_invalid_value(self):
        result = _run_auth_block({"CLAUDE_AUTH_MODE": "bogus"})
        assert "bogus" in result.stderr


class TestRenderComposeAuthEnvVars:
    """Regression guard: render_compose.py must emit all three auth vars for agents."""

    def test_agent_env_includes_claude_auth_mode(self, tmp_path):
        import textwrap

        from router.config import discover_agents
        from scripts.render_compose import build_compose

        base = tmp_path / "agents"
        (base / "lisa").mkdir(parents=True)
        (base / "lisa" / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                thinking_status: "thinking…"
                capabilities: {}
            """)
        )
        agents = discover_agents(base)
        compose = build_compose(agents, base)
        env = compose["services"]["lisa"]["environment"]

        assert "CLAUDE_AUTH_MODE=${CLAUDE_AUTH_MODE:-credentials}" in env

    def test_agent_env_includes_anthropic_api_key(self, tmp_path):
        import textwrap

        from router.config import discover_agents
        from scripts.render_compose import build_compose

        base = tmp_path / "agents"
        (base / "lisa").mkdir(parents=True)
        (base / "lisa" / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                thinking_status: "thinking…"
                capabilities: {}
            """)
        )
        agents = discover_agents(base)
        compose = build_compose(agents, base)
        env = compose["services"]["lisa"]["environment"]

        assert "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}" in env

    def test_agent_env_includes_claude_code_oauth_token(self, tmp_path):
        import textwrap

        from router.config import discover_agents
        from scripts.render_compose import build_compose

        base = tmp_path / "agents"
        (base / "lisa").mkdir(parents=True)
        (base / "lisa" / "agent.yaml").write_text(
            textwrap.dedent("""\
                name: Lisa
                container: lisa
                thinking_status: "thinking…"
                capabilities: {}
            """)
        )
        agents = discover_agents(base)
        compose = build_compose(agents, base)
        env = compose["services"]["lisa"]["environment"]

        assert "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}" in env
