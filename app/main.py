"""
OpenPlan AI 서비스 — FastAPI 진입점 (W2 뼈대).

지금 목표는 "상용 LLM에 핑 → 응답"까지만. 계획 생성은 W3.
Spring 백엔드가 REST로 이 서비스를 호출한다: AI 초안 생성 → Spring 규칙검증 → 사용자 확정.

제공자(Gemini·Claude·GPT·로컬)는 .env 의 모델 문자열로만 정해진다 — 아래 코드는 제공자를 모른다.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import get_settings, required_env_for
from app.models.schemas import HealthResponse, PingRequest, PingResponse, PlanDraftRequest, PlanDraftResponse
from app.orchestrator.orchestrator import DRAFT_TIMEOUT_SECONDS, ModelOutputError, Orchestrator
from app.router.model_router import ModelRouter, Tier

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # .env 의 키를 LiteLLM 이 읽는 os.environ 으로 브리지(실제 환경변수가 우선).
    # 쓰지 않는 제공자의 키는 비어 있어도 된다.
    for env_name, value in settings.api_keys_by_env().items():
        if value:
            os.environ.setdefault(env_name, value)

    app.state.router = ModelRouter(settings)
    app.state.orchestrator = Orchestrator(app.state.router)  # 서브에이전트 자리 — W3+
    yield


app = FastAPI(title="OpenPlan AI Service", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="openplan-ai")


@app.post("/ping", response_model=PingResponse)
async def ping(req: PingRequest) -> PingResponse:
    """뼈대 동작 검증용 — 프롬프트를 라우터로 모델에 보내 응답을 돌려준다. (계획 생성 아님)"""
    router: ModelRouter = app.state.router
    try:
        tier = Tier(req.tier)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"알 수 없는 tier: {req.tier}")

    # 키 미설정을 LLM 호출 실패와 구분한다 — 첫 실행에서 가장 흔한 원인이라 메시지를 명확히.
    model = router.model_for(tier)
    env_name = required_env_for(model)
    if env_name and not os.environ.get(env_name):
        raise HTTPException(
            status_code=503,
            detail=f"{env_name} 미설정 — 모델 '{model}' 을 호출할 수 없습니다. .env 를 확인하세요.",
        )

    try:
        result = await router.complete(req.prompt, tier)
    except Exception as e:  # 모델명 오타·한도 초과·네트워크 등
        raise HTTPException(status_code=502, detail=f"LLM 호출 실패: {e}")
    return PingResponse(tier=result.tier.value, model=result.model, output=result.output)


@app.post("/plans/draft", response_model=PlanDraftResponse)
async def plans_draft(req: PlanDraftRequest) -> PlanDraftResponse:
    """
    주간 계획 초안 생성 (계약 §3·§4). 스키마 위반은 FastAPI/pydantic 이 자동으로 422를
    낸다 — 여기서는 그 뒤(모델 키·호출·시간·형식)만 다룬다.

    실패 매핑(계약 §4 실패 응답 표):
      503 모델 키 미설정 · 502 모델 호출 실패/형식 오류 · 504 타임아웃(20초, §7 #2).
    본문은 전부 {"detail": "…"} (FastAPI 기본형).
    """
    router: ModelRouter = app.state.router
    orchestrator: Orchestrator = app.state.orchestrator

    # /ping과 동일한 판단: 키 미설정을 호출 실패와 구분해 즉시 503으로 끊는다(계약 §4).
    model = router.model_for(Tier.COMPLEX)
    env_name = required_env_for(model)
    if env_name and not os.environ.get(env_name):
        raise HTTPException(
            status_code=503,
            detail=f"{env_name} 미설정 — 모델 '{model}' 을 호출할 수 없습니다. .env 를 확인하세요.",
        )

    try:
        return await orchestrator.generate_plan(req)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504, detail=f"모델 응답이 {DRAFT_TIMEOUT_SECONDS:.0f}초 안에 오지 않았습니다."
        )
    except ModelOutputError as e:
        raise HTTPException(status_code=502, detail=f"모델 응답 형식 오류: {e}")
    except Exception as e:  # 한도 초과·네트워크·모델 은퇴 등
        raise HTTPException(status_code=502, detail=f"LLM 호출 실패: {e}")
