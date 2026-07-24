"""
메인 에이전트(오케스트레이터) — 진입점 스텁.

전체 흐름을 조정하고, 아래 서브에이전트에게 작업을 나눠 맡긴다. **지금은 자리만** 잡아두고
구현은 W3+ 에 채운다(재기획 금지 — 로드맵대로).

서브에이전트 슬롯:
  - generate_plan       계획 생성      [W3, 상용]   ← 데모 성립선(L1)
  - replan              재계획 대안    [W4, 상용]
  - explain             비교·설명 생성 [W4]
  - evaluate_task       태스크 평가    [W5, 경량→로컬]
  - personalize         개인화·보정    [W5, 경량→로컬]

경계(엄수): **AI는 '초안 생성'만.** 판정(검증)은 Spring 규칙엔진이 한다.
흐름: 사용자 요청 → (이 서비스) AI 초안 생성 → Spring 규칙검증 → 사용자 확정 → 저장.
"""
from __future__ import annotations

from app.router.model_router import ModelRouter, Tier


class Orchestrator:
    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    async def generate_plan(self, *args, **kwargs):
        # TODO(W3): 스냅샷(태스크·주간계획·가용) → 계획 초안 생성(COMPLEX). 반환은 '제안'일 뿐,
        #           Spring 규칙엔진 검증을 통과해야 저장된다.
        raise NotImplementedError("W3에서 구현")

    async def replan(self, *args, **kwargs):
        raise NotImplementedError("W4에서 구현")

    async def explain(self, *args, **kwargs):
        raise NotImplementedError("W4에서 구현")

    async def evaluate_task(self, *args, **kwargs):
        # W5: LIGHT 티어(분류·요약) — 로컬 Qwen 후보
        raise NotImplementedError("W5에서 구현")

    async def personalize(self, *args, **kwargs):
        # W5: 누적 이력(소요시간 편차·선호) 반영. LIGHT 티어 — 로컬 Qwen 후보
        raise NotImplementedError("W5에서 구현")
