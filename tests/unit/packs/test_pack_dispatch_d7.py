"""Unit tests for D-7 approval gating (issue #203).

Covers:
- require_always: true  → dispatch_issue returns approval_required (no subprocess,
  no workspace state written).
- require_always: false + benign (sonnet, no destructive keywords, low cost) → runs directly.
- require_always: false + model=opus + destructive keyword in issue text → gates.
- require_always: false + 5h window cost ≥ $15 → gates regardless of model/keywords.
- DISPATCH_APPROVAL_COST_USD env override — gate fires at the new threshold.
- Unparseable DISPATCH_APPROVAL_COST_USD → warns, falls back to $15 default.
- Approval card preview contains gate_reason; cost_threshold includes cost/threshold numbers.
- _approved=True → bypass gate, dispatch proceeds normally (gate_bypass_via_approval logged).
- Regression: dispatch_cancel, dispatch_status, dispatch_health ignore require_always + _approved.
- Malformed approval config → fail-closed (require_always=true).
- load_approval_config unit tests (absence, malformed types, valid config).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _no_op_seed_auth(workspace: Path) -> Path:
    d = workspace / "auth"
    d.mkdir(exist_ok=True)
    return d


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_d7_handler",
        PACK_DIR / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_quota():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location("_test_d7_quota", PACK_DIR / "quota.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    return _load_handler()


@pytest.fixture
def quota():
    return _load_quota()


class _FakePopen:
    instances: list["_FakePopen"] = []
    wait_returncode = 0

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.pid = 99999
        self.cwd = kwargs.get("cwd")
        _FakePopen.instances.append(self)

    def wait(self):
        if self.cwd:
            (Path(self.cwd) / "exitcode").write_text("0")
        return 0

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.wait_returncode = 0


# ── Positive: require_always gating ─────────────────────────────────────────


class TestRequireAlwaysGating:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_require_always_true_returns_approval_required(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={"require_always": True, "destructive_keywords": []},
        )

        assert result["status"] == "approval_required"
        assert "draft_id" in result
        assert len(result["draft_id"]) == 8  # short hex uuid
        preview = result["preview"]
        assert preview["gate_reason"] == "always"
        assert preview["issue_url"] == "https://github.com/bramveen1/ai-dev-team/issues/42"
        assert preview["model"] == "sonnet"
        assert "repo" in preview
        assert "est_workspace_path" in preview

    def test_require_always_no_subprocess_spawned(self, handler, tmp_path: Path) -> None:
        handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={"require_always": True, "destructive_keywords": []},
        )
        assert len(_FakePopen.instances) == 0

    def test_require_always_no_workspace_state_written(self, handler, tmp_path: Path) -> None:
        handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={"require_always": True, "destructive_keywords": []},
        )
        # No dispatch workspace dirs should have been created.
        dispatch_dirs = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("dispatch-")]
        assert dispatch_dirs == [], f"workspace dirs created: {dispatch_dirs}"

    def test_require_always_false_benign_runs_directly(self, handler, tmp_path: Path) -> None:
        """Benign: sonnet model, no destructive keywords, cost below threshold."""
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg={
                "require_always": False,
                "destructive_keywords": ["destructive", "delete", "drop", "migration", "reset"],
            },
            _fetch_issue_fn=lambda url: "Fix a typo in the README",
        )

        assert result["status"] == "launched"
        assert len(_FakePopen.instances) == 1


# ── Positive: smart-gate — destructive keyword ───────────────────────────────


class TestDestructiveKeywordGating:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_opus_plus_destructive_keyword_gates(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/99",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="opus",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={
                "require_always": False,
                "destructive_keywords": ["destructive", "delete", "drop", "migration", "reset"],
            },
            _fetch_issue_fn=lambda url: "Add a migration for the users table",
        )

        assert result["status"] == "approval_required"
        assert result["preview"]["gate_reason"] == "destructive_keyword"
        assert result["preview"]["matched_keyword"] == "migration"
        assert len(_FakePopen.instances) == 0

    def test_sonnet_with_destructive_keyword_does_not_gate(self, handler, tmp_path: Path) -> None:
        """The destructive-keyword check only fires when model=opus."""
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/99",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg={
                "require_always": False,
                "destructive_keywords": ["migration"],
            },
            _fetch_issue_fn=lambda url: "Add a migration for the users table",
        )

        assert result["status"] == "launched"

    def test_opus_without_keyword_does_not_gate(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/99",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="opus",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg={
                "require_always": False,
                "destructive_keywords": ["migration"],
            },
            _fetch_issue_fn=lambda url: "Refactor the auth module",
        )

        assert result["status"] == "launched"

    def test_keyword_match_is_case_insensitive(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/99",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="opus",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={
                "require_always": False,
                "destructive_keywords": ["DROP"],
            },
            _fetch_issue_fn=lambda url: "drop the legacy table",
        )

        assert result["status"] == "approval_required"
        assert result["preview"]["gate_reason"] == "destructive_keyword"


# ── Positive: smart-gate — cost threshold ───────────────────────────────────


def _make_cost_dispatch(root: Path, *, cost_usd: float) -> None:
    """Write a minimal dispatch dir with the given cost, started 1h ago."""
    from datetime import timedelta

    dispatch_id = "dispatch-20260519T000000-aabbcc"
    d = root / dispatch_id
    d.mkdir(exist_ok=True)
    (d / "started_at").write_text((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    (d / "cost").write_text(str(cost_usd))


class TestCostThresholdGating:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_cost_at_threshold_gates(self, handler, tmp_path: Path) -> None:
        _make_cost_dispatch(tmp_path, cost_usd=15.0)

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/5",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _fetch_issue_fn=lambda url: "",
        )

        assert result["status"] == "approval_required"
        assert result["preview"]["gate_reason"] == "cost_threshold"
        assert result["preview"]["current_window_cost_usd"] == 15.0
        assert result["preview"]["threshold_usd"] == 15.0

    def test_cost_below_threshold_does_not_gate(self, handler, tmp_path: Path) -> None:
        _make_cost_dispatch(tmp_path, cost_usd=14.99)

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/5",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _fetch_issue_fn=lambda url: "",
        )

        assert result["status"] == "launched"

    def test_cost_threshold_env_override(self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setting DISPATCH_APPROVAL_COST_USD=25 → gate fires at $25, not $15."""
        monkeypatch.setenv(handler.DISPATCH_APPROVAL_COST_USD_ENV, "25")
        _make_cost_dispatch(tmp_path, cost_usd=20.0)  # $20 < $25 — should not gate

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/5",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _fetch_issue_fn=lambda url: "",
        )

        assert result["status"] == "launched"

    def test_cost_threshold_env_override_gates_at_new_value(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(handler.DISPATCH_APPROVAL_COST_USD_ENV, "25")
        _make_cost_dispatch(tmp_path, cost_usd=26.0)  # $26 ≥ $25 — should gate

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/5",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _fetch_issue_fn=lambda url: "",
        )

        assert result["status"] == "approval_required"
        assert result["preview"]["threshold_usd"] == 25.0

    def test_unparseable_cost_threshold_env_falls_back_to_default(
        self, handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unparseable env value → warn + fall back to $15 (fail-safe, not fail-closed)."""
        monkeypatch.setenv(handler.DISPATCH_APPROVAL_COST_USD_ENV, "abc")
        _make_cost_dispatch(tmp_path, cost_usd=15.0)  # $15 ≥ $15 default → gates

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/5",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _fetch_issue_fn=lambda url: "",
        )

        # Falls back to $15 default, so $15.0 cost should still gate.
        assert result["status"] == "approval_required"
        assert result["preview"]["threshold_usd"] == handler.DEFAULT_APPROVAL_COST_THRESHOLD_USD


# ── Positive: _approved bypass ────────────────────────────────────────────────


class TestApprovedBypass:
    def setup_method(self) -> None:
        _FakePopen.reset()

    def test_approved_bypasses_require_always(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approved=True,
            _approval_cfg={"require_always": True, "destructive_keywords": []},
        )

        assert result["status"] == "launched"
        assert len(_FakePopen.instances) == 1

    def test_approved_bypasses_destructive_keyword_gate(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="opus",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approved=True,
            _approval_cfg={
                "require_always": False,
                "destructive_keywords": ["migration"],
            },
            _fetch_issue_fn=lambda url: "Add a migration",
        )

        assert result["status"] == "launched"

    def test_approved_bypasses_cost_gate(self, handler, tmp_path: Path) -> None:
        _make_cost_dispatch(tmp_path, cost_usd=100.0)

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/42",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            workspace_root=tmp_path,
            popen=_FakePopen,
            supervision_mode="poll",
            _seed_auth_fn=_no_op_seed_auth,
            _approved=True,
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _fetch_issue_fn=lambda url: "",
        )

        assert result["status"] == "launched"

    def test_approved_flag_via_cli(
        self, handler, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        """--approved CLI flag is parsed and forwarded to dispatch_issue."""
        recorded: dict = {}

        def fake_dispatch(**kwargs):
            recorded.update(kwargs)
            return {"status": "launched", "dispatch_id": "d-1", "workspace": str(tmp_path), "pid": 1}

        monkeypatch.setattr(handler, "dispatch_issue", fake_dispatch)
        monkeypatch.setenv(handler.DISPATCH_CHANNEL_ENV, "C1")
        monkeypatch.setenv(handler.DISPATCH_THREAD_TS_ENV, "1.0")
        monkeypatch.setenv(handler.DISPATCH_AGENT_ENV, "sam")

        rc = handler.run(["dispatch_issue", "--issue-url", "https://github.com/o/r/issues/1", "--approved"])
        assert rc == 0
        assert recorded.get("_approved") is True


# ── Regression: carve-outs (dispatch_cancel, dispatch_status, dispatch_health) ──


class TestCarveOuts:
    """These verbs must never be affected by approval gating."""

    def test_dispatch_status_unaffected_by_require_always(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_status(workspace_root=tmp_path)
        assert "approval_required" not in str(result)
        assert "running" in result
        assert "queued" in result

    def test_dispatch_cancel_unaffected_by_require_always(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_cancel(dispatch_id="dispatch-ghost", workspace_root=tmp_path)
        assert result["status"] == "noop"
        assert result.get("reason") != "approval_required"

    def test_dispatch_health_unaffected_by_require_always(self, handler, tmp_path: Path) -> None:
        def _completed_mock():
            m = MagicMock()
            m.stdout = json.dumps({"is_error": False, "result": "hello"})
            m.stderr = ""
            m.returncode = 0
            return m

        result = handler.dispatch_health(
            which=lambda _: None,
            run=lambda *a, **k: _completed_mock(),
            workspace_root=tmp_path,
        )
        assert "approval_required" not in str(result)
        assert "sonnet_probe_ok" in result

    def test_dispatch_cancel_has_no_approved_parameter(self, handler) -> None:
        """dispatch_cancel must not accept or read _approved."""
        import inspect

        sig = inspect.signature(handler.dispatch_cancel)
        assert "_approved" not in sig.parameters, "_approved must not be in dispatch_cancel signature"

    def test_dispatch_status_has_no_approved_parameter(self, handler) -> None:
        import inspect

        sig = inspect.signature(handler.dispatch_status)
        assert "_approved" not in sig.parameters, "_approved must not be in dispatch_status signature"

    def test_dispatch_health_has_no_approved_parameter(self, handler) -> None:
        import inspect

        sig = inspect.signature(handler.dispatch_health)
        assert "_approved" not in sig.parameters, "_approved must not be in dispatch_health signature"


# ── load_approval_config unit tests ──────────────────────────────────────────
# Note: malformed-YAML fail-closed behavior is covered in TestLoadApprovalConfig
# below. A handler-level "missing require_always" test was removed because it
# can't exercise the gate without bypassing the auth-seed path, and its
# behavior is already pinned by load_approval_config()'s defaults.


class TestLoadApprovalConfig:
    def test_returns_defaults_when_file_missing(self, quota, tmp_path: Path) -> None:
        result = quota.load_approval_config(tmp_path / "nonexistent.yaml")
        assert result["require_always"] is True
        assert isinstance(result["destructive_keywords"], list)
        assert len(result["destructive_keywords"]) > 0

    def test_reads_valid_config(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("approval:\n  require_always: false\n  destructive_keywords:\n    - delete\n    - drop\n")
        result = quota.load_approval_config(cfg_file)
        assert result["require_always"] is False
        assert result["destructive_keywords"] == ["delete", "drop"]

    def test_require_always_true_parsed(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("approval:\n  require_always: true\n")
        result = quota.load_approval_config(cfg_file)
        assert result["require_always"] is True

    def test_absent_approval_block_returns_fail_closed_defaults(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("quota:\n  threshold_usd: 50\n")
        result = quota.load_approval_config(cfg_file)
        assert result["require_always"] is True

    def test_malformed_approval_not_dict_fails_closed(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("approval: just_a_string\n")
        result = quota.load_approval_config(cfg_file)
        assert result["require_always"] is True

    def test_require_always_not_bool_fails_closed(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("approval:\n  require_always: yes_please\n")
        result = quota.load_approval_config(cfg_file)
        assert result["require_always"] is True

    def test_destructive_keywords_not_list_fails_closed(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("approval:\n  require_always: false\n  destructive_keywords: not_a_list\n")
        result = quota.load_approval_config(cfg_file)
        assert result["require_always"] is True

    def test_destructive_keywords_non_string_entries_fail_closed(self, quota, tmp_path: Path) -> None:
        cfg_file = tmp_path / "dispatch.yaml"
        cfg_file.write_text("approval:\n  require_always: false\n  destructive_keywords:\n    - 123\n    - delete\n")
        result = quota.load_approval_config(cfg_file)
        assert result["require_always"] is True


# ── Preview payload shape ────────────────────────────────────────────────────


class TestApprovalPreviewShape:
    def test_always_gate_preview_fields(self, handler, tmp_path: Path) -> None:
        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/203",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={"require_always": True, "destructive_keywords": []},
        )
        preview = result["preview"]
        assert preview["gate_reason"] == "always"
        assert preview["repo"] == "bramveen1/ai-dev-team"
        assert "branch_target" in preview
        assert "model" in preview
        assert "est_workspace_path" in preview

    def test_cost_threshold_preview_includes_numbers(self, handler, tmp_path: Path) -> None:
        _make_cost_dispatch(tmp_path, cost_usd=16.0)

        result = handler.dispatch_issue(
            issue_url="https://github.com/bramveen1/ai-dev-team/issues/5",
            channel="C1",
            thread_ts="1.0",
            agent="sam",
            model="sonnet",
            workspace_root=tmp_path,
            popen=_FakePopen,
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _fetch_issue_fn=lambda url: "",
        )

        preview = result["preview"]
        assert preview["gate_reason"] == "cost_threshold"
        assert "current_window_cost_usd" in preview
        assert "threshold_usd" in preview
        assert isinstance(preview["current_window_cost_usd"], float)
        assert isinstance(preview["threshold_usd"], float)

    def test_approval_required_exits_zero_via_cli(
        self, handler, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        """approval_required is a non-error outcome from the CLI's perspective."""
        monkeypatch.setattr(
            handler,
            "dispatch_issue",
            lambda **kw: {
                "status": "approval_required",
                "draft_id": "deadbeef",
                "preview": {"gate_reason": "always"},
            },
        )
        monkeypatch.setenv(handler.DISPATCH_CHANNEL_ENV, "C1")
        monkeypatch.setenv(handler.DISPATCH_THREAD_TS_ENV, "1.0")
        monkeypatch.setenv(handler.DISPATCH_AGENT_ENV, "sam")

        rc = handler.run(["dispatch_issue", "--issue-url", "https://github.com/o/r/issues/1"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "approval_required"
