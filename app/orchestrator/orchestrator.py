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
    ExplainRequest,
    ExplainResponse,
    PlanDraftMeta,
    PlanDraftRequest,
    PlanDraftResponse,
    ProposedBlock,
    ReplanOptionOut,
    ReplanRequest,
    ReplanResponse,
)
from app.router.model_router import ModelRouter, Tier
import logging

logger = logging.getLogger(__name__)


# 계약 §7 #2 확정값(리드 통보) — 리드가 "추측하지 말고 이대로" 라고 명시한 상수라 그대로 하드코딩한다.
# 초과 시 Spring이 규칙 폴백으로 넘어간다(호출자 책임 — 이 서비스는 그냥 늦게 실패할 뿐이다).
DRAFT_TIMEOUT_SECONDS = 20.0

# 재계획은 전략 3종을 한 번에 만들어야 해서 초안보다 길다. Spring 쪽 NFR-029(5초)는
# **규칙 엔진 판정**의 예산이지 이 서비스의 예산이 아니다 — 둘을 같은 수로 묶으면
# 상용 LLM 한 번 왕복에 5초를 주는 셈이라 정상 응답이 타임아웃으로 죽는다.
REPLAN_TIMEOUT_SECONDS = 30.0

# 설명은 요약 성격(LIGHT)이라 짧다.
EXPLAIN_TIMEOUT_SECONDS = 15.0


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


async def _complete_json(router, prompt: str, tier, timeout: float):
    """
    모델을 부르고 JSON 으로 파싱한다. **파싱에 실패하면 한 번만 다시 묻는다.**

    🔴 왜 재시도가 필요한가. LLM 출력은 간헐적으로 망가진다 — 2026-08-23 실측에서 같은
    요청을 세 번 보냈더니 두 번은 정상이고 한 번이 `Expecting ',' delimiter` 로 깨졌다.
    예산 부족이 아니었다(완료 1021 토큰 / 상한 2048). 그냥 그날의 출력이 어긋난 것이다.

    재시도가 없으면 그 한 번이 그대로 502 가 되고, 재계획은 **규칙 폴백이 3전략을 만들지
    못하므로** 사용자가 대안 화면을 아예 못 본다. 초안과 달리 대체할 것이 없다.

    두 번까지만 한다 — 모델이 계속 같은 형식으로 실패하면 그건 프롬프트 문제이지
    운이 아니고, 반복하면 한도만 태운다. 지연은 최악에 두 배가 되지만 타임아웃은
    호출마다 따로 걸리므로 전체가 무한정 늘어나지는 않는다.

    @return (parsed, model, latency_ms) — latency 는 성공한 호출 기준이다.
    """
    last_error: ModelOutputError | None = None
    for attempt in (1, 2):
        start = time.monotonic()
        result = await asyncio.wait_for(router.complete(prompt, tier), timeout=timeout)
        latency_ms = int((time.monotonic() - start) * 1000)
        try:
            return _parse_model_output(result.output), result.model, latency_ms
        except ModelOutputError as e:
            last_error = e
            if attempt == 1:
                logger.warning("모델 출력 파싱 실패 — 한 번 다시 묻는다: %s", e)
    raise last_error  # type: ignore[misc]


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



def _snapshot_payload(snapshot) -> dict:
    """스냅샷 → 프롬프트에 실을 dict. 초안·재계획·설명이 같은 표현을 쓰도록 한 곳에 둔다."""
    return {
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
    }


def _build_replan_prompt(req: ReplanRequest) -> str:
    """
    재계획 프롬프트 — 전략 3종을 한 번에 요청한다.

    한 번에 부르는 이유: 세 번 나눠 부르면 전략끼리 서로를 모른 채 만들어져 **같은 안이 세 번**
    나올 수 있다. 사용자에게 대안 셋을 보여주는 화면에서 셋이 똑같으면 고를 것이 없다.
    """
    data_json = json.dumps(_snapshot_payload(req.snapshot), ensure_ascii=False, indent=2)
    return f"""당신은 OpenPlan 주간 계획의 재배치 대안을 제안하는 도우미입니다.

아래는 현재 주간 계획 스냅샷입니다.

{data_json}

재계획이 필요해진 이유: {req.trigger}

위 이유를 반영해 **서로 다른 전략 3종**의 재배치안을 만드십시오.

- MINIMAL_CHANGE  : 지금 배치를 최대한 그대로 두고 꼭 필요한 것만 옮깁니다.
- DEADLINE_FIRST  : 마감(dueDate)이 임박한 태스크를 앞으로 당깁니다.
- WORKLOAD_BALANCE: 요일별 배치 시간을 고르게 폅니다.

지침:
- existingBlocks 의 블록을 옮기거나 빼는 것이 재배치입니다. 없던 태스크를 새로 만들지 마십시오.
- taskFacts 에 없는 taskId 를 지어내지 마십시오.
- availabilities 안에, activeFixedSchedules 와 겹치지 않게 두는 것을 우선하십시오. 다만 최종
  겹침·마감·가용 판정은 별도 규칙 엔진이 하므로 완벽하지 않아도 됩니다.
- 세 전략은 **서로 달라야** 합니다. 같은 배치를 세 번 내지 마십시오.
- 점수는 매기지 마십시오. 숫자로 된 평가는 이 응답에 넣지 않습니다.

아래 JSON 하나만 출력하십시오. 코드펜스·설명 문장 없이 JSON 객체만 출력하십시오.
{{
  "options": [
    {{"strategyType": "MINIMAL_CHANGE",
      "proposedBlocks": [
        {{"type": "TASK", "taskId": "<UUID>", "scheduleId": null,
          "startAt": "<ISO-8601 UTC, 'Z'>", "endAt": "<ISO-8601 UTC, 'Z'>"}}
      ],
      "changeSummary": "<무엇이 어떻게 바뀌는지 한국어 한두 문장 — 빈 문자열 금지>",
      "reason": "<이 전략을 권하는 근거 한국어 문장 — 빈 문자열 금지>"}}
  ]
}}
options 는 위 3종을 각각 하나씩, 정확히 3개여야 합니다.
blockId 필드는 넣지 마십시오(저장 전 제안이라 ID는 Spring이 저장 시 만듭니다).
"""


def _to_replan_response(parsed: dict, req: ReplanRequest, model: str, latency_ms: int) -> ReplanResponse:
    """
    파싱된 LLM 출력 → 재계획 응답. 초안과 같이 **형식** 검증만 한다.

    전략 3종이 정확히 한 번씩 나왔는지까지 보는 이유: 화면이 대안 비교라 하나가 빠지거나
    둘이 같은 전략이면 사용자가 고를 것이 줄어든다. 이건 "배치가 옳은가"가 아니라
    "요청한 모양으로 왔는가"라 경계 안이다.
    """
    if "options" not in parsed:
        raise ModelOutputError("모델 출력에 필수 필드 누락: ['options']")

    try:
        options = [ReplanOptionOut.model_validate(o) for o in parsed["options"]]
    except (ValidationError, ValueError, TypeError) as e:
        raise ModelOutputError(f"모델 출력 형식이 계약과 다릅니다: {e}") from e

    # 공백만 있는 문자열은 min_length=1 을 통과한다(길이가 1 이상이므로). 초안의 reason 을
    # 오케스트레이터가 따로 strip 검사하는 것과 같은 이유로 여기서도 막는다 — 화면에 빈 칸이
    # 뜨는 것과 근거가 없는 것은 사용자에게 같은 일이다(C-3).
    for option in options:
        for field, value in (("changeSummary", option.change_summary), ("reason", option.reason)):
            if not value.strip():
                raise ModelOutputError(f"{option.strategy_type} 의 {field} 가 비어 있습니다")

    expected = ["MINIMAL_CHANGE", "DEADLINE_FIRST", "WORKLOAD_BALANCE"]
    got = [o.strategy_type for o in options]
    if sorted(got) != sorted(expected):
        raise ModelOutputError(f"전략 3종이 각각 하나씩 와야 합니다: {got}")

    known_ids = set(req.snapshot.task_facts)
    unknown = sorted(
        {str(b.task_id) for o in options for b in o.proposed_blocks
         if b.task_id is not None and b.task_id not in known_ids}
    )
    if unknown:
        raise ModelOutputError(f"모델이 스냅샷에 없는 taskId 를 참조했습니다: {unknown}")

    # 선언 순으로 고정 — 모델이 낸 순서에 화면 순서가 흔들리면 같은 요청이 매번 다르게 보인다.
    options.sort(key=lambda o: expected.index(o.strategy_type))
    return ReplanResponse(options=options, meta=PlanDraftMeta(model=model, latency_ms=latency_ms))


def _build_explain_prompt(req: ExplainRequest) -> str:
    """
    설명 프롬프트 — JSON 이 아니라 **문장**을 받는다.

    구조화 출력을 요구하지 않는 이유: 결과가 사용자에게 그대로 보이는 한 덩어리 글이라
    파싱할 구조가 없다. 형식을 강제하면 모델이 형식 맞추는 데 토큰을 쓰고 설명이 짧아진다.
    """
    data_json = json.dumps(_snapshot_payload(req.snapshot), ensure_ascii=False, indent=2)
    if req.issues:
        issues_block = "\n".join(f"- {i}" for i in req.issues)
        issues_section = f"""
규칙 엔진이 아래 문제를 지적했습니다. 이미 내려진 판정이므로 <b>그대로 전제</b>하고 설명하십시오.

{issues_block}
"""
    else:
        # 위반이 없을 때 "문제 없습니다"라고 쓰게 하지 않는다 — 그것은 판정이고, 판정은 규칙 몫이다.
        issues_section = "\n규칙 엔진의 지적은 전달받지 않았습니다. 배치 내용만 설명하십시오.\n"

    return f"""당신은 OpenPlan 주간 계획을 사용자에게 설명하는 도우미입니다.

{data_json}
{issues_section}
지침:
- 한국어 평문으로 3~6문장. 목록·표·마크다운 없이 문단으로 쓰십시오.
- 언제 무엇이 얼마나 배치돼 있는지, 사용자가 알아야 할 것을 먼저 쓰십시오.
- 지적받은 문제가 있으면 그것이 왜 문제인지, 무엇을 하면 되는지 덧붙이십시오.
- <b>계획이 옳은지 그른지 스스로 판단하지 마십시오.</b> 지적받지 않은 것을 문제라고 하거나,
  문제가 없다고 단정하지 마십시오. 판정은 규칙 엔진이 이미 한 것만 전달합니다.
- 숫자를 지어내지 마십시오. 위 데이터에 있는 값만 쓰십시오.

설명문만 출력하십시오. 머리말·꼬리말·따옴표 없이 본문만 쓰십시오.
"""


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
        parsed, model, latency_ms = await _complete_json(
            self._router, prompt, Tier.COMPLEX, DRAFT_TIMEOUT_SECONDS
        )
        return _to_response(parsed, req.tasks_to_place, model=model, latency_ms=latency_ms)

    async def replan(self, req: ReplanRequest) -> ReplanResponse:
        """
        스냅샷 + 재계획 사유 → 대안 3종(COMPLEX 티어).

        초안과 같은 경계에 선다 — 만드는 것은 배치안이고, 그 안이 겹치는지·마감을 넘는지는
        Spring 규칙 엔진이 판정한다. 그래서 여기서 하는 검증도 전부 **형식**이다.

        KEEP_CURRENT(기준선)는 만들지 않는다. "안 바꾼 안"은 현재 스냅샷 그 자체라 생성할
        것이 없고, 그것까지 AI 에게 시키면 없는 변경을 지어낸다(계약도 행 미생성으로 정의).
        """
        prompt = _build_replan_prompt(req)
        parsed, model, latency_ms = await _complete_json(
            self._router, prompt, Tier.COMPLEX, REPLAN_TIMEOUT_SECONDS
        )
        return _to_replan_response(parsed, req, model=model, latency_ms=latency_ms)

    async def explain(self, req: ExplainRequest) -> ExplainResponse:
        """
        스냅샷 + 규칙 위반 사유 → 사용자용 설명(LIGHT 티어).

        <b>LIGHT 인 것이 중요하다.</b> 이 작업은 이미 내려진 판정을 말로 풀어내는 요약이지
        새로운 추론이 아니다. 경량 티어로 두면 로컬 Qwen 배선({@code LOCAL_MODEL})이 그대로
        받아 가므로, 상용 모델 호출 없이 도는 첫 기능이 된다.

        판정은 하지 않는다 — {@code issues} 가 비어 있으면 "문제 없음"을 <b>지어내지 않고</b>
        배치 내용만 설명한다. AI 가 "이 계획은 문제 없습니다"라고 말하는 순간 그것은 판정이다.
        """
        prompt = _build_explain_prompt(req)
        start = time.monotonic()
        result = await asyncio.wait_for(
            self._router.complete(prompt, Tier.LIGHT), timeout=EXPLAIN_TIMEOUT_SECONDS
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        explanation = _strip_code_fence(result.output).strip()
        if not explanation:
            raise ModelOutputError("모델이 빈 설명을 돌려주었습니다")
        return ExplainResponse(
            explanation=explanation,
            meta=PlanDraftMeta(model=result.model, latency_ms=latency_ms),
        )

    async def evaluate_task(self, *args, **kwargs):
        # W5: LIGHT 티어(분류·요약) — 로컬 Qwen 후보
        raise NotImplementedError("W5에서 구현")

    async def personalize(self, *args, **kwargs):
        # W5: 누적 이력(소요시간 편차·선호) 반영. LIGHT 티어 — 로컬 Qwen 후보
        raise NotImplementedError("W5에서 구현")
