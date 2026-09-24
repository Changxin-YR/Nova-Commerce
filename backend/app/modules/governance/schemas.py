from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DecidePendingActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_hash: str = Field(min_length=64, max_length=64)
    decision_reason: str | None = Field(default=None, max_length=500)


class PendingActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_run_id: str
    action_type: str
    tool_name: str
    summary: str
    risk_level: str
    status: str
    payload: dict
    payload_hash: str
    requested_by: str
    decided_by: str | None
    decision_reason: str | None
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None
    executed_at: datetime | None
    execution_receipt: dict | None


__all__ = ["DecidePendingActionRequest", "PendingActionOut"]
