"""Runs the eval set inside the server (admin). Results are files: eval/results/run-<stamp>.json / .md
(no eval tables: a run is a report you read and diff, not a record you query)."""
import json

from src.const.config import config
from src.const.errors import AppError

RESULTS = config.root / "eval" / "results"


def run(case_ids: list[str] | None) -> dict:
    from eval.run_eval import run_cases  # WHY local: keeps server boot independent of the eval package

    summary, _results = run_cases(case_ids=case_ids, write_reports=True)
    return summary


def list_runs() -> dict:
    runs = []
    for f in sorted(RESULTS.glob("run-*.json"), reverse=True)[:50]:
        s = json.loads(f.read_text(encoding="utf-8"))["summary"]
        runs.append({"run_id": f.stem, **{k: s.get(k) for k in ("started_at", "prompt_version", "model", "total", "passed",
                                                                 "pass_rate", "critical_fail", "gate_passed")}})
    return {"runs": runs}


def get_run(run_id: str) -> dict:
    f = RESULTS / f"{run_id}.json"
    if not run_id.startswith("run-") or not f.is_file():
        raise AppError("NOT_FOUND")
    return json.loads(f.read_text(encoding="utf-8"))
