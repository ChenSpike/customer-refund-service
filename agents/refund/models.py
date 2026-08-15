from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RefundRequest(BaseModel):
    trace_id: str
    ticket_id: str
    order_id: str
    amount: float = Field(ge=0)
    currency: str


class RefundResult(BaseModel):
    status: Literal["success", "failed"]
    refund_id: str | None = None
    order_id: str
    amount: float = Field(ge=0)
    currency: str
    processed_at: datetime
    message: str
    failure_code: str | None = None