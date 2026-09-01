"""Unit tests for router.epic.loop — Stage 1 orchestrator tick (#755) and
Stage 2 auto-dispatch (#756).

Focus areas:
- Master flag gate: EPIC_ORCHESTRATOR off is a hard no-op (no DAG build).
- Not-configured / token-error gates.
- DAG-ordered dispatch: held children (open parent PR) are never dispatched
  and are logged with reason ``parent_pr_open``; ready children are.
- A child already carrying a pending draft is not re-dispatched.
- A landed PR gets the ``epic:<slug>`` label applied exactly once.
- A terminal child (merged closing PR / closed issue) is never (re-)dispatched.
- register_epic_orchestrator: idempotent registration.
- Stage 2 (EPIC_AUTO_DISPATCH): ready children are auto-dispatched with no
  approval card, gated by the shared daily/hourly caps; the handler's own
  smart-gate fallback still posts a card.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from router.epic.config import EPIC_DISPATCH_MAX_AGE_SECONDS
from router.epic.dag import DagCycleError, DagError
from router.epic.github import TokenError
from router.epic.loop import register_epic_orchestrator, tick
from router.epic.state import _mark_dispatched, _read_dispatched

pytestmark = pytest.mark.unit


@pytest.fixture
def now():
    return datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def slack_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "1111111111.000001"})
    return client


@pytest.fixture
def status_adapter():
    adapter = MagicMock()
    adapter.send_message = AsyncMock()
    return adapter


@pytest.fixture(autouse=True)
def _epic_status_chat_adapter(monkeypatch, status_adapter):
    """#861: EPIC_STATUS_VIA_CHAT_ADAPTER now defaults on and _post_status's
    raw-Slack fallback is gone, so every tick in this file needs a
    resolvable ChatAdapter for the orchestrator's own status posts (kickoff
    card, DAG-cycle warning, deploy-posture notice, shadow-dispatch notice)
    to succeed — this stands in for what `slack_client` used to cover."""
    monkeypatch.setattr("router.epic.loop.config.resolve_default_agent", lambda: "sam")
    monkeypatch.setattr("router.epic.loop.runtime.discord_adapter_for_agent", lambda agent: status_adapter)
    return status_adapter


@pytest.fixture
def pat_file(tmp_path):
    p = tmp_path / "pat.token"
    p.write_text("gh_test_token")
    return str(p)


@pytest.fixture
def epic_config_file(tmp_path):
    def _write(repo="o/r", epics=None):
        p = tmp_path / "epic.yaml"
        p.write_text(
            yaml.dump(
                {
                    "epic_orchestrator": {
                        "repo": repo,
                        "epics": epics if epics is not None else [{"number": 751, "slug": "auto-feature-orchestrator"}],
                    }
                }
            )
        )
        return str(p)

    return _write


@pytest.fixture
def base_payload(pat_file, tmp_path, epic_config_file):
    return {
        "config_path": epic_config_file(),
        "pat_path": pat_file,
        "state_path": str(tmp_path / "state.json"),
        "counter_path": str(tmp_path / "counters.json"),
        "destination": "C_TEST",
    }


def _settings_get(
    flag_on: bool,
    *,
    auto_dispatch: bool = False,
    shadow: bool = False,
    auto_merge: bool = False,
    auto_deploy: bool = False,
):
    def _get(key):
        if key == "EPIC_ORCHESTRATOR":
            return flag_on
        if key == "EPIC_AUTO_DISPATCH":
            return auto_dispatch
        if key == "EPIC_SHADOW_MODE":
            return shadow
        if key == "EPIC_AUTO_MERGE":
            return auto_merge
        if key == "EPIC_AUTO_DEPLOY":
            return auto_deploy
        if key == "EPIC_STATUS_VIA_CHAT_ADAPTER":
            return True
        if key == "EPIC_STATUS_TRANSPORT":
            return "discord"
        if key == "EPIC_STATUS_CONVERSATION_REF":
            return "discord:1:2:3"
        return None

    return _get


def _issue(number, title="A sub-issue"):
    return {"number": number, "title": title, "html_url": f"https://github.com/o/r/issues/{number}"}


@pytest.mark.asyncio
class TestTickGates:
    async def test_disabled_returns_ok_skipped_and_never_builds_dag(self, slack_client, now, base_payload):
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(False)),
            patch("router.epic.loop.build_dag") as build_dag_mock,
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result == {"status": "ok", "skipped": "disabled"}
        build_dag_mock.assert_not_called()

    async def test_no_repo_returns_not_configured(self, slack_client, now, base_payload, epic_config_file):
        payload = {**base_payload, "config_path": epic_config_file(repo="")}
        with patch("router.epic.loop.settings.get", side_effect=_settings_get(True)):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result == {"status": "ok", "skipped": "not_configured"}

    async def test_no_epics_returns_not_configured(self, slack_client, now, base_payload, epic_config_file):
        payload = {**base_payload, "config_path": epic_config_file(epics=[])}
        with patch("router.epic.loop.settings.get", side_effect=_settings_get(True)):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result == {"status": "ok", "skipped": "not_configured"}

    async def test_missing_pat_returns_token_error(self, slack_client, now, base_payload, tmp_path):
        payload = {**base_payload, "pat_path": str(tmp_path / "does_not_exist.token")}
        with patch("router.epic.loop.settings.get", side_effect=_settings_get(True)):
            result = await tick(payload=payload, slack_client=slack_client, now=now)
        assert result == {"status": "ok", "skipped": "token_error"}


@pytest.mark.asyncio
class TestDagOrderedDispatch:
    """A child is never dispatched while a parent PR is open (#754's ready_nodes
    gate) and held children are logged with reason=parent_pr_open."""

    async def test_held_child_is_logged_and_not_dispatched(self, slack_client, now, base_payload, caplog):
        dag = {101: [], 102: [101]}  # 102 depends on 101, still open.
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value=dag)),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_issue", new=AsyncMock(side_effect=lambda repo, n, pat: _issue(n))),
            caplog.at_level(logging.INFO, logger="router.epic.loop"),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 1
        assert result["held"] == 1
        create_fn.assert_awaited_once()
        assert create_fn.await_args.kwargs["issue_num"] == 101
        assert any("held reason=parent_pr_open" in r.message and "#102" in r.message for r in caplog.records)

    async def test_child_dispatched_once_parent_merges(self, slack_client, now, base_payload):
        dag = {101: [], 102: [101]}
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value=dag)),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101, 102])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_issue", new=AsyncMock(side_effect=lambda repo, n, pat: _issue(n))),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 2
        assert result["held"] == 0
        dispatched_issue_nums = [c.kwargs["issue_num"] for c in create_fn.await_args_list]
        assert dispatched_issue_nums == [101, 102]  # ascending / DAG order

    async def test_cyclic_epic_holds_everything_and_notifies(self, slack_client, now, base_payload, status_adapter):
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", side_effect=DagCycleError([101, 102, 101])),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result == {"status": "ok", "dispatched": 0, "held": 0}
        status_adapter.send_message.assert_awaited_once()
        assert "cycle" in status_adapter.send_message.await_args.args[0].text

    async def test_dag_error_skips_epic_without_raising(self, slack_client, now, base_payload):
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", side_effect=DagError("boom")),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result == {"status": "ok", "dispatched": 0, "held": 0}


@pytest.mark.asyncio
class TestTerminalChildNeverReDispatched:
    """#768: a child with no open PR is only dispatched if it's not already
    terminal (merged closing PR / closed issue) — otherwise the tracker-clear
    in `_reconcile_landed_pr` would reopen the dispatch window forever."""

    async def test_merged_closing_pr_blocks_redispatch(self, slack_client, now, base_payload, status_adapter):
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=True)),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 0
        create_fn.assert_not_awaited()
        status_adapter.send_message.assert_not_awaited()
        assert _read_dispatched(base_payload["state_path"]) == {}

    async def test_closed_issue_blocks_redispatch(self, slack_client, now, base_payload, status_adapter):
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=True)),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 0
        create_fn.assert_not_awaited()
        status_adapter.send_message.assert_not_awaited()
        assert _read_dispatched(base_payload["state_path"]) == {}

    async def test_terminal_check_token_error_skips_dispatch_without_raising(self, slack_client, now, base_payload):
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(side_effect=TokenError("boom"))),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 0
        create_fn.assert_not_awaited()

    async def test_open_pr_reconcile_path_unaffected_by_terminal_check(self, slack_client, now, base_payload):
        """An open-PR child still labels + reconciles only — no terminal-state call needed."""
        pr = {"number": 55, "labels": []}
        apply_label = AsyncMock(return_value=True)
        is_terminal = AsyncMock(return_value=False)
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=pr)),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
            patch("router.epic.loop._is_child_terminal", new=is_terminal),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result["dispatched"] == 0
        apply_label.assert_awaited_once()
        is_terminal.assert_not_awaited()


@pytest.mark.asyncio
class TestDispatchDedup:
    async def test_already_dispatched_child_is_not_re_dispatched(self, slack_client, now, base_payload):
        _mark_dispatched(base_payload["state_path"], 101, "auto-feature-orchestrator", now.timestamp())
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 0
        create_fn.assert_not_awaited()
        # Companion assertion (#854 acceptance criteria): a <60-min entry is
        # left untouched — no race with a still-running 30-min worker.
        assert "101" in _read_dispatched(base_payload["state_path"])

    async def test_epic_tracker_ages_out_crashed_worker(self, slack_client, now, base_payload):
        """#854: a tracker entry with no landed PR whose dispatch timestamp is
        > 60 minutes old (a worker that crashed before opening a PR) is
        dropped on the next tick, so the slice becomes re-dispatchable."""
        stale_ts = now.timestamp() - EPIC_DISPATCH_MAX_AGE_SECONDS - 1
        _mark_dispatched(base_payload["state_path"], 101, "auto-feature-orchestrator", stale_ts)
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_issue", new=AsyncMock(return_value=_issue(101))),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 1
        create_fn.assert_awaited_once()
        assert create_fn.await_args.kwargs["issue_num"] == 101
        # Re-marked with a fresh timestamp by the re-dispatch itself, not left
        # as the stale tombstone.
        assert _read_dispatched(base_payload["state_path"])["101"]["ts"] == now.timestamp()

    async def test_empty_kickoff_ts_skips_dispatch(self, slack_client, now, base_payload, status_adapter):
        status_adapter.send_message = AsyncMock(side_effect=RuntimeError("boom"))  # adapter post fails, no ref
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_issue", new=AsyncMock(return_value=_issue(101))),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 0
        create_fn.assert_not_awaited()


@pytest.mark.asyncio
class TestEpicLabelReconciliation:
    async def test_landed_pr_without_label_gets_labelled(self, slack_client, now, base_payload):
        pr = {"number": 55, "labels": []}
        apply_label = AsyncMock(return_value=True)
        _mark_dispatched(base_payload["state_path"], 101, "auto-feature-orchestrator", now.timestamp())
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=pr)),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result["dispatched"] == 0  # labelling, not dispatching
        apply_label.assert_awaited_once_with("o/r", 55, "epic:auto-feature-orchestrator", "gh_test_token")
        # Tracker entry cleared once the PR has landed.
        assert "101" not in _read_dispatched(base_payload["state_path"])

    async def test_landed_pr_already_labelled_is_not_relabelled(self, slack_client, now, base_payload):
        pr = {"number": 55, "labels": [{"name": "epic:auto-feature-orchestrator"}]}
        apply_label = AsyncMock(return_value=True)
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=pr)),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        apply_label.assert_not_awaited()


@pytest.mark.asyncio
class TestEpicAutoMergeGate:
    """Stage 3 (#757): `epic-auto-merge` is applied to a landed epic PR only
    when EPIC_AUTO_MERGE is on and the PR is reviewed + green + DAG-satisfied,
    honouring EPIC_SHADOW_MODE as its own dry-run gate (mirrors Stage 2/#773).
    Review/CI/parent probes are the shared helpers this gate must reuse, not
    duplicate: `router.merge_queue._has_approving_review`, `._get_pr_details`,
    and `router.epic.dag._parent_merged`.
    """

    @staticmethod
    def _landed_pr(labelled: bool = True):
        labels = [{"name": "epic:auto-feature-orchestrator"}] if labelled else []
        return {"number": 55, "labels": labels}

    async def test_flag_off_never_applies_epic_auto_merge_label(self, slack_client, now, base_payload):
        """Flag off → `_apply_epic_label` is called only for `epic:<slug>`, never
        `epic-auto-merge`, even when reviewed/green/DAG-satisfied would otherwise pass."""
        apply_label = AsyncMock(return_value=True)
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_merge=False)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch(
                "router.epic.loop._get_open_pr_for_issue",
                new=AsyncMock(return_value=self._landed_pr(labelled=False)),
            ),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
            patch("router.epic.loop._has_approving_review", new=AsyncMock(return_value=True)),
            patch("router.epic.loop._get_pr_details", new=AsyncMock(return_value={"mergeable_state": "clean"})),
            patch("router.epic.loop._parent_merged", new=AsyncMock(return_value=True)),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        apply_label.assert_awaited_once_with("o/r", 55, "epic:auto-feature-orchestrator", "gh_test_token")

    async def test_flag_on_reviewed_green_dag_satisfied_applies_label_once(self, slack_client, now, base_payload):
        apply_label = AsyncMock(return_value=True)
        parent_merged = AsyncMock(return_value=True)
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_merge=True, shadow=False)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: [99]})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=self._landed_pr())),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
            patch("router.epic.loop._has_approving_review", new=AsyncMock(return_value=True)),
            patch("router.epic.loop._get_pr_details", new=AsyncMock(return_value={"mergeable_state": "clean"})),
            patch("router.epic.loop._parent_merged", new=parent_merged),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        apply_label.assert_awaited_once_with("o/r", 55, "epic-auto-merge", "gh_test_token")
        parent_merged.assert_awaited_once_with("o/r", 99, "gh_test_token", base_branch="main")

    async def test_flag_on_shadow_on_logs_and_does_not_apply_label(self, slack_client, now, base_payload, caplog):
        apply_label = AsyncMock(return_value=True)
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_merge=True, shadow=True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=self._landed_pr())),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
            patch("router.epic.loop._has_approving_review", new=AsyncMock(return_value=True)),
            patch("router.epic.loop._get_pr_details", new=AsyncMock(return_value={"mergeable_state": "clean"})),
            patch("router.epic.loop._parent_merged", new=AsyncMock(return_value=True)),
            caplog.at_level(logging.INFO, logger="router.epic.loop"),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        apply_label.assert_not_awaited()
        assert any("would apply epic-auto-merge to PR #55" in r.message for r in caplog.records)

    async def test_flag_on_unreviewed_does_not_apply_label(self, slack_client, now, base_payload):
        apply_label = AsyncMock(return_value=True)
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_merge=True, shadow=False)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=self._landed_pr())),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
            patch("router.epic.loop._has_approving_review", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_pr_details", new=AsyncMock(return_value={"mergeable_state": "clean"})),
            patch("router.epic.loop._parent_merged", new=AsyncMock(return_value=True)),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        apply_label.assert_not_awaited()

    async def test_flag_on_red_ci_does_not_apply_label(self, slack_client, now, base_payload):
        apply_label = AsyncMock(return_value=True)
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_merge=True, shadow=False)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=self._landed_pr())),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
            patch("router.epic.loop._has_approving_review", new=AsyncMock(return_value=True)),
            patch("router.epic.loop._get_pr_details", new=AsyncMock(return_value={"mergeable_state": "dirty"})),
            patch("router.epic.loop._parent_merged", new=AsyncMock(return_value=True)),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        apply_label.assert_not_awaited()

    async def test_flag_on_unmerged_parent_does_not_apply_label(self, slack_client, now, base_payload):
        apply_label = AsyncMock(return_value=True)
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_merge=True, shadow=False)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: [99]})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=self._landed_pr())),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
            patch("router.epic.loop._has_approving_review", new=AsyncMock(return_value=True)),
            patch("router.epic.loop._get_pr_details", new=AsyncMock(return_value={"mergeable_state": "clean"})),
            patch("router.epic.loop._parent_merged", new=AsyncMock(return_value=False)),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        apply_label.assert_not_awaited()


@pytest.mark.asyncio
class TestDeployPostureStage4:
    """Stage 4 (#758): EPIC_AUTO_DEPLOY gates only the notification posted once
    `epic-auto-merge` actually lands — merge/deploy mechanics (merge_queue,
    the pull daemon's health-check + auto-revert) are untouched either way."""

    @staticmethod
    def _landed_pr():
        return {"number": 55, "labels": []}

    async def _run_tick(self, *, slack_client, now, base_payload, auto_deploy):
        with (
            patch(
                "router.epic.loop.settings.get",
                side_effect=_settings_get(True, auto_merge=True, shadow=False, auto_deploy=auto_deploy),
            ),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=self._landed_pr())),
            patch("router.epic.loop._apply_epic_label", new=AsyncMock(return_value=True)),
            patch("router.epic.loop._has_approving_review", new=AsyncMock(return_value=True)),
            patch("router.epic.loop._get_pr_details", new=AsyncMock(return_value={"mergeable_state": "clean"})),
            patch("router.epic.loop._parent_merged", new=AsyncMock(return_value=True)),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)

    async def test_flag_off_notification_asks_bram_to_approve_deploy(
        self, slack_client, now, base_payload, status_adapter
    ):
        await self._run_tick(slack_client=slack_client, now=now, base_payload=base_payload, auto_deploy=False)
        posted = [call.args[0].text for call in status_adapter.send_message.await_args_list]
        assert any("EPIC_AUTO_DEPLOY is off" in text and "approve" in text for text in posted)

    async def test_flag_on_notification_is_monitor_only(self, slack_client, now, base_payload, status_adapter):
        await self._run_tick(slack_client=slack_client, now=now, base_payload=base_payload, auto_deploy=True)
        posted = [call.args[0].text for call in status_adapter.send_message.await_args_list]
        assert any("monitor-only" in text for text in posted)

    async def test_no_notification_when_label_not_applied(self, slack_client, now, base_payload, status_adapter):
        """Unreviewed PR never reaches the label step, so no deploy-posture
        notification should be posted either."""
        with (
            patch(
                "router.epic.loop.settings.get",
                side_effect=_settings_get(True, auto_merge=True, shadow=False, auto_deploy=False),
            ),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=self._landed_pr())),
            patch("router.epic.loop._apply_epic_label", new=AsyncMock(return_value=True)),
            patch("router.epic.loop._has_approving_review", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_pr_details", new=AsyncMock(return_value={"mergeable_state": "clean"})),
            patch("router.epic.loop._parent_merged", new=AsyncMock(return_value=True)),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        status_adapter.send_message.assert_not_awaited()


@pytest.mark.asyncio
class TestAutoDispatchStage2:
    """#756: EPIC_AUTO_DISPATCH on → ready children launch straight to the
    worker (no approval card), gated by the bug loop's shared 12/day + 1/hr
    caps. Flag off is covered by every other test class in this file (they
    all use the default ``auto_dispatch=False``)."""

    async def test_ready_child_auto_dispatched_no_card_posted(self, slack_client, now, base_payload):
        create_fn = AsyncMock()
        dispatch_worker = AsyncMock(return_value="launched")
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_dispatch=True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_issue", new=AsyncMock(side_effect=lambda repo, n, pat: _issue(n))),
            patch("router.epic.loop._dispatch_worker", new=dispatch_worker),
            patch("router.epic.loop.get_counters", return_value={"daily_count": 0, "hourly_count": 0}),
            patch("router.epic.loop.increment_counters") as increment_mock,
            patch(
                "router.epic.loop.load_auto_dispatch_config",
                return_value={"rate_per_hour": 1, "daily_cap": 12, "enabled": True, "shadow_mode": False},
            ),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 1
        create_fn.assert_not_awaited()  # no approval card — Stage 2 skips it
        dispatch_worker.assert_awaited_once()
        assert dispatch_worker.await_args.kwargs["issue_num"] == 101
        increment_mock.assert_called_once()
        assert _read_dispatched(base_payload["state_path"])["101"]["slug"] == "auto-feature-orchestrator"

    async def test_shadow_mode_logs_would_dispatch_and_does_not_launch(
        self, slack_client, now, base_payload, status_adapter
    ):
        """#773: EPIC_SHADOW_MODE on (the default) — a dispatch-eligible sub-issue
        is logged as would-dispatch; no worker is spawned and no counter moves."""
        dispatch_worker = AsyncMock(return_value="launched")
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_dispatch=True, shadow=True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_issue", new=AsyncMock(side_effect=lambda repo, n, pat: _issue(n))),
            patch("router.epic.loop._dispatch_worker", new=dispatch_worker),
            patch("router.epic.loop.get_counters", return_value={"daily_count": 0, "hourly_count": 0}),
            patch("router.epic.loop.increment_counters") as increment_mock,
            patch(
                "router.epic.loop.load_auto_dispatch_config",
                return_value={"rate_per_hour": 1, "daily_cap": 12, "enabled": True, "shadow_mode": False},
            ),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result["dispatched"] == 1
        dispatch_worker.assert_not_awaited()
        increment_mock.assert_not_called()
        assert _read_dispatched(base_payload["state_path"]) == {}
        posted = [call.args[0].text for call in status_adapter.send_message.await_args_list]
        assert any("would auto-dispatch" in msg and "#101" in msg for msg in posted)

    async def test_auto_dispatch_enabled_false_holds_without_dispatching(self, slack_client, now, base_payload):
        """#773: the shared auto_dispatch.enabled kill switch also gates the epic
        lane — off means hold, same as the bug loop, even with EPIC_AUTO_DISPATCH on."""
        dispatch_worker = AsyncMock(return_value="launched")
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_dispatch=True, shadow=False)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._dispatch_worker", new=dispatch_worker),
            patch(
                "router.epic.loop.load_auto_dispatch_config",
                return_value={"rate_per_hour": 1, "daily_cap": 12, "enabled": False, "shadow_mode": False},
            ),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result["dispatched"] == 0
        dispatch_worker.assert_not_awaited()
        assert _read_dispatched(base_payload["state_path"]) == {}

    async def test_daily_cap_reached_holds_without_dispatching(self, slack_client, now, base_payload):
        dispatch_worker = AsyncMock(return_value="launched")
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_dispatch=True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._dispatch_worker", new=dispatch_worker),
            patch("router.epic.loop.get_counters", return_value={"daily_count": 12, "hourly_count": 0}),
            patch(
                "router.epic.loop.load_auto_dispatch_config",
                return_value={"rate_per_hour": 1, "daily_cap": 12, "enabled": True, "shadow_mode": False},
            ),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result["dispatched"] == 0
        dispatch_worker.assert_not_awaited()
        assert _read_dispatched(base_payload["state_path"]) == {}

    async def test_hourly_rate_reached_holds_without_dispatching(self, slack_client, now, base_payload):
        dispatch_worker = AsyncMock(return_value="launched")
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_dispatch=True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._dispatch_worker", new=dispatch_worker),
            patch("router.epic.loop.get_counters", return_value={"daily_count": 0, "hourly_count": 1}),
            patch(
                "router.epic.loop.load_auto_dispatch_config",
                return_value={"rate_per_hour": 1, "daily_cap": 12, "enabled": True, "shadow_mode": False},
            ),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result["dispatched"] == 0
        dispatch_worker.assert_not_awaited()

    async def test_handler_gate_fallback_still_posts_card_and_skips_counter(self, slack_client, now, base_payload):
        """The handler's own smart gate (deny-keyword / cost-threshold) can still
        fire under Stage 2 — `_dispatch_worker` posts a card in that case rather
        than raising. Counters must NOT be incremented (no worker was spawned)."""
        create_fn = AsyncMock()
        dispatch_worker = AsyncMock(return_value="approval_required")
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_dispatch=True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_issue", new=AsyncMock(side_effect=lambda repo, n, pat: _issue(n))),
            patch("router.epic.loop._dispatch_worker", new=dispatch_worker),
            patch("router.epic.loop.get_counters", return_value={"daily_count": 0, "hourly_count": 0}),
            patch("router.epic.loop.increment_counters") as increment_mock,
            patch(
                "router.epic.loop.load_auto_dispatch_config",
                return_value={"rate_per_hour": 1, "daily_cap": 12, "enabled": True, "shadow_mode": False},
            ),
        ):
            result = await tick(
                payload=base_payload,
                slack_client=slack_client,
                now=now,
                _create_draft_fn=create_fn,
            )
        assert result["dispatched"] == 1
        increment_mock.assert_not_called()
        assert "101" in _read_dispatched(base_payload["state_path"])

    async def test_dispatch_worker_error_holds_without_marking_dispatched(self, slack_client, now, base_payload):
        dispatch_worker = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True, auto_dispatch=True)),
            patch("router.epic.loop.build_dag", new=AsyncMock(return_value={101: []})),
            patch("router.epic.loop.ready_nodes", new=AsyncMock(return_value=[101])),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=None)),
            patch("router.epic.loop._is_child_terminal", new=AsyncMock(return_value=False)),
            patch("router.epic.loop._get_issue", new=AsyncMock(side_effect=lambda repo, n, pat: _issue(n))),
            patch("router.epic.loop._dispatch_worker", new=dispatch_worker),
            patch("router.epic.loop.get_counters", return_value={"daily_count": 0, "hourly_count": 0}),
            patch(
                "router.epic.loop.load_auto_dispatch_config",
                return_value={"rate_per_hour": 1, "daily_cap": 12, "enabled": True, "shadow_mode": False},
            ),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result["dispatched"] == 0
        assert _read_dispatched(base_payload["state_path"]) == {}


@pytest.fixture
def task_store(tmp_path):
    from router.scheduled_tasks.store import ScheduledTaskStore

    return ScheduledTaskStore(str(tmp_path / "tasks.db"))


class TestRegisterEpicOrchestrator:
    def test_registers_new_task_with_tick_ref(self, task_store):
        from router import epic

        task = register_epic_orchestrator(task_store, agent_name="sam")
        assert task.callable_ref == epic.CALLABLE_REF == "router.epic:tick"
        assert task.period_seconds == epic.DEFAULT_PERIOD_SECONDS

    def test_idempotent_on_second_call(self, task_store):
        from router import epic

        register_epic_orchestrator(task_store, agent_name="sam")
        register_epic_orchestrator(task_store, agent_name="sam")
        assert len(task_store.list_by_callable_ref(epic.CALLABLE_REF)) == 1

    def test_destination_threaded_into_payload(self, task_store):
        task = register_epic_orchestrator(task_store, agent_name="sam", destination="C_BRAM")
        assert task.payload["destination"] == "C_BRAM"
