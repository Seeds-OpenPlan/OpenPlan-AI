"""
요청·응답 스키마.

W3: POST /plans/draft 의 스키마는 계약 정본
"05. Spring ↔ AI 서비스 REST 계약 초안.md" §3(요청)·§4(응답)을 그대로 따른다.
D-2(계약 §1) 에 따라 필드 구조는 Spring 쪽 규칙 엔진 타입(`rule/model/*.java`)과 동형이다 —
필드명·구조를 여기서 새로 발명하지 않는다(계약이 명시적으로 금지).

표기 규약(계약 §3)을 파이썬 타입 자체로 강제한다:
  - 시각(Instant)  → `InstantUTC`  : tz-aware + UTC 오프셋만 허용(그 외 422)
  - "HH:mm"        → `TimeHHMM`   : 정규식으로 초 단위·다른 구분자 차단
  - "YYYY-MM-DD"   → `date`        : pydantic 기본 ISO 파서가 정확히 이 형식만 받음
  - 요일           → `Weekday`     : Literal["MON",...,"SUN"] — 세 번째 표현이 원천 차단됨
  - ID             → `UUID`        : 형식이 아니면 422

필드 이름은 파이썬 관례상 snake_case 로 쓰되, `CamelModel` 의 alias_generator 가 자동으로
camelCase 별칭을 만든다 — 요청 파싱·응답 직렬화 모두 계약의 camelCase 그대로 나간다
(FastAPI/pydantic 기본이 별칭 우선이라 이중 관리가 필요 없다).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from pydantic.alias_generators import to_camel


class HealthResponse(BaseModel):
    status: str
    service: str


class PingRequest(BaseModel):
    prompt: str = Field(..., description="모델에 보낼 프롬프트 (뼈대 동작 검증용)")
    tier: str = Field("complex", description="complex(복합 추론) | light(경량 — W5+ 로컬 전환 대상)")


class PingResponse(BaseModel):
    tier: str
    model: str
    output: str


# ── /plans/draft 공용 타입 (계약 §3 표기 규약) ──────────────────────────────


def _require_utc(dt: datetime) -> datetime:
    # 계약: "시각: ISO-8601 UTC(Z)". +09:00 같은 비-UTC 오프셋도 유효한 Instant지만
    # 계약이 "UTC로 통일"을 명시했으므로 여기서 막는다 — Spring 조립 버그를 조기에 잡기 위함.
    if dt.utcoffset() != timedelta(0):
        raise ValueError("시각은 UTC(오프셋 +00:00 / 'Z')여야 합니다")
    return dt


InstantUTC = Annotated[datetime, AfterValidator(_require_utc)]

TimeHHMM = Annotated[
    str,
    StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$"),
]

Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

BlockTypeLiteral = Literal["TASK", "SCHEDULE"]


def _require_known_zone(zone: str) -> str:
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        raise ValueError(f"알 수 없는 IANA 타임존: {zone}")
    return zone


ZoneName = Annotated[str, AfterValidator(_require_known_zone)]


class CamelModel(BaseModel):
    """계약 필드는 camelCase. 파이썬 코드는 snake_case 로 쓰고 별칭만 camelCase로 낸다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ── 요청 — PlanSnapshot 은 rule/model/PlanSnapshot.java 와 동형(D-2) ────────


class BlockView(CamelModel):
    """이미 배치된 블록 (rule/model/BlockView 와 동형). 요청에만 존재 — 응답 쪽은 ProposedBlock(§4, blockId 없음)."""

    block_id: UUID
    type: BlockTypeLiteral
    task_id: UUID | None = None
    schedule_id: UUID | None = None
    start_at: InstantUTC
    end_at: InstantUTC


class FixedWindow(CamelModel):
    """rule/model/FixedWindow 와 동형. 주차 예외·INACTIVE 필터링은 Spring 조립 책임(가이드 §2) — 여기선 그대로 받는다."""

    fixed_schedule_id: UUID
    weekday: Weekday
    start_time: TimeHHMM
    end_time: TimeHHMM
    effective_from: date | None = None
    effective_to: date | None = None


class AvailabilityWindow(CamelModel):
    """rule/model/AvailabilityWindow 와 동형. 요일 7행."""

    weekday: Weekday
    start_time: TimeHHMM
    end_time: TimeHHMM
    active: bool


class TaskFacts(CamelModel):
    """rule/model/TaskFacts 와 동형. wbsStart/wbsEnd 는 WBS 미설정 태스크면 null(가이드 §1)."""

    due_date: date | None = None
    wbs_start: date | None = None
    wbs_end: date | None = None
    estimated_minutes: int
    priority: int


class PlanSnapshot(CamelModel):
    """rule/model/PlanSnapshot 과 동형(D-2). referenceTime 은 Spring이 주입 — 여기서도 now() 를 쓰지 않는다(계약 경계)."""

    week_start_date: date
    zone: ZoneName
    reference_time: InstantUTC
    blocks: list[BlockView] = Field(default_factory=list)
    active_fixed_schedules: list[FixedWindow] = Field(default_factory=list)
    availabilities: list[AvailabilityWindow] = Field(default_factory=list)
    task_facts: dict[UUID, TaskFacts] = Field(default_factory=dict)


