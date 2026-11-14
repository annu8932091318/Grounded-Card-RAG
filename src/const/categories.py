"""Service-request queues and SLAs, copied from POL-SVC-006 (guarded by tests/test_policy_drift.py)."""

# WHY: the tool contract requires an integer sla_hours; working days are converted at 24h/day
# as a declared assumption. The customer is always shown the policy SLA *text*, never the hours.
CATEGORIES = {
    "CARDS_URGENT": {"intent": "Card block or replacement", "sla": "4 hours", "sla_hours": 4},
    "DISPUTES": {"intent": "Transaction dispute", "sla": "2 working days", "sla_hours": 48},
    "SELFSERVE": {"intent": "Statement or certificate request", "sla": "1 working day", "sla_hours": 24},
    "CREDIT_OPS": {"intent": "Limit change", "sla": "3 working days", "sla_hours": 72},
    "PAYMENTS": {"intent": "Standing instruction change", "sla": "2 working days", "sla_hours": 48},
    "COMPLAINTS": {
        "intent": "Complaint",
        "sla": "5 working days, escalation review at day 3",
        "sla_hours": 120,
    },
    "GENERAL": {
        "intent": "Cannot be categorised with confidence",
        "sla": "human review within 1 working day",
        "sla_hours": 24,
    },
}

QUEUE_NAMES = list(CATEGORIES)
