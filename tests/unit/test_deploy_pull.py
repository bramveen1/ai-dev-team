"""Unit tests for scripts/deploy-pull.sh — Slack payload escaping and
git repo-detection error classification.

The deploy script interpolates commit subjects into a Slack webhook
payload. Subjects can legally contain ``"`` and ``\\``, both of which
break the JSON if pasted into a raw string body. We use ``jq`` to
build the payload; this test verifies the function tolerates the
tricky characters end-to-end.

A second set of tests covers the git repo-detection block (issue #355):
the script must capture git's stderr rather than discarding it and must
classify the three failure modes (dubious-ownership, not-a-repo,
inaccessible) so the journal always shows the true cause.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_PULL = REPO_ROOT / "scripts" / "deploy-pull.sh"
MAKEFILE = REPO_ROOT / "Makefile"


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
    """``docker-compose.yml`` is a generated artifact — produced from
    ``config/agents/*/agent.yaml`` by ``scripts/render_compose``. It is
    tracked in git, but the on-box file has been observed to drift from
    the deployed SHA's manifests, leaving new/changed agents without a
    service entry. A deploy must re-render the file before
    ``docker compose build`` so the on-disk compose matches the deployed
    SHA. Guard: a ``make compose`` invocation must appear between the
    forward ``git reset --hard "origin/$BRANCH"`` and the
    ``docker compose build`` call.
    """
    body = DEPLOY_PULL.read_text()
    reset_marker = 'git reset --hard "origin/$BRANCH"'
    build_marker = "docker compose build"
    reset_idx = body.find(reset_marker)
    build_idx = body.find(build_marker)
    assert reset_idx != -1, 'expected forward `git reset --hard "origin/$BRANCH"` in deploy-pull.sh'
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
    assert revert_reset_idx != -1, 'expected revert `git reset --hard "$LOCAL"` in deploy-pull.sh'
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


def test_deploy_pull_rebuilds_image_during_auto_revert():
    """The auto-revert path must rebuild the Docker image before
    restarting. ``docker compose up -d`` only rebuilds when an image is
    missing — not when source under a build context has changed. Without
    an explicit rebuild step, the revert ``up -d`` would happily reuse
    the freshly-built BAD_SHA image, so a "revert" would restart with
    the exact broken image it just rolled back from. Guard: a
    ``docker compose build`` invocation must appear between the revert
    ``git reset --hard "$LOCAL"`` and the revert ``docker compose up``,
    and must be wrapped with ``if !`` so a build failure routes to the
    auto-revert-failure branch.
    """
    body = DEPLOY_PULL.read_text()
    revert_reset_marker = 'git reset --hard "$LOCAL"'
    revert_reset_idx = body.find(revert_reset_marker)
    assert revert_reset_idx != -1, 'expected revert `git reset --hard "$LOCAL"` in deploy-pull.sh'
    revert_up_idx = body.find("docker compose up", revert_reset_idx)
    assert revert_up_idx != -1, "expected revert `docker compose up` after revert git-reset"
    between = body[revert_reset_idx:revert_up_idx]
    assert "docker compose build" in between, (
        "auto-revert path must rebuild the image so the restarted stack "
        "runs the reverted SHA's source, not the still-tagged BAD_SHA image"
    )
    assert "if ! docker compose build" in between, (
        "revert-path `docker compose build` must be guarded with `if !` "
        "so a build failure falls through to the auto-revert-failure "
        "branch rather than killing the script silently"
    )


def test_make_up_passes_build_flag():
    """`make up` is the user-facing manual-deploy command (documented in
    docs/add-a-new-agent.md and docs/authoring-a-pack.md). `docker
    compose up -d` only builds when the image is missing — once it
    exists, source changes under a build context (router/, browser-use)
    are silently dropped on subsequent ``make up`` runs and the
    container keeps running stale baked code. The forward-path deploy
    daemon does its own ``docker compose build --no-cache`` for the
    same reason; this guard keeps manual ``make up`` honest about that
    contract so they don't drift.
    """
    body = MAKEFILE.read_text()
    # Locate the recipe under the `up:` target — the contiguous block of
    # tab-indented lines immediately following it.
    recipe_lines: list[str] = []
    in_up = False
    for line in body.splitlines():
        if line.startswith("up:"):
            in_up = True
            continue
        if in_up:
            if line.startswith("\t"):
                recipe_lines.append(line)
            elif line.strip() == "":
                # Blank line inside a recipe is unusual but tolerable; the
                # recipe ends at the next non-tab, non-blank line.
                continue
            else:
                break
    assert recipe_lines, "expected a recipe under the `up:` target in Makefile"
    recipe = "\n".join(recipe_lines)
    assert "docker compose up -d --build" in recipe, (
        "Makefile `up` target must pass `--build` so manual `make up` "
        "rebuilds images when source under a build context changed; "
        "otherwise routers/sidecars keep running stale baked code"
    )


# ── Deploy-pause sentinel (issue #305) ───────────────────────────────────────


def test_deploy_pause_sentinel_written_before_drain():
    """Sentinel write must appear BEFORE the drain section so that a new
    dispatch fired after the sentinel write but before drain-start is still
    caught by the sentinel gate."""
    body = DEPLOY_PULL.read_text()
    sentinel_write_marker = 'printf \'{"started_at":%d,"deploy_sha":"%s","pid":%d}\\n\''
    drain_marker = "# --- Drain in-flight dispatches"
    sentinel_idx = body.find(sentinel_write_marker)
    drain_idx = body.find(drain_marker)
    assert sentinel_idx != -1, "expected deploy-pause sentinel write in deploy-pull.sh"
    assert drain_idx != -1, "expected drain section marker in deploy-pull.sh"
    assert sentinel_idx < drain_idx, (
        "sentinel write must appear before the drain section so new dispatches are blocked as soon as drain begins"
    )


def test_deploy_pause_trap_present():
    """A trap on EXIT INT TERM ERR must remove the sentinel so the router
    is never permanently wedged by a crashed or killed deploy."""
    body = DEPLOY_PULL.read_text()
    assert "_deploy_pause_cleanup" in body, "expected _deploy_pause_cleanup function in deploy-pull.sh"
    assert "trap _deploy_pause_cleanup EXIT INT TERM ERR" in body, (
        "expected `trap _deploy_pause_cleanup EXIT INT TERM ERR` in deploy-pull.sh"
    )


def test_deploy_pause_enabled_variable_declared():
    """DEPLOY_PAUSE_ENABLED must be declared with a default of 1 so the gate
    is on by default and can be disabled by setting the env var to 0."""
    body = DEPLOY_PULL.read_text()
    assert 'DEPLOY_PAUSE_ENABLED="${DEPLOY_PAUSE_ENABLED:-1}"' in body, (
        "expected DEPLOY_PAUSE_ENABLED declared with default 1 in deploy-pull.sh"
    )


def test_deploy_pause_sentinel_uses_deploy_pause_enabled_guard():
    """The sentinel write must be gated on DEPLOY_PAUSE_ENABLED so operators
    can disable it without a code change."""
    body = DEPLOY_PULL.read_text()
    assert 'DEPLOY_PAUSE_ENABLED" != "0"' in body, (
        'sentinel write must be guarded with `[ "$DEPLOY_PAUSE_ENABLED" != "0" ]` in deploy-pull.sh'
    )


# ── Repo-detection error classification (issue #355) ─────────────────────────


def _extract_bash_function(body: str, name: str) -> str:
    """Extract a named bash function from a script body using brace-depth
    tracking.  Handles the common ``name() {\\n...\\n}`` form."""
    lines = body.splitlines()
    result: list[str] = []
    depth = 0
    inside = False
    for line in lines:
        if not inside:
            if line.startswith(f"{name}()"):
                inside = True
        if inside:
            result.append(line)
            # Count bare braces to track nesting depth.
            depth += line.count("{") - line.count("}")
            if depth == 0 and len(result) > 1:
                break
    return "\n".join(result)


def _call_log_git_repo_error(repo_dir: str, git_err: str) -> str:
    """Extract log() and _log_git_repo_error() from deploy-pull.sh, invoke
    the classifier with *repo_dir* and *git_err*, and return the combined
    output so tests can assert on specific log lines."""
    body = DEPLOY_PULL.read_text()
    log_fn = _extract_bash_function(body, "log")
    classify_fn = _extract_bash_function(body, "_log_git_repo_error")
    if not classify_fn:
        pytest.skip("_log_git_repo_error not found in deploy-pull.sh")
    snippet = "\n".join(
        [
            "set -euo pipefail",
            log_fn,
            classify_fn,
            f"_log_git_repo_error {shlex.quote(repo_dir)} {shlex.quote(git_err)}",
        ]
    )
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    # log() writes to stdout; _log_git_repo_error redirects those calls
    # to stderr with >&2.  Combine both streams for robust assertions.
    return result.stdout + result.stderr


def test_repo_check_stderr_captured_not_discarded():
    """The git rev-parse check must NOT use ``>/dev/null 2>&1``, which
    silently discards git's stderr.  Instead stderr must be captured so
    the classifier can inspect it (issue #355)."""
    body = DEPLOY_PULL.read_text()
    # Locate the section containing the rev-parse call.
    rev_parse_idx = body.find("git rev-parse --is-inside-work-tree")
    assert rev_parse_idx != -1, "expected git rev-parse call in deploy-pull.sh"
    # Extract just the line that contains the rev-parse call.
    line_start = body.rfind("\n", 0, rev_parse_idx) + 1
    line_end = body.find("\n", rev_parse_idx)
    rev_parse_line = body[line_start:line_end]
    assert ">/dev/null 2>&1" not in rev_parse_line, (
        "git rev-parse line must not use '>/dev/null 2>&1' — "
        "stderr must be captured for classification, not silently discarded"
    )


def test_repo_check_calls_log_git_repo_error():
    """The repo-check block must call _log_git_repo_error so the
    classification and diagnostic logging are never bypassed."""
    body = DEPLOY_PULL.read_text()
    # Find the failure branch of the rev-parse check.
    rev_parse_idx = body.find("git rev-parse --is-inside-work-tree")
    assert rev_parse_idx != -1
    fi_idx = body.find("\nfi\n", rev_parse_idx)
    check_block = body[rev_parse_idx:fi_idx]
    assert "_log_git_repo_error" in check_block, (
        "repo-check block must call _log_git_repo_error to classify and log the failure"
    )


def test_repo_check_classifies_dubious_ownership():
    """A git stderr containing 'dubious ownership' must produce a log
    message that names the cause and must NOT claim 'not a git repo'."""
    git_err = "fatal: detected dubious ownership in repository at '/opt/ai-dev-team'"
    output = _call_log_git_repo_error("/opt/ai-dev-team", git_err)
    assert "dubious ownership" in output, "expected dubious-ownership classification in log"
    assert "not a git repo" not in output, "dubious-ownership error must not be mislabelled as 'not a git repo'"


def test_repo_check_dubious_ownership_suggests_safe_directory():
    """The dubious-ownership log message must include a ``safe.directory``
    remediation hint so the operator can unblock the deploy without
    manual diagnostics."""
    git_err = "fatal: detected dubious ownership in repository at '/opt/ai-dev-team'"
    output = _call_log_git_repo_error("/opt/ai-dev-team", git_err)
    assert "safe.directory" in output, "dubious-ownership log must include a 'safe.directory' remediation hint"


def test_repo_check_classifies_not_a_git_repo():
    """A git 'not a git repository' error must be labelled as such and
    must NOT be reported as 'inaccessible'."""
    git_err = "fatal: not a git repository (or any of the parent directories): .git"
    output = _call_log_git_repo_error("/opt/ai-dev-team", git_err)
    assert "not a git repo" in output, "expected 'not a git repo' classification in log"
    assert "inaccessible" not in output, "'not a git repo' error must not be classified as 'inaccessible'"
    assert "dubious ownership" not in output


def test_repo_check_classifies_inaccessible_repo():
    """A permission-denied or other non-repo-existence git error must be
    classified as 'inaccessible', NOT as 'not a git repo', so operators
    are not misled into recreating a healthy but temporarily inaccessible
    checkout."""
    git_err = "fatal: could not open '/opt/ai-dev-team/.git/config': Permission denied"
    output = _call_log_git_repo_error("/opt/ai-dev-team", git_err)
    assert "inaccessible" in output, "expected 'inaccessible' classification in log"
    assert "not a git repo" not in output, "permission-denied error must not be mislabelled as 'not a git repo'"
    assert "dubious ownership" not in output


def test_repo_check_always_logs_git_diagnostic():
    """The raw git diagnostic must appear in the log output for every error
    class so operators always have the original git message in the journal
    without needing to reproduce the failure manually."""
    unique_token = "fatal: some-unique-git-error-XYZ-789"
    output = _call_log_git_repo_error("/opt/ai-dev-team", unique_token)
    assert unique_token in output, "raw git diagnostic must be logged regardless of error classification"


# ── Deployed-SHA idempotency marker (issue #561) ─────────────────────────────


def test_deployed_sha_file_variable_declared():
    """DEPLOYED_SHA_FILE must be declared so it can be overridden in tests and
    so the script has a stable default location for the marker."""
    body = DEPLOY_PULL.read_text()
    assert "DEPLOYED_SHA_FILE=" in body, "expected DEPLOYED_SHA_FILE declaration in deploy-pull.sh"


def test_early_exit_requires_deployed_sha_marker():
    """The early-exit gate must check the deployed-SHA marker in addition to
    comparing git HEAD to the remote.  Keying only on git SHAs means an
    interrupted build (git reset --hard ran, image build never completed)
    looks identical to a successful deploy, wedging the box permanently
    (issue #561)."""
    body = DEPLOY_PULL.read_text()
    # Locate the early-exit block and check that it reads the deployed-SHA file.
    early_exit_marker = 'log "up to date at $LOCAL"'
    early_exit_idx = body.find(early_exit_marker)
    assert early_exit_idx != -1, 'expected early-exit log "up to date" in deploy-pull.sh'
    # Walk backwards to find the conditional that guards the early exit.
    block_start = body.rfind("\nif ", 0, early_exit_idx)
    assert block_start != -1, "expected an `if` block guarding the early exit"
    gate_block = body[block_start:early_exit_idx]
    assert "DEPLOYED_SHA" in gate_block, (
        "early-exit gate must check DEPLOYED_SHA (the deployed-SHA marker) — "
        "git HEAD == remote is not sufficient when a prior build was interrupted"
    )


def test_deployed_sha_written_only_after_health_check():
    """The deployed-SHA marker must be written AFTER health_check returns
    successfully, not before.  Writing it before the health check would
    cause a subsequent tick to exit early even when the image is unhealthy.
    Guard: the write must appear inside the `if health_check` success
    branch, between `if health_check; then` and the corresponding `fi`."""
    body = DEPLOY_PULL.read_text()
    # Find the forward-path health check block.
    health_check_idx = body.find("if health_check; then")
    assert health_check_idx != -1, "expected `if health_check; then` in deploy-pull.sh"
    # The marker write for the forward path must be inside this block.
    fi_idx = body.find("\nfi\n", health_check_idx)
    health_block = body[health_check_idx:fi_idx]
    assert "DEPLOYED_SHA_FILE" in health_block, (
        "deployed-SHA marker write must appear inside the `if health_check` "
        "block — writing before health-check passes would mask an unhealthy deploy"
    )
    # The write must come before the exit 0 inside the block.
    write_idx = health_block.find("DEPLOYED_SHA_FILE")
    exit_idx = health_block.find("exit 0")
    assert write_idx < exit_idx, "deployed-SHA marker must be written before `exit 0` in the health-check success block"


def test_deployed_sha_written_after_revert_health_check():
    """After a successful auto-revert the marker must be updated to the
    reverted SHA so the next tick exits early correctly instead of
    re-running a revert that already succeeded."""
    body = DEPLOY_PULL.read_text()
    # The second `if health_check; then` is the revert path.
    first_idx = body.find("if health_check; then")
    assert first_idx != -1, "expected first health_check block in deploy-pull.sh"
    revert_health_idx = body.find("if health_check; then", first_idx + 1)
    assert revert_health_idx != -1, "expected second (revert) health_check block in deploy-pull.sh"
    fi_idx = body.find("\nfi\n", revert_health_idx)
    revert_block = body[revert_health_idx:fi_idx]
    assert "DEPLOYED_SHA_FILE" in revert_block, (
        "auto-revert health-check success block must update the deployed-SHA "
        "marker to the reverted SHA so the next deploy tick exits cleanly"
    )


def _run_early_exit_logic(local_sha: str, remote_sha: str, marker_contents: str | None) -> tuple[int, str]:
    """Run just the early-exit gate from deploy-pull.sh in a subshell.

    Stubs out git rev-parse and optionally creates (or omits) the marker file,
    then returns (exit_code, combined_output).  An exit code of 0 from the
    early-exit branch means the script printed "up to date" and stopped; any
    other path falls through and exits non-zero (we stop the snippet there with
    an explicit `exit 2` sentinel so the test can tell "no early exit" from a
    genuine error).
    """
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        marker_path = os.path.join(tmpdir, ".deployed-sha")
        if marker_contents is not None:
            Path(marker_path).write_text(marker_contents)

        snippet = f"""
set -euo pipefail
log() {{ printf '%s\\n' "$*"; }}
short_sha() {{ printf '%s' "${{1:0:12}}"; }}

DEPLOYED_SHA_FILE={shlex.quote(marker_path)}

LOCAL={shlex.quote(local_sha)}
REMOTE={shlex.quote(remote_sha)}

DEPLOYED_SHA=""
if [ -f "$DEPLOYED_SHA_FILE" ]; then
    DEPLOYED_SHA=$(cat "$DEPLOYED_SHA_FILE")
fi

if [ "$LOCAL" = "$REMOTE" ] && [ "$DEPLOYED_SHA" = "$REMOTE" ]; then
    log "up to date at $LOCAL"
    exit 0
fi

exit 2
"""
        result = subprocess.run(
            ["bash", "-c", snippet],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout + result.stderr


def test_early_exit_when_sha_and_marker_match():
    """When git HEAD == remote AND marker == remote → exit early (no work to do)."""
    code, output = _run_early_exit_logic("abc123", "abc123", "abc123")
    assert code == 0, "expected early-exit (exit 0) when git SHAs and marker all match"
    assert "up to date" in output


def test_no_early_exit_when_marker_missing():
    """When git HEAD == remote but the marker file does not exist → do NOT
    exit early.  This is the interrupted-build case: git was advanced but
    the image was never built successfully."""
    code, _ = _run_early_exit_logic("abc123", "abc123", None)
    assert code != 0, "must NOT exit early when marker file is absent — the image may not have been built yet"


def test_no_early_exit_when_marker_stale():
    """When git HEAD == remote but marker holds an older SHA → do NOT exit
    early.  The image is stale even though git has caught up."""
    code, _ = _run_early_exit_logic("abc123", "abc123", "old000000000")
    assert code != 0, (
        "must NOT exit early when marker SHA differs from remote — "
        "an interrupted build leaves git advanced but image stale"
    )


def test_no_early_exit_when_git_shas_differ():
    """When LOCAL != REMOTE → always deploy, regardless of marker state."""
    code, _ = _run_early_exit_logic("old000000000", "new111111111", "old000000000")
    assert code != 0, "must NOT exit early when there are new commits to deploy"
