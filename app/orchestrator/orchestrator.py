"""
메인 에이전트(오케스트레이터) — 진입점.

전체 흐름을 조정하고, 아래 서브에이전트에게 작업을 나눠 맡긴다.

서브에이전트 슬롯:
  - generate_plan       계획 생성      [W3, 상용]   ← 데모 성립선(L1). 이번 커밋에서 구현.
  - replan              재계획 대안    [W4, 상용]   ← 자리만 (로드맵대로 재기획 금지)
  - explain             비교·설명 생성 [W4]
  - evaluate_task       태스크 평가    [W5, 경량→로컬]
  - personalize         개인화·보정    [W5, 경량→로컬]

경계(엄수): **AI는 '초안 생성'만.** 판정(검증)은 Spring 규칙엔진이 한다(계약 §4).
흐름: 사용자 요청 → (이 서비스) AI 초안 생성 → Spring 규칙검증 → 사용자 확정 → 저장.

generate_plan 은 겹침·마감·가용시간 위반을 스스로 판정하지 않는다 — 프롬프트로 좋은 배치를
유도할 뿐이다. 최종 판정은 항상 Spring `PlanValidationPort.validate`가 한다(계약 §4·§5).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from uuid import UUID

from pydantic import ValidationError

from app.models.schemas import (
    PlanDraftMeta,
    PlanDraftRequest,
    PlanDraftResponse,
    ProposedBlock,
)
from app.router.model_router import ModelRouter, Tier

# 계약 §7 #2 확정값(리드 통보) — 리드가 "추측하지 말고 이대로" 라고 명시한 상수라 그대로 하드코딩한다.
# 초과 시 Spring이 규칙 폴백으로 넘어간다(호출자 책임 — 이 서비스는 그냥 늦게 실패할 뿐이다).
DRAFT_TIMEOUT_SECONDS = 20.0


class ModelOutputError(Exception):
    """LLM 출력이 비-JSON이거나 계약 스키마(§4)와 어긋날 때. main.py 가 502로 매핑한다."""


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """모델이 ```json ... ``` 로 감싸 답하는 경우가 흔하다 — JSON 파싱 전에 벗겨낸다."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def _build_draft_prompt(req: PlanDraftRequest) -> str:
    """
    스냅샷 + tasksToPlace 를 프롬프트에 그대로 실어 보낸다(무상태, D-1 — 서비스가 따로
    상태를 들고 있지 않고 매 요청에 실린 것만 본다).

    타임존 변환 등은 여기서 하지 않는다 — snapshot.zone 을 문자열 그대로 넘기고 판단은
    모델에 맡긴다. 시각 계산을 이 코드가 대신하면 그 계산이 또 하나의 "판정 로직"이 되어
    Spring 규칙 엔진과 이중화될 위험이 있다(계약이 명시적으로 경계한 지점).
    """
    snapshot = req.snapshot
    payload = {
        "weekStartDate": snapshot.week_start_date.isoformat(),
        "zone": snapshot.zone,
        "referenceTime": snapshot.reference_time.isoformat().replace("+00:00", "Z"),
        "existingBlocks": [b.model_dump(mode="json", by_alias=True) for b in snapshot.blocks],
        "activeFixedSchedules": [
            f.model_dump(mode="json", by_alias=True) for f in snapshot.active_fixed_schedules
        ],
        "availabilities": [a.model_dump(mode="json", by_alias=True) for a in snapshot.availabilities],
        "taskFacts": {
            str(task_id): facts.model_dump(mode="json", by_alias=True)
            for task_id, facts in snapshot.task_facts.items()
        },
        "tasksToPlace": [str(t) for t in req.tasks_to_place],
    }
    data_json = json.dumps(payload, ensure_ascii=False, indent=2)

    # 스키마 설명을 프롬프트에 박아 넣는다(코드로 강제하는 것과 별개로, 모델이 형식을 지키게
    # 유도하는 최선의 방법은 여전히 예시를 보여주는 것 — 실제 강제는 orchestrator 쪽 파싱/검증이 한다).
    return f"""당신은 OpenPlan 주간 계획의 배치 초안을 제안하는 도우미입니다.

아래는 이번 주 스냅샷입니다(이미 배치된 블록·고정 일정·가용 시간·태스크 사실).
"tasksToPlace" 에 있는 태스크만 배치 대상입니다. 그 외 태스크는 건드리지 마십시오.

{data_json}

지침:
- 마감(dueDate)이 이른 태스크, priority 값이 큰 태스크를 우선 배치하십시오.
- estimatedMinutes 길이만큼 availabilities 의 가용 시간 안에, existingBlocks·
  activeFixedSchedules 와 겹치지 않는 자리를 우선 고르십시오. 다만 최종 겹침·마감·가용
  판정은 별도 규칙 엔진이 담당하므로 완벽하지 않아도 됩니다 — 명백한 충돌만 피하십시오.
- 자리를 찾지 못한 태스크는 억지로 배치하지 말고 unplacedTaskIds 에 넣으십시오.
- tasksToPlace 에 없는 taskId 를 만들어 쓰지 마십시오.

아래 JSON 하나만 출력하십시오. 코드펜스·설명 문장 없이 JSON 객체만 출력하십시오.
{{
  "proposedBlocks": [
    {{"type": "TASK", "taskId": "<tasksToPlace 안의 UUID>", "scheduleId": null,
      "startAt": "<ISO-8601 UTC, 'Z'>", "endAt": "<ISO-8601 UTC, 'Z'>"}}
  ],
  "unplacedTaskIds": ["<배치 못한 태스크 UUID>"],
  "reason": "<배치 근거를 설명하는 한국어 문장 — 빈 문자열 금지>"
}}
blockId 필드는 넣지 마십시오(저장 전 제안이라 ID는 Spring이 저장 시 만듭니다).
"""


