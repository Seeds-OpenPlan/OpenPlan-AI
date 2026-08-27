"""
재계획·설명 서브에이전트 (POST /plans/replan · /plans/explain).

`test_plans_draft.py` 와 같은 방식 — 더미 키로 503 분기를 피하고 `router.complete` 를
monkeypatch 해서 실제 네트워크 호출을 막는다. 검증하는 것은 **오케스트레이터의 형식 검증과
경계**이지 모델의 답이 아니다.

경계(엄수): 만드는 건 AI, 판정은 규칙. 아래 테스트들은 그 경계가 코드로 지켜지는지를 본다 —
전략 3종이 요청한 모양으로 오는가, 스냅샷 밖 ID 를 지어내지 않는가, 설명이 판정을 흉내내지 않는가.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings, required_env_for
from app.main import app
from app.router.model_router import RouterResult, Tier

TASK_A = "11111111-1111-1111-1111-111111111111"
TASK_B = "22222222-2222-2222-2222-222222222222"
STRATEGIES = ["MINIMAL_CHANGE", "DEADLINE_FIRST", "WORKLOAD_BALANCE"]


def _snapshot() -> dict:
    return {
        "weekStartDate": "2026-08-03",
        "zone": "Asia/Seoul",
        "referenceTime": "2026-08-01T09:00:00Z",
        "blocks": [
            {
                "blockId": "aaaaaaaa-0000-0000-0000-000000000000",
                "type": "TASK",
                "taskId": TASK_A,
                "scheduleId": None,
                "startAt": "2026-08-03T01:00:00Z",
                "endAt": "2026-08-03T02:00:00Z",
            }
        ],
        "activeFixedSchedules": [],
        "availabilities": [
            {"weekday": "MON", "startTime": "09:00", "endTime": "18:00", "active": True},
            {"weekday": "TUE", "startTime": "09:00", "endTime": "18:00", "active": True},
        ],
        "taskFacts": {
            TASK_A: {
                "dueDate": "2026-08-07",
                "wbsStart": "2026-08-03",
                "wbsEnd": "2026-08-06",
                "estimatedMinutes": 60,
                "priority": 2,
            },
            TASK_B: {
                "dueDate": "2026-08-05",
                "wbsStart": "2026-08-03",
                "wbsEnd": "2026-08-05",
                "estimatedMinutes": 30,
                "priority": 3,
            },
        },
    }


def _replan_payload() -> dict:
    return {"snapshot": _snapshot(), "trigger": "화요일에 갑자기 회의가 잡혔습니다."}


def _explain_payload(issues: list[str] | None = None) -> dict:
    payload: dict = {"snapshot": _snapshot()}
    if issues is not None:
        payload["issues"] = issues
    return payload


def _option(strategy: str, task_id: str = TASK_A) -> dict:
    return {
        "strategyType": strategy,
        "proposedBlocks": [
            {
                "type": "TASK",
                "taskId": task_id,
                "scheduleId": None,
                "startAt": "2026-08-04T01:00:00Z",
                "endAt": "2026-08-04T02:00:00Z",
            }
        ],
        "changeSummary": f"{strategy} 전략으로 옮깁니다.",
        "reason": "화요일 회의를 피해 월요일로 당깁니다.",
    }


@pytest.fixture
def client(monkeypatch):
    # 🔴 제공자 이름을 박지 않는다 — 설정된 모델에서 키 이름을 유도한다. 박아 두면 제공자를
    #    갈아탄 순간 이 더미 주입이 무효가 되고, 단위 테스트가 **진짜 API 를 호출**한다.
    #    (2026-08-25 Gemini→Groq 전환에서 실제로 겪음)
    for model in {get_settings().complex_model, get_settings().light_model}:
        key_env = required_env_for(model)
        if key_env:
            monkeypatch.setenv(key_env, "test-key-not-real")
    with TestClient(app) as c:
        yield c


def _stub(monkeypatch, client, output: str = None, exc: Exception = None, delay: float = 0.0):
    """router.complete 대체. 호출된 티어를 기록해 라우팅까지 검증할 수 있게 한다."""
    seen: dict = {}

    async def fake_complete(prompt, tier=Tier.COMPLEX):
        seen["tier"] = tier
        seen["prompt"] = prompt
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return RouterResult(tier=tier, model="stub/model", output=output)

    monkeypatch.setattr(client.app.state.router, "complete", fake_complete)
    return seen


# ═══════════════════ 재계획 ═══════════════════


def test_replan_success_returns_three_strategies(client, monkeypatch):
    _stub(monkeypatch, client, output=json.dumps({"options": [_option(s) for s in STRATEGIES]}))

    r = client.post("/plans/replan", json=_replan_payload())

    assert r.status_code == 200
    body = r.json()
    assert [o["strategyType"] for o in body["options"]] == STRATEGIES
    assert body["meta"]["model"] == "stub/model"
    assert all(o["changeSummary"] and o["reason"] for o in body["options"])


def test_replan_order_is_fixed_regardless_of_model_order(client, monkeypatch):
    """모델이 낸 순서에 화면 순서가 흔들리면 같은 요청이 매번 다르게 보인다."""
    shuffled = ["WORKLOAD_BALANCE", "MINIMAL_CHANGE", "DEADLINE_FIRST"]
    _stub(monkeypatch, client, output=json.dumps({"options": [_option(s) for s in shuffled]}))

    r = client.post("/plans/replan", json=_replan_payload())

    assert r.status_code == 200
    assert [o["strategyType"] for o in r.json()["options"]] == STRATEGIES


def test_replan_rejects_missing_strategy(client, monkeypatch):
    """하나가 빠지면 대안 비교 화면에서 고를 것이 줄어든다 — 형식 위반으로 502."""
    _stub(monkeypatch, client, output=json.dumps({"options": [_option(s) for s in STRATEGIES[:2]]}))

    r = client.post("/plans/replan", json=_replan_payload())

    assert r.status_code == 502
    assert "전략 3종" in r.json()["detail"]


def test_replan_rejects_duplicate_strategy(client, monkeypatch):
    dup = ["MINIMAL_CHANGE", "MINIMAL_CHANGE", "DEADLINE_FIRST"]
    _stub(monkeypatch, client, output=json.dumps({"options": [_option(s) for s in dup]}))

    r = client.post("/plans/replan", json=_replan_payload())

    assert r.status_code == 502


def test_replan_rejects_unknown_task_id(client, monkeypatch):
    """스냅샷에 없는 taskId 를 그대로 Spring 에 넘기면 저장 단계에서 조회 실패가 된다."""
    ghost = "99999999-9999-9999-9999-999999999999"
    options = [_option(s) for s in STRATEGIES]
    options[0]["proposedBlocks"][0]["taskId"] = ghost
    _stub(monkeypatch, client, output=json.dumps({"options": options}))

    r = client.post("/plans/replan", json=_replan_payload())

    assert r.status_code == 502
    assert ghost in r.json()["detail"]


def test_replan_rejects_blank_change_summary(client, monkeypatch):
    """근거 없는 대안은 사용자가 고를 수 없다 — 초안의 reason 규약과 같은 원칙(C-3)."""
    options = [_option(s) for s in STRATEGIES]
    options[1]["changeSummary"] = "   "
    _stub(monkeypatch, client, output=json.dumps({"options": options}))

    assert client.post("/plans/replan", json=_replan_payload()).status_code == 502


def test_replan_uses_complex_tier(client, monkeypatch):
    seen = _stub(monkeypatch, client, output=json.dumps({"options": [_option(s) for s in STRATEGIES]}))

    client.post("/plans/replan", json=_replan_payload())

    assert seen["tier"] == Tier.COMPLEX


def test_replan_prompt_carries_trigger(client, monkeypatch):
    """재계획 사유가 프롬프트에 없으면 모델이 '왜 바꾸는가'를 스스로 지어낸다."""
    seen = _stub(monkeypatch, client, output=json.dumps({"options": [_option(s) for s in STRATEGIES]}))

    client.post("/plans/replan", json=_replan_payload())

    assert "화요일에 갑자기 회의가 잡혔습니다." in seen["prompt"]


def test_replan_timeout_maps_to_504(client, monkeypatch):
    _stub(monkeypatch, client, exc=asyncio.TimeoutError())

    assert client.post("/plans/replan", json=_replan_payload()).status_code == 504


def test_replan_requires_trigger(client):
    """trigger 는 빈 문자열 금지 — pydantic 이 422 로 끊는다."""
    payload = _replan_payload()
    payload["trigger"] = ""

    assert client.post("/plans/replan", json=payload).status_code == 422


# ═══════════════════ 설명 ═══════════════════


def test_explain_success_returns_text(client, monkeypatch):
    _stub(monkeypatch, client, output="월요일 오전에 60분짜리 작업이 하나 있습니다. 나머지 요일은 비어 있습니다.")

    r = client.post("/plans/explain", json=_explain_payload())

    assert r.status_code == 200
    body = r.json()
    assert body["explanation"].startswith("월요일")
    assert body["meta"]["model"] == "stub/model"


def test_explain_uses_light_tier(client, monkeypatch):
    """LIGHT 라야 로컬 Qwen 배선(LOCAL_MODEL)이 그대로 받아 간다 — 상용 호출 없이 도는 첫 기능."""
    seen = _stub(monkeypatch, client, output="설명입니다.")

    client.post("/plans/explain", json=_explain_payload())

    assert seen["tier"] == Tier.LIGHT


def test_explain_passes_issues_verbatim(client, monkeypatch):
    """규칙 판정은 문자열 그대로 전달된다 — AI 가 판정을 다시 만들지 않는다."""
    issue = "월요일 배치 시간이 가용 시간을 초과했습니다. (가용 60분 / 배치 90분, 30분 초과)"
    seen = _stub(monkeypatch, client, output="설명입니다.")

    client.post("/plans/explain", json=_explain_payload([issue]))

    assert issue in seen["prompt"]


def test_explain_without_issues_does_not_claim_no_problem(client, monkeypatch):
    """위반이 없을 때 '문제 없다'고 쓰게 하면 그것이 판정이 된다 — 프롬프트가 그것을 금지해야 한다."""
    seen = _stub(monkeypatch, client, output="설명입니다.")

    client.post("/plans/explain", json=_explain_payload([]))

    assert "배치 내용만 설명하십시오" in seen["prompt"]
    assert "문제가 없다고 단정하지 마십시오" in seen["prompt"]


def test_explain_rejects_empty_output(client, monkeypatch):
    """빈 설명을 200 으로 돌려주면 화면에 빈 칸이 뜬다 — 502 로 끊는다."""
    _stub(monkeypatch, client, output="   ")

    r = client.post("/plans/explain", json=_explain_payload())

    assert r.status_code == 502


def test_explain_strips_code_fence(client, monkeypatch):
    """모델이 코드펜스를 붙여 보내도 사용자에게 ``` 이 보이면 안 된다."""
    _stub(monkeypatch, client, output="```\n월요일에 작업이 하나 있습니다.\n```")

    r = client.post("/plans/explain", json=_explain_payload())

    assert r.status_code == 200
    assert "```" not in r.json()["explanation"]


def test_explain_timeout_maps_to_504(client, monkeypatch):
    _stub(monkeypatch, client, exc=asyncio.TimeoutError())

    assert client.post("/plans/explain", json=_explain_payload()).status_code == 504


def test_explain_issues_default_to_empty(client, monkeypatch):
    """issues 를 안 보내도 동작해야 한다 — 규칙 검증 전에 설명을 보는 화면이 있을 수 있다."""
    _stub(monkeypatch, client, output="설명입니다.")

    assert client.post("/plans/explain", json={"snapshot": _snapshot()}).status_code == 200
