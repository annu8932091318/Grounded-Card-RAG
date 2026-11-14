"""Response status enum (spec §10.3)."""

ANSWERED = "answered"
ANSWERED_WITH_GAP = "answered_with_gap"
DECLINED = "declined"
REFUSED = "refused"
REQUEST_RAISED = "request_raised"
DEGRADED = "degraded"

STATUSES = [ANSWERED, ANSWERED_WITH_GAP, DECLINED, REFUSED, REQUEST_RAISED, DEGRADED]
