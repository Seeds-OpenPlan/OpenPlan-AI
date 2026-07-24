"""요청·응답 스키마."""
from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str


class PingRequest(BaseModel):
    prompt: str = Field(..., description="Claude에 보낼 프롬프트 (뼈대 동작 검증용)")
    tier: str = Field("complex", description="complex(상용 추론) | light(경량 — 현재 상용, W5+ 로컬)")


class PingResponse(BaseModel):
    tier: str
    model: str
    output: str
