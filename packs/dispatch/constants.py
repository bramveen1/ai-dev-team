"""Shared constants for the dispatch pack.

Centralised here so handler.py and babysit.py always agree without
requiring a "must stay in sync" comment.
"""

POOL_SLOTS_DIR_NAME = ".slots"

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

# Maximum age for an in-flight dispatch slot before it is force-reaped as
# stale.  Set to the dispatch budget ceiling (2 h) so any slot older than
# the longest possible legitimate run is treated as a ghost.
MAX_DISPATCH_AGE_SECONDS: int = 7200

# Babysit touches the heartbeat file this often. Janitor uses 3× this
# as the freshness threshold: a workspace is considered live if its
# heartbeat was updated within the last 3 * HEARTBEAT_INTERVAL seconds.
HEARTBEAT_INTERVAL: int = 15  # seconds

# #327: Attachments shared scratch dir.
ATTACHMENTS_ROOT: str = "/var/lib/attachments"
# Thread dirs older than this (mtime) are removed by attachments_sweep.
ATTACHMENTS_TTL_DAYS: int = 7
# Disk-pressure threshold: skip new ingest when used% >= this value.
ATTACHMENTS_DISK_PRESSURE_PCT: int = 80
