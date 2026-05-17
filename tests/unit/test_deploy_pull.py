"""Unit tests for scripts/deploy-pull.sh — Slack payload escaping.

The deploy script interpolates commit subjects into a Slack webhook
payload. Subjects can legally contain ``"`` and ``\\``, both of which
break the JSON if pasted into a raw string body. We use ``jq`` to
build the payload; this test verifies the function tolerates the
tricky characters end-to-end.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_PULL = REPO_ROOT / "scripts" / "deploy-pull.sh"


def _call_build_slack_payload(subject: str) -> str:
    """Invoke ``build_slack_payload`` from scripts/deploy-pull.sh in a subshell.

    Sourcing the script directly would run its top-level body. Instead we
    extract the function definition and evaluate just that, keeping the
    test hermetic (no flock, no docker, no curl).
    """
    snippet = (
        "set -euo pipefail\n"
        "build_slack_payload() {\n"
        "    jq -cn --arg text \"$1\" '{text: $text}'\n"
        "}\n"
        'build_slack_payload "$1"\n'
    )
    result = subprocess.run(
        ["bash", "-c", snippet, "_test", subject],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_build_slack_payload_escapes_quotes_and_backslashes():
    """Commit subject `"hello" \\world` must produce valid JSON whose
    ``text`` round-trips byte-for-byte."""
    subject = ':rocket: deployed abc123 — "hello" \\world'
    raw = _call_build_slack_payload(subject)

    # `jq -e .` would also catch invalid JSON; using json.loads here so
    # the round-trip assertion lives in Python.
    parsed = json.loads(raw)
    assert parsed == {"text": subject}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_build_slack_payload_validates_with_jq_e():
    """Same assertion via the acceptance-criterion route: pipe to `jq -e .`."""
    subject = 'commit with "quotes" and \\backslashes'
    raw = _call_build_slack_payload(subject)
    # `jq -e .` exits non-zero when the input isn't valid JSON or is
    # falsey at the top level — for a JSON object both are caught.
    subprocess.run(["jq", "-e", "."], input=raw, check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_build_slack_payload_handles_newlines_and_unicode():
    """Multi-line and non-ASCII subjects round-trip too."""
    subject = "line one\nline two — 🚀 unicode"
    raw = _call_build_slack_payload(subject)
    parsed = json.loads(raw)
    assert parsed == {"text": subject}


def test_deploy_pull_uses_jq_for_slack_payload():
    """Guard against regressions to the unescaped ``-d "{\\"text\\":..."`` form."""
    body = DEPLOY_PULL.read_text()
    assert "build_slack_payload()" in body, "expected dedicated jq-based payload builder"
    assert "jq -cn --arg text" in body, "build_slack_payload should call jq with --arg"
    # The old raw-interpolation pattern must be gone.
    assert '"{\\"text\\":\\"${text}\\"}"' not in body
    assert '-d "{\\"text\\":\\"' not in body


def test_deploy_pull_body_is_in_brace_group():
    """Self-edit safety: the body must be wrapped in a brace group so bash
    slurps the whole file before executing it, surviving a mid-run
    ``git reset --hard`` that rewrites the script."""
    body = DEPLOY_PULL.read_text()
    lines = [line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]
    # First non-comment lines should be `set -euo pipefail`, then `{`.
    assert lines[0] == "set -euo pipefail"
    assert lines[1] == "{"
    # Last non-comment line should close the group.
    assert lines[-1] == "}"


def test_deploy_pull_renders_compose_before_docker_build():
    """``docker-compose.yml`` is gitignored — generated from
    ``config/agents/*/agent.yaml`` by ``scripts/render_compose``. A deploy
    that adds or changes an agent must re-render the file before
    ``docker compose build`` runs, otherwise the new service silently has
    no entry. Guard: a ``make compose`` invocation must appear between
    the forward ``git reset --hard "origin/$BRANCH"`` and the
    ``docker compose build`` call.
    """
    body = DEPLOY_PULL.read_text()
    reset_marker = 'git reset --hard "origin/$BRANCH"'
    build_marker = "docker compose build"
    reset_idx = body.find(reset_marker)
    build_idx = body.find(build_marker)
    assert reset_idx != -1, "expected forward `git reset --hard \"origin/$BRANCH\"` in deploy-pull.sh"
    assert build_idx != -1, "expected `docker compose build` in deploy-pull.sh"
    assert reset_idx < build_idx, "forward git reset must precede docker compose build"
    between = body[reset_idx:build_idx]
    assert "make compose" in between, (
        "deploy-pull.sh must invoke `make compose` between the forward "
        "git-reset and `docker compose build` so generated docker-compose.yml "
        "matches the deployed SHA's agent manifests"
    )


def test_deploy_pull_renders_compose_during_auto_revert():
    """The auto-revert path must also re-render compose: after
    ``git reset --hard "$LOCAL"`` the on-disk compose still matches the
    bad SHA's agent set unless we re-render. Guard: a ``make compose``
    must appear between the revert reset and the revert ``docker compose
    up`` invocation, and must be wrapped so a render failure falls
    through to the existing "auto-revert ALSO failed" branch rather than
    killing the script via ``set -e``.
    """
    body = DEPLOY_PULL.read_text()
    revert_reset_marker = 'git reset --hard "$LOCAL"'
    revert_reset_idx = body.find(revert_reset_marker)
    assert revert_reset_idx != -1, "expected revert `git reset --hard \"$LOCAL\"` in deploy-pull.sh"
    # The auto-revert's `docker compose up` is the *second* occurrence in
    # the file (the first is the happy-path one above). Find it relative
    # to the revert reset.
    revert_up_idx = body.find("docker compose up", revert_reset_idx)
    assert revert_up_idx != -1, "expected revert `docker compose up` after revert git-reset"
    between = body[revert_reset_idx:revert_up_idx]
    assert "make compose" in between, (
        "auto-revert path must re-render compose so the reverted SHA's "
        "agent manifests are reflected on disk before restarting"
    )
    # Render failure during revert must not be fatal via `set -e`; it
    # should be caught by an `if !` so we can route to the timer-stop
    # branch instead.
    assert "if ! make compose" in between, (
        "revert-path `make compose` must be guarded with `if !` so a "
        "render failure falls through to the auto-revert-failure branch "
        "rather than killing the script silently"
    )
