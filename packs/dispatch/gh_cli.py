"""gh_cli.py — centralized `gh` subprocess run/error/parse helper (#736).

Canonical implementation for the "shell out to `gh`, catch spawn/timeout
exceptions, check returncode, slice an error tail, then json.loads the
stdout" block that was independently hand-rolled at seven call sites
across ``pr_review.py`` and ``handler.py``, with drifted exception tuples,
timeouts, and tail-slice lengths. ``verify.py`` already had a private
``_run`` helper meant to absorb this; it now delegates here instead of
carrying a second definition.

Distinct from ``router/github_api.py``, which wraps the httpx REST path —
this module is the counterpart for the `gh` *subprocess* path, which had
no shared wrapper before this.

Co-located with ``token_reader.py`` / ``quota.py`` so siblings reach it
with a plain ``from gh_cli import ...`` (both pack containers mount this
dir; see ``token_reader.py`` docstring for the sys.path context).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

_DEFAULT_TAIL = 300


@dataclass
class GhResult:
    """Uniform result of a single ``gh`` subprocess invocation.

    ``ok`` is True only when the process spawned and exited zero.
    ``error_kind`` distinguishes *why* a run didn't produce a usable
    result: ``"not_found"`` (gh binary missing), ``"timeout"``, or
    ``"os_error"`` (any other spawn failure); ``None`` means the process
    ran to completion, whatever its exit code.
    """

    returncode: int
    stdout: str
    stderr: str
    ok: bool
    error: str | None = None
    error_kind: str | None = None

    def error_detail(self, *, tail: int = _DEFAULT_TAIL) -> str:
        """One error-detail policy: spawn-error message, else a stderr-then-stdout tail.

        Used by callers that want a short, loggable/returnable string
        instead of branching on ``error_kind`` themselves.
        """
        if self.error:
            return self.error
        text = (self.stderr or self.stdout or "").strip()
        return text[-tail:] if tail else text


def run_gh(
    cmd: list[str],
    *,
    timeout: float = 30,
    run: Any = None,
    env: dict[str, str] | None = None,
) -> GhResult:
    """Run a ``gh`` subprocess and return a uniform :class:`GhResult`.

    Never raises: ``FileNotFoundError`` (gh binary missing),
    ``subprocess.TimeoutExpired``, and any other ``OSError`` are all
    caught and folded into ``GhResult(ok=False, error=..., error_kind=...)``.

    ``run`` defaults to ``None`` and resolves to ``subprocess.run`` at
    call time — *not* as a bound parameter default — so tests that
    monkeypatch ``subprocess.run`` globally stay effective.
    """
    run_fn = run if run is not None else subprocess.run
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "check": False, "timeout": timeout}
    if env is not None:
        kwargs["env"] = env
    try:
        result = run_fn(cmd, **kwargs)
    except FileNotFoundError:
        return GhResult(
            returncode=-1, stdout="", stderr="", ok=False, error="gh binary not found", error_kind="not_found"
        )
    except subprocess.TimeoutExpired:
        return GhResult(
            returncode=-1, stdout="", stderr="", ok=False, error=f"gh timed out: {cmd!r}", error_kind="timeout"
        )
    except OSError as exc:
        return GhResult(returncode=-1, stdout="", stderr="", ok=False, error=str(exc), error_kind="os_error")

    return GhResult(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        ok=result.returncode == 0,
    )


def run_gh_json(
    cmd: list[str],
    *,
    timeout: float = 30,
    run: Any = None,
    env: dict[str, str] | None = None,
) -> tuple[GhResult, Any]:
    """:func:`run_gh` plus ``json.loads(stdout or "{}")`` on success.

    Returns ``(result, None)`` when the run failed or stdout was not
    valid JSON; callers distinguish the two cases via ``result.ok``.
    """
    result = run_gh(cmd, timeout=timeout, run=run, env=env)
    if not result.ok:
        return result, None
    try:
        return result, json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return result, None
