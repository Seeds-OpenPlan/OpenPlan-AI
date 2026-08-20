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
from app.models.schemas import (
    ExplainRequest,
    ExplainResponse,
    HealthResponse,
    PingRequest,
    PingResponse,
    PlanDraftRequest,
    PlanDraftResponse,
    ReplanRequest,
    ReplanResponse,
)
from app.orchestrator.orchestrator import (
    DRAFT_TIMEOUT_SECONDS,
    EXPLAIN_TIMEOUT_SECONDS,
    REPLAN_TIMEOUT_SECONDS,
    ModelOutputError,
    Orchestrator,
)
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


def _require_model_key(router: ModelRouter, tier: Tier) -> None:
    """
    키 미설정을 호출 실패와 구분해 즉시 503 으로 끊는다(계약 §4).

    한 곳에 모으는 이유: 엔드포인트마다 복사하면 티어가 늘 때 한 군데를 빠뜨린다.
    그 빠뜨림은 "키가 없는데 502 로 보고되는" 모양이라 원인 찾기가 오래 걸린다.
    """
    model = router.model_for(tier)
    env_name = required_env_for(model)
    if env_name and not os.environ.get(env_name):
        raise HTTPException(
            status_code=503,
            detail=f"{env_name} 미설정 — 모델 '{model}' 을 호출할 수 없습니다. .env 를 확인하세요.",
        )


@app.post("/plans/replan", response_model=ReplanResponse)
async def plans_replan(req: ReplanRequest) -> ReplanResponse:
    """
    재계획 대안 3종 생성 (SS-07/08/09).

    실패 매핑은 /plans/draft 와 같다 — 503 키 미설정 · 502 호출/형식 오류 · 504 타임아웃.
    타임아웃만 30초로 다르다(전략 3종을 한 번에 만들어 초안보다 길다).
    """
    router: ModelRouter = app.state.router
    orchestrator: Orchestrator = app.state.orchestrator
    _require_model_key(router, Tier.COMPLEX)

    try:
        return await orchestrator.replan(req)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504, detail=f"모델 응답이 {REPLAN_TIMEOUT_SECONDS:.0f}초 안에 오지 않았습니다."
        )
    except ModelOutputError as e:
        raise HTTPException(status_code=502, detail=f"모델 응답 형식 오류: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM 호출 실패: {e}")


@app.post("/plans/explain", response_model=ExplainResponse)
async def plans_explain(req: ExplainRequest) -> ExplainResponse:
    """
    계획 설명 생성 — <b>LIGHT 티어</b>라 로컬 Qwen 배선({@code LOCAL_MODEL})이 그대로 받아 간다.

    키 검사도 LIGHT 기준으로 한다. COMPLEX 기준으로 검사하면 로컬 모델만 쓰는 구성에서
    "상용 키가 없다"며 503 이 나간다 — 정작 이 요청은 키가 필요 없는데도.
    """
    router: ModelRouter = app.state.router
    orchestrator: Orchestrator = app.state.orchestrator
    _require_model_key(router, Tier.LIGHT)

    try:
        return await orchestrator.explain(req)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504, detail=f"모델 응답이 {EXPLAIN_TIMEOUT_SECONDS:.0f}초 안에 오지 않았습니다."
        )
    except ModelOutputError as e:
        raise HTTPException(status_code=502, detail=f"모델 응답 형식 오류: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM 호출 실패: {e}")
