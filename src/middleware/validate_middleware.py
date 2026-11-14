"""Request body schemas (pydantic = the zod equivalent). FastAPI validates them before the controller runs;
error_middleware maps failures to 400 VALIDATION_ERROR."""
from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=r"^CUST-\d{4}$")
    message: str = Field(min_length=1, max_length=1000)
    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


class EvalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_ids: list[str] | None = None