def _parse_model_output(raw: str) -> dict:
    candidate = _strip_code_fence(raw)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ModelOutputError(f"모델 출력이 JSON이 아닙니다: {e}") from e
    if not isinstance(parsed, dict):
        raise ModelOutputError("모델 출력이 JSON 객체가 아닙니다")
    return parsed


def _to_response(parsed: dict, tasks_to_place: list[UUID], model: str, latency_ms: int) -> PlanDraftResponse:
    """
    파싱된 LLM 출력 → 계약 §4 응답. 여기서 하는 검증은 전부 **형식** 검증이다
    (필드 존재·타입·ID가 tasksToPlace 소속인지) — 겹침·마감·가용 같은 **판정**은 하지 않는다.
    그건 Spring 규칙 엔진의 일이다(계약 §4 "AI가 겹침·마감 위반을 낼 수 있고, 그걸 잡는 게
    규칙의 일이다").
    """
    missing = [k for k in ("proposedBlocks", "unplacedTaskIds", "reason") if k not in parsed]
    if missing:
        raise ModelOutputError(f"모델 출력에 필수 필드 누락: {missing}")

    try:
        proposed_blocks = [ProposedBlock.model_validate(b) for b in parsed["proposedBlocks"]]
        unplaced_task_ids = [UUID(str(t)) for t in parsed["unplacedTaskIds"]]
    except (ValidationError, ValueError, TypeError) as e:
        raise ModelOutputError(f"모델 출력 형식이 계약과 다릅니다: {e}") from e

    reason = parsed["reason"]
    if not isinstance(reason, str) or not reason.strip():
        # 계약: reason 은 필수·빈 문자열 금지(규칙 엔진 C-3와 같은 원칙).
        raise ModelOutputError("모델 출력의 reason 이 비어 있습니다")

    # ID 소속 검증(형식 검증) — tasksToPlace 밖의 taskId 를 Spring에 그대로 넘기면 저장 단계에서
    # 조회 실패로 이어질 수 있다. 이건 "이 ID가 요청받은 것인가"이지 "이 배치가 옳은가"가 아니므로
    # 경계(판정은 Spring 몫) 안에 있다고 판단했다 — 계약에 명시되지 않아 리드 확인 필요(최종 보고 참조).
    known_ids = set(tasks_to_place)
    referenced_ids = unplaced_task_ids + [b.task_id for b in proposed_blocks if b.task_id is not None]
    unknown_ids = [str(t) for t in referenced_ids if t not in known_ids]
    if unknown_ids:
        raise ModelOutputError(f"모델이 tasksToPlace 밖의 taskId 를 참조했습니다: {unknown_ids}")

    return PlanDraftResponse(
        proposed_blocks=proposed_blocks,
        unplaced_task_ids=unplaced_task_ids,
        reason=reason,
        meta=PlanDraftMeta(model=model, latency_ms=latency_ms),
    )


class Orchestrator:
    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    async def generate_plan(self, req: PlanDraftRequest) -> PlanDraftResponse:
        """
        스냅샷 + tasksToPlace → 계획 초안(COMPLEX 티어). 반환은 '제안'일 뿐,
        Spring 규칙엔진 검증을 통과해야 저장된다(계약 §5 호출 흐름).

        타임아웃은 asyncio.wait_for 로 여기서 직접 건다(라우터/litellm 자체 타임아웃에
        기대지 않음 — 계약 §7 #2 의 20초는 "이 서비스가 몇 초 안에 응답해야 하는가"이므로
        호출 지점에서 재는 것이 정확하다). asyncio.TimeoutError 는 그대로 올려 보내
        main.py 가 504로 매핑한다.
        """
        prompt = _build_draft_prompt(req)
        start = time.monotonic()
        result = await asyncio.wait_for(
            self._router.complete(prompt, Tier.COMPLEX), timeout=DRAFT_TIMEOUT_SECONDS
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        parsed = _parse_model_output(result.output)
        return _to_response(parsed, req.tasks_to_place, model=result.model, latency_ms=latency_ms)

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
