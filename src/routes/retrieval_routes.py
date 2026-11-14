"""POST /retrieval/search (debug)"""
from fastapi import APIRouter, Depends

from src.controller import retrieval_controller
from src.middleware.rate_limit_middleware import rate_limit
from src.middleware.validate_middleware import SearchRequest

router = APIRouter(tags=["retrieval"])


@router.post("/retrieval/search")
def search(body: SearchRequest, ctx: dict = Depends(rate_limit)):
    return retrieval_controller.search(body, ctx)
