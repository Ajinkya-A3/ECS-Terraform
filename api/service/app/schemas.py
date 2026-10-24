from datetime import datetime
from typing import Literal, Optional

from pydantic import UUID7, BaseModel, Field

BorrowerBucket = Literal["0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD"]
ProductType = Literal["credit_card", "bnpl", "personal_loan"]
Outcome = Literal["connected", "no_answer", "ptp_taken", "dispute", "failed_downstream"]


class CallEvent(BaseModel):
    event_id: UUID7
    campaign_id: str
    borrower_bucket: BorrowerBucket
    timestamp: datetime
    outcome: Outcome
    product_type: Optional[ProductType] = None
    call_duration_s: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    audio_ref: str = "synthetic"  # stand-in for real audio


class CallResponse(BaseModel):
    event_id: str
    status: Literal["completed", "degraded"]
    outcome: Outcome
    attempts: int
    transcript: Optional[str] = None
    response_audio_ref: Optional[str] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    dlq_id: Optional[int] = None