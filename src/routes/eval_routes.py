"""POST /eval/run (admin), GET /eval/runs, GET /eval/runs/{id}"""
from fastapi import APIRouter, Depends

from src.controller import eval_controller
from src.middleware.auth_middleware import require_admin, require_api_key
from src.middleware.validate_middleware import EvalRunRequest

router = APIRouter(tags=["eval"])


@router.post("/eval/run", dependencies=[Depends(require_admin)])
def run(body: EvalRunRequest | None = None):
    return eval_controller.run(body.case_ids if body else None)


@router.get("/eval/runs", dependencies=[Depends(require_api_key)])
def list_runs():
    return eval_controller.list_runs()


@router.get("/eval/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_run(run_id: str):
    return eval_controller.get_run(run_id)
