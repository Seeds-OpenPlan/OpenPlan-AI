"""
OpenPlan AI 서비스 — FastAPI 진입점 (W2 뼈대).

지금 목표는 "상용 Claude에 핑 → 응답"까지만. 계획 생성은 W3.
Spring 백엔드가 REST로 이 서비스를 호출한다: AI 초안 생성 → Spring 규칙검증 → 사용자 확정.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.models.schemas import HealthResponse, PingRequest, PingResponse
from app.orchestrator.orchestrator import Orchestrator
from app.router.model_router import ModelRouter, Tier

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # .env 의 키를 LiteLLM 이 읽는 os.environ 으로 브리지(실제 환경변수가 우선).
    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
    app.state.router = ModelRouter(settings)
    app.state.orchestrator = Orchestrator(app.state.router)  # 서브에이전트 자리 — W3+
    yield


app = FastAPI(title="OpenPlan AI Service", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="openplan-ai")


@app.post("/ping", response_model=PingResponse)
async def ping(req: PingRequest) -> PingResponse:
    """뼈대 동작 검증용 — 프롬프트를 라우터로 Claude에 보내 응답을 돌려준다. (계획 생성 아님)"""
    router: ModelRouter = app.state.router
    try:
        tier = Tier(req.tier)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"알 수 없는 tier: {req.tier}")
    try:
        result = await router.complete(req.prompt, tier)
    except Exception as e:  # 키 미설정·모델 오류 등
        raise HTTPException(status_code=502, detail=f"LLM 호출 실패: {e}")
    return PingResponse(tier=result.tier.value, model=result.model, output=result.output)
