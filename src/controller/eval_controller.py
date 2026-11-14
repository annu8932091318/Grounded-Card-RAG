"""Runs the eval set inside the server (admin). CI uses the CLI: python -m eval.run_eval."""
from src.const.errors import AppError
from src.db.repositories import eval_repo


def run(case_ids: list[str] | None) -> dict:
    from eval.run_eval import run_cases  # WHY local: keeps server boot independent of the eval package

    summary, _results = run_cases(case_ids=case_ids, write_reports=True)
    return summary


def list_runs() -> dict:
    return {"runs": eval_repo.list_runs()}


def get_run(run_id: str) -> dict:
    run = eval_repo.get_run(run_id)
    if not run:
        raise AppError("NOT_FOUND")
    return run
