"""POST /chat, POST /chat/stream"""
from fastapi import APIRouter, Depends

from src.controller import chat_controller
from src.middleware.rate_limit_middleware import rate_limit
from src.middleware.validate_middleware import ChatRequest

router = APIRouter(tags=["chat"])


@router.post("/chat")
def chat(body: ChatRequest, ctx: dict = Depends(rate_limit)):
    return chat_controller.chat(body, ctx)


@router.post("/chat/stream")
def chat_stream(body: ChatRequest, ctx: dict = Depends(rate_limit)):
    return chat_controller.chat_stream(body, ctx)
