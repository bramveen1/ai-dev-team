"""Triage gate (machine-checkable — no LLM vibes).

Two layers, both biased to hold:

* :func:`triage` — diff-based, run on the real PR files after the worker PR
  lands.  Path globs + changed-file count; first deny-list match wins.
* :func:`_pre_dispatch_triage` — coarse issue-level prescan (title + labels)
  run before a PR exists, so obviously sensitive issues are held early.
"""

from __future__ import annotations

import re
from pathlib import Path

# Triage deny-list: path glob → reason label.  Evaluated in order; first
# match wins.  Any path that matches any entry routes to "hold".
TRIAGE_DENY_GLOBS: tuple[tuple[str, str], ...] = (
    # Auth
    ("**/auth/**", "auth"),
    ("**/authentication/**", "auth"),
    ("**/*auth*", "auth"),
    # Money / billing
    ("**/billing/**", "billing"),
    ("**/payment/**", "billing"),
    ("**/invoice/**", "billing"),
    ("**/*billing*", "billing"),
    ("**/*payment*", "billing"),
    # DB migrations
    ("**/migrations/**", "db_migration"),
    ("**/alembic/**", "db_migration"),
    ("**/*.sql", "db_migration"),
    ("**/*migration*", "db_migration"),
    ("**/*migrate*", "db_migration"),
    # Deploy / compose config
    ("**/docker-compose*", "deploy_config"),
    ("**/Dockerfile*", "deploy_config"),
    ("**/systemd/**", "deploy_config"),
    ("**/.github/**", "deploy_config"),
    ("**/deploy/**", "deploy_config"),
    # Secrets
    ("**/secrets/**", "secrets"),
    ("**/.env*", "secrets"),
    ("**/*secret*", "secrets"),
    ("**/*token*", "secrets"),
)


def _compile_glob(pattern: str) -> re.Pattern:
    """Compile a ``**``-glob pattern to a regex.

    Semantics:
    - ``**`` matches zero or more path components (including ``/``).
    - ``*`` matches any characters except ``/``.
    - ``?`` matches any single character except ``/``.

    Works on Python 3.10+ (``Path.match`` gained full ``**`` support only
    in 3.12; this helper uses regex so it is version-independent).
    """
    # Split on "**" to isolate the double-star segments.
    parts = pattern.split("**")
    re_parts: list[str] = []
    for segment in parts:
        # Within each non-** segment, convert * → [^/]* and ? → [^/], and
        # escape all other regex-special characters.
        seg_re = ""
        for ch in segment:
            if ch == "*":
                seg_re += "[^/]*"
            elif ch == "?":
                seg_re += "[^/]"
            elif ch in r"\.+^${}()|[]":
                seg_re += "\\" + ch
            else:
                seg_re += ch
        re_parts.append(seg_re)

    if len(re_parts) == 1:
        # No ** in pattern — match the full path (or just filename).
        return re.compile(f"^(?:.*/)?{re_parts[0]}$", re.IGNORECASE)

    # Join segments with .*  (** = any characters including /).
    # Then post-process: replace `.*<sep>` and `<sep>.*` edge patterns so
    # that **/ at the start means "zero or more leading directories" and
    # /** at the end means "zero or more trailing path components".
    combined = ".*".join(re_parts)
    # Replace `.*/<something>` with `(?:.*/<something>|<something>)` so the
    # leading **/ is truly optional (matches files at the repo root too).
    # We do this by replacing a leading `.*` followed by `/` with `(?:.*/)?`.
    combined = re.sub(r"^\.\*/", "(?:.*/)?", combined)
    # Replace a trailing `/<something>.*` → `(?:/.*)?` suffix for /** at end.
    combined = re.sub(r"/\.\*$", "(?:/.*)?", combined)
    return re.compile(f"^{combined}$", re.IGNORECASE)