class PlanDraftRequest(CamelModel):
    """POST /plans/draft 요청 (계약 §3). tasksToPlace 는 Spring이 명시(계약 §7 #3) — AI가 대상을 고르지 않는다."""

    snapshot: PlanSnapshot
    tasks_to_place: list[UUID]


# ── 응답 (계약 §4) ───────────────────────────────────────────────────────


class ProposedBlock(CamelModel):
    """
    응답 전용 — blockId 필드가 **없다**(계약 §4: "아직 저장되지 않은 제안이므로 ID는 Spring이
    저장 시 만든다"). extra="ignore" 는 모델(LLM) 원시 출력에 blockId 등 잉여 키가 섞여
    들어와도 조용히 버리기 위함(오케스트레이터가 이 스키마로 다시 검증할 때 사용) —
    "AI가 blockId 를 만들지 않는다"는 이 타입 자체가 구조로 강제한다.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    type: BlockTypeLiteral
    task_id: UUID | None = None
    schedule_id: UUID | None = None
    start_at: InstantUTC
    end_at: InstantUTC


class PlanDraftMeta(CamelModel):
    """관측용(계약 §4) — Spring은 로그만 하고 사용자에게 보이지 않는다. LLM에게 요청하지 않고 오케스트레이터가 채운다."""

    model: str
    latency_ms: int


class PlanDraftResponse(CamelModel):
    """
    POST /plans/draft 응답(200, 계약 §4).
    reason 은 필수·빈 문자열 금지(계약: "AI 제안도 근거를 동반한다" — 규칙 엔진 C-3와 같은 원칙).
    이 min_length 제약은 방어선의 마지막 층일 뿐이다 — 실제로는 오케스트레이터가 LLM 원시 출력
    단계에서 먼저 걸러 502로 응답한다(모델 응답 검증 실패를 500이 아니라 502로 매핑하기 위해).
    """

    proposed_blocks: list[ProposedBlock]
    unplaced_task_ids: list[UUID]
    reason: str = Field(..., min_length=1)
    meta: PlanDraftMeta


# ── 재계획 (SS-07/08/09 · POST /plans/replan) ─────────────────────────────

ReplanStrategyLiteral = Literal["MINIMAL_CHANGE", "DEADLINE_FIRST", "WORKLOAD_BALANCE"]
"""
AI 가 만드는 전략 3종.

Spring 계약(openapi ReplanOption)의 strategyType 은 KEEP_CURRENT 를 포함한 4종이지만,
KEEP_CURRENT 는 **현재 계획 그대로**라는 기준선이라 생성할 것이 없다(계약도 replanOptionId 를
null·행 미생성으로 정의한다). AI 에게 "안 바꾼 안"을 만들라고 시키면 없는 변경을 지어낼 수 있다.
"""


class ReplanRequest(CamelModel):
    """
    POST /plans/replan 요청.

    {@code tasksToPlace} 가 없는 것이 초안(draft)과의 차이다 — 재계획은 <b>이미 있는 배치를
    다시 짜는</b> 일이라 대상이 스냅샷의 blocks 자체다. 무엇 때문에 다시 짜는지는
    {@code trigger} 로 받는다: 그것이 없으면 모델이 "왜 바꾸는가"를 스스로 지어낸다.
    """

    snapshot: PlanSnapshot
    trigger: str = Field(..., min_length=1)


class ReplanOptionOut(CamelModel):
    """
    대안 하나 — Spring 의 ReplanOption 으로 그대로 옮겨진다.

    <b>score 가 없다.</b> 계약의 score 는 nullable 이고, LLM 에게 점수를 만들라고 하면
    근거 없는 수치가 사용자 화면에 뜬다. 순위가 필요하면 규칙 엔진이 낸 위반 수처럼
    **셀 수 있는 것**으로 Spring 이 계산해야 한다 — 이 프로젝트의 "수치 날조 금지"가
    LLM 출력에 특히 그대로 적용된다.
    """

    strategy_type: ReplanStrategyLiteral
    proposed_blocks: list[ProposedBlock]
    change_summary: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class ReplanResponse(CamelModel):
    """POST /plans/replan 응답. 전략 3종이 각각 하나씩 — 순서는 위 Literal 선언 순으로 고정한다."""

    options: list[ReplanOptionOut]
    meta: PlanDraftMeta


# ── 계획 설명 (POST /plans/explain) ───────────────────────────────────────


class ExplainRequest(CamelModel):
    """
    POST /plans/explain 요청.

    {@code issues} 는 <b>규칙 엔진이 낸 사유 문자열 그대로</b>다(ValidationIssue.reason).
    타입을 새로 만들지 않는 이유가 편의가 아니다 — 규칙 판정 스키마를 이 서비스가 다시 정의하면
    두 곳이 갈라지고, 갈라진 순간 AI 가 <b>판정을 흉내내기</b> 시작한다. 문자열로만 받으면
    AI 가 할 수 있는 일은 "이미 내려진 판정을 말로 풀어내는 것"뿐이다 — 경계가 타입으로 지켜진다.
    """

    snapshot: PlanSnapshot
    issues: list[str] = Field(default_factory=list)


class ExplainResponse(CamelModel):
    """POST /plans/explain 응답. 사용자에게 그대로 보여줄 한국어 설명 한 덩어리."""

    explanation: str = Field(..., min_length=1)
    meta: PlanDraftMeta
