from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.modules.agent.read_tools import ReadToolRequest


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    agent_name: str = Field(default="assistant", pattern="^(assistant|operations|analytics)$")
    knowledge_base_id: int | None = None
    client_request_id: str = Field(min_length=1, max_length=64)
    tool_input: ReadToolRequest = Field(default_factory=ReadToolRequest)


class AgentRunQuery(BaseModel):
    status: str | None = None
    agent_name: str | None = None
    thread_id: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


__all__ = ["AgentRunQuery", "ChatRequest"]