def _is_test_file(file_path: str) -> bool:
    """Return True if ``file_path`` is a test file (conservative allow-list, bias-to-hold).

    A file is "test" iff: it lives under a ``tests/`` directory, OR its
    filename matches ``test_*.py`` / ``*_test.py``, OR its filename is
    ``conftest.py``. Anything ambiguous falls through as "code" (counts
    against ``multi_file_threshold``) — the deny-list still runs over test
    files regardless of this classification.
    """
    path = Path(file_path).as_posix()
    name = Path(path).name
    if "tests" in Path(path).parent.parts:
        return True
    if name == "conftest.py":
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    return False


def _path_matches_deny(file_path: str, deny_globs: tuple[tuple[str, str], ...] = TRIAGE_DENY_GLOBS) -> str | None:
    """Return the reason label if ``file_path`` matches any deny-list glob, else None.

    Uses a regex compiled from each glob pattern so ``**`` semantics work on
    Python 3.10+ (``Path.match`` gained full ``**`` support only in 3.12).
    The path is normalised to POSIX forward-slash form before matching.

    Bias to hold: an ambiguous/unrecognisable path falls through all patterns
    and returns None from this function, but :func:`triage` catches the empty
    case before calling here.
    """
    normalised = Path(file_path).as_posix()
    for glob, reason in deny_globs:
        pattern_re = _compile_glob(glob)
        if pattern_re.match(normalised):
            return reason
    return None


def triage(
    changed_files: list[str],
    *,
    multi_file_threshold: int = 1,
    deny_globs: tuple[tuple[str, str], ...] = TRIAGE_DENY_GLOBS,
) -> tuple[str, str]:
    """Evaluate blast radius and return ``(decision, reason)``.

    ``decision`` is one of:

    * ``"low_risk"`` — safe to auto-merge after verdict+CI gate.
    * ``"hold"``     — hold for human review.

    Bias to hold — false-negative (calling a sensitive change low_risk) is
    the dangerous direction:

    1. **No files** — ``hold`` (unknown diff).
    2. **Multi-file** — ``hold`` if the count of *non-test* files (see
       :func:`_is_test_file`) exceeds ``multi_file_threshold``. Test files
       are excluded from the count — a 1-code + N-tests diff can pass.
    3. **Deny-list match** — ``hold`` on first match, evaluated over *all*
       changed files including tests.
    4. **Remaining files** — ``low_risk``.
    """
    if not changed_files:
        return "hold", "unknown_diff"

    non_test_files = [f for f in changed_files if not _is_test_file(f)]
    if len(non_test_files) > multi_file_threshold:
        return "hold", f"multi_file:{len(non_test_files)}"

    for file_path in changed_files:
        reason = _path_matches_deny(file_path, deny_globs)
        if reason is not None:
            return "hold", reason

    return "low_risk", "clean"


# ---------------------------------------------------------------------------
# Pre-dispatch triage (issue-level, before a PR exists)
# ---------------------------------------------------------------------------

# Keywords that map directly to deny-list reasons when found in issue title/body.
_PRESCAN_DENY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bauth(?:entication|orization)?\b", re.IGNORECASE), "auth"),
    (re.compile(r"\bbilling\b|\bpayment\b|\binvoice\b", re.IGNORECASE), "billing"),
    (re.compile(r"\bmigration\b|\balembic\b", re.IGNORECASE), "db_migration"),
    (re.compile(r"\bdocker-compose\b|\bdockerfile\b|\bsystemd\b|\bdeploy\b", re.IGNORECASE), "deploy_config"),
    (re.compile(r"\bsecret\b|\btoken\b|\bcredential\b", re.IGNORECASE), "secrets"),
)


def _pre_dispatch_triage(issue: dict, *, multi_file_threshold: int = 1) -> tuple[str, str]:
    """Coarse pre-dispatch triage from issue metadata (title + labels).

    Falls back to ``low_risk`` when no deny pattern is matched — the fine-grained
    diff-based triage runs after the worker PR is created.
    """
    label_names = " ".join(lbl.get("name", "") for lbl in (issue.get("labels") or []))
    text = (issue.get("title") or "") + " " + label_names
    for pattern, reason in _PRESCAN_DENY_PATTERNS:
        if pattern.search(text):
            return "hold", reason
    return "low_risk", "pre_dispatch_ok"
