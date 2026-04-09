"""Project-wide constants.

These values are intentionally hard-coded (not settings):
- Alert codes that must match `EMAIL_TEMPLATE_REPOSITORY` entries
- ZK paths relative to `zk_root_path`
- Cooldown / cache sizing that should not vary per-environment
"""
from __future__ import annotations

# Alert codes (must exist in EARS.EMAIL_TEMPLATE_REPOSITORY)
ALERT_CODE_RESOURCE_MONITOR = "RESOURCE_MONITOR"
ALERT_SUBCODE_CRITICAL = "CRITICAL"
ALERT_SUBCODE_WARNING = "WARNING"
ALERT_SUBCODE_SELF = "SELF"  # Service self-alert on fatal error

# Service self-identification (for self-alert)
SELF_ALERT_PROCESS = "ResourceMonitorServer"
SELF_ALERT_MODEL = "self"
SELF_ALERT_LINE = "self"

# MongoDB collection names (EARS DB — confirmed no collision with existing 7)
COLL_PROFILE = "RESOURCE_MONITOR_PROFILE"
COLL_RULE = "RESOURCE_MONITOR_RULE"
COLL_EQP_INFO = "EQP_INFO"  # read-only, managed by Akka server

# ZK paths (appended to settings.zk_root_path)
ZK_PATH_LEADER_ELECTION = "leader-election"
ZK_PATH_LEADER_EPOCH = "leader-epoch"
ZK_PATH_MEMBERS = "members"
ZK_PATH_ASSIGNMENTS = "assignments"
ZK_PATH_LOCKS = "locks"

# Caching
PROFILE_CACHE_MAX_SIZE = 10000
PROFILE_CACHE_TTL_SEC = 300  # 5 minutes

COOLDOWN_LOCAL_CACHE_MAX_SIZE = 50000
COOLDOWN_LOCAL_CACHE_MAX_TTL_SEC = 3600  # upper bound for TTLCache eviction

# Scheduler concurrency
ES_QUERY_SEMAPHORE = 3

# Debounce for partition redistribution
REDISTRIBUTE_DEBOUNCE_SEC = 2.0
