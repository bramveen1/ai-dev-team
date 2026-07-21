"""Unit tests for router.epic.loop — Stage 1 orchestrator tick (#755).

Focus areas:
- Master flag gate: EPIC_ORCHESTRATOR off is a hard no-op (no DAG build).
- Not-configured / token-error gates.
- DAG-ordered dispatch: held children (open parent PR) are never dispatched
  and are logged with reason ``parent_pr_open``; ready children are.
- A child already carrying a pending draft is not re-dispatched.
- A landed PR gets the ``epic:<slug>`` label applied exactly once.
- A terminal child (merged closing PR / closed issue) is never (re-)dispatched.
- register_epic_orchestrator: idempotent registration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

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
        "destination": "C_TEST",
    }


def _settings_get(flag_on: bool):
    def _get(key):
        if key == "EPIC_ORCHESTRATOR":
            return flag_on
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
            patch("router.epic.loop.build_dag", return_value=dag),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
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
            patch("router.epic.loop.build_dag", return_value=dag),
            patch("router.epic.loop.ready_nodes", return_value=[101, 102]),
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

    async def test_cyclic_epic_holds_everything_and_notifies(self, slack_client, now, base_payload):
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", side_effect=DagCycleError([101, 102, 101])),
        ):
            result = await tick(payload=base_payload, slack_client=slack_client, now=now)
        assert result == {"status": "ok", "dispatched": 0, "held": 0}
        slack_client.chat_postMessage.assert_awaited_once()
        assert "cycle" in slack_client.chat_postMessage.await_args.kwargs["text"]

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

    async def test_merged_closing_pr_blocks_redispatch(self, slack_client, now, base_payload):
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", return_value={101: []}),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
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
        slack_client.chat_postMessage.assert_not_awaited()
        assert _read_dispatched(base_payload["state_path"]) == {}

    async def test_closed_issue_blocks_redispatch(self, slack_client, now, base_payload):
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", return_value={101: []}),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
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
        slack_client.chat_postMessage.assert_not_awaited()
        assert _read_dispatched(base_payload["state_path"]) == {}

    async def test_terminal_check_token_error_skips_dispatch_without_raising(self, slack_client, now, base_payload):
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", return_value={101: []}),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
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
            patch("router.epic.loop.build_dag", return_value={101: []}),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
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
            patch("router.epic.loop.build_dag", return_value={101: []}),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
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

    async def test_empty_kickoff_ts_skips_dispatch(self, slack_client, now, base_payload):
        slack_client.chat_postMessage = AsyncMock(return_value={})  # no ts
        create_fn = AsyncMock()
        with (
            patch("router.epic.loop.settings.get", side_effect=_settings_get(True)),
            patch("router.epic.loop.build_dag", return_value={101: []}),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
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
            patch("router.epic.loop.build_dag", return_value={101: []}),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
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
            patch("router.epic.loop.build_dag", return_value={101: []}),
            patch("router.epic.loop.ready_nodes", return_value=[101]),
            patch("router.epic.loop._get_open_pr_for_issue", new=AsyncMock(return_value=pr)),
            patch("router.epic.loop._apply_epic_label", new=apply_label),
        ):
            await tick(payload=base_payload, slack_client=slack_client, now=now)
        apply_label.assert_not_awaited()


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
