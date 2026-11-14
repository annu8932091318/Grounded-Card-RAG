"""GET /prompts, GET /prompts/active"""
from fastapi import APIRouter, Depends

from src.controller import prompt_controller
from src.middleware.auth_middleware import require_api_key

router = APIRouter(tags=["prompts"], dependencies=[Depends(require_api_key)])


@router.get("/prompts")
def list_prompts():
    return prompt_controller.list_prompts()


@router.get("/prompts/active")
def active_prompt():
    return prompt_controller.active_prompt()
