"""Shared constants for the dispatch pack.

Centralised here so handler.py and babysit.py always agree without
requiring a "must stay in sync" comment.
"""

POOL_SLOTS_DIR_NAME = ".slots"
POOL_QUEUE_DIR_NAME = ".queue"

# D-5: Quota sentinel file names (also defined in quota.py; kept here
# so handler.py and babysit.py can reference them without importing quota).
QUOTA_LOCKED_FILE = ".quota_locked"
WARNING_SENT_PREFIX = ".warning_sent_"

# D-6: Janitor constants.
# Top-level names under /var/lib/dispatch/ that the janitor must never touch.
RESERVED_TOPLEVEL: frozenset[str] = frozenset(
    {
        ".slots",
        ".queue",
        "_orphans",
        "_window",
    }
)

# Workspaces whose mtime is newer than this are in the race window —
# a handler may be in the middle of creating them.
STARTUP_GRACE_SECONDS: int = 60

# _orphans/ entries older than this are permanently deleted.
ORPHAN_TTL_DAYS: int = 7

# #327: Attachments shared scratch dir.
ATTACHMENTS_ROOT: str = "/var/lib/attachments"
# Thread dirs older than this (mtime) are removed by attachments_sweep.
ATTACHMENTS_TTL_DAYS: int = 7
# Disk-pressure threshold: skip new ingest when used% >= this value.
ATTACHMENTS_DISK_PRESSURE_PCT: int = 80
