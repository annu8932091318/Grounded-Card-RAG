"""Redaction. The raw record stays server-side (decision/ rules); the LLM and logs get the redacted copy."""

# D7: internal score never reaches the model. WHY name too: the model does not need it to answer
# (data minimisation — synthetic data, but we behave as if it were real).
LLM_HIDDEN_FIELDS = {"behavioural_score", "name"}


def redact_account(raw: dict) -> dict:
    return {k: v for k, v in raw.items() if k not in LLM_HIDDEN_FIELDS}
