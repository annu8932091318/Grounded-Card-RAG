"""Admin: re-ingest the policy corpus (same code path as `python -m scripts.ingest`)."""


def ingest() -> dict:
    from scripts.ingest import run_ingest

    return run_ingest()
