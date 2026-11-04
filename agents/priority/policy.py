# agents/priority/policy.py
"""
Prioritization policy — the ONLY place weights and mappings live.

Default: WSJF (Weighted Shortest Job First)
    cost_of_delay = value + time_criticality + risk_reduction + enablement   (each 1-5, weighted)
    score         = cost_of_delay / job_size                                 (job_size from PERT hours)

These defaults are a starting proposal, NOT team-approved weights. Change the numbers
here (and bump POLICY_VERSION) — no code changes needed. A policy change invalidates
the stored backlog fingerprint, so the next run re-ranks automatically.
"""

POLICY_VERSION = "wsjf-default-2"

WEIGHTS = {
    "business_value": 1.0,
    "time_criticality": 1.0,
    "risk_reduction": 1.0,
    "enablement": 1.0,
}

# Business value comes from the PO's Jira priority (a real field, not an LLM guess).
JIRA_PRIORITY_VALUE = {
    "highest": 5, "blocker": 5, "critical": 5,
    "high": 4,
    "medium": 3,
    "low": 2,
    "lowest": 1, "trivial": 1, "minor": 2, "major": 4,
}
DEFAULT_VALUE = 3  # used when no ticket priority exists; always flagged for review

# Time criticality: bugs hurt users today; labels can mark urgency explicitly.
TIME_CRITICALITY_BY_KIND = {"bug_fix": 4, "story": 2, "enabler": 2}
DEFAULT_TIME_CRITICALITY = 2
BUG_URGENCY_MIN_VALUE = 3  # a bug only counts as time-critical if its value is Medium or higher
URGENT_LABELS = {"urgent", "hotfix", "escalation", "customer-escalation", "sev1", "p0", "deadline"}
URGENT_TIME_CRITICALITY = 5
DUE_DATE_TIME_CRITICALITY = 4  # ticket has a due date

# Risk reduction: doing a story that a high-exposure risk affects reduces that risk early.
LIKELIHOOD_POINTS = {"low": 1, "medium": 2, "high": 3}
IMPACT_POINTS = {"low": 1, "medium": 2, "high": 3, "critical": 4}
# exposure = likelihood x impact (1..12) -> 1..5
RISK_EXPOSURE_BANDS = [(0, 1), (2, 2), (4, 3), (6, 4), (9, 5)]  # (min exposure, score)

# Enablement: 1 + number of stories that (transitively) wait on this one, capped at 5.
MAX_ENABLEMENT = 5

# Job size from PERT expected hours (Fibonacci-like, as in WSJF).
SIZE_BUCKETS = [(8, 1), (16, 2), (40, 3), (80, 5), (float("inf"), 8)]  # (max hours, size)

# Items the PO marked Low/Lowest (value <= LOW_VALUE_MAX) rank after all higher-value items,
# so a tiny low-value job cannot jump into P0 just because it is small. Dependencies still win.
DEPRIORITIZE_LOW_VALUE = True
LOW_VALUE_MAX = 2

# Labels by position in the ranked list (fraction of ranked items).
LABEL_CUTS = [("P0", 0.20), ("P1", 0.60), ("P2", 1.00)]

# Ticket statuses that mean a dependency is already satisfied.
DONE_STATUSES = {"done", "closed", "resolved", "released", "complete", "completed"}

LOW_ESTIMATE_CONFIDENCE = {"low"}