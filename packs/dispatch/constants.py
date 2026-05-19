"""Shared constants for the dispatch pack.

Centralised here so handler.py and babysit.py always agree without
requiring a "must stay in sync" comment.
"""

POOL_SLOTS_DIR_NAME = ".slots"

# D-5: Quota sentinel file names (also defined in quota.py; kept here
# so handler.py and babysit.py can reference them without importing quota).
QUOTA_LOCKED_FILE = ".quota_locked"
WARNING_SENT_PREFIX = ".warning_sent_"
