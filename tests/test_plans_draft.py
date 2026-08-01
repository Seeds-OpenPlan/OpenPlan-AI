"""
POST /plans/draft 테스트 (계약 §3·§4).

모델 호출은 전부 목(mock)이다 — 실제 LLM API 키가 없어도 통과해야 한다(로컬 CI 부재,
키는 로컬 .env에만 있다는 전제). `client` 픽스처가 GEMINI_API_KEY 를 테스트용 더미 값으로
덮어써 503 분기를 피하고, 실제 네트워크 호출은 `router.complete` 를 monkeypatch 해서 막는다.

커버 범위(리드 지시 최소 커버 ⑴~⑺ + 형식 위반 추가 케이스):
  1. 정상 요청 → §4 응답 스키마 충족
  2. 스키마 위반 → 422 (FastAPI/pydantic 자동)
  3. 키 미설정 → 503
  4. 모델 호출 자체 실패(네트워크 등) → 502
  5. 타임아웃 → 504
  6. reason 빈 문자열 → 실패(502) 취급
  7. 응답에 blockId 없음(모델이 섞어 보내도 버려짐)
  8. 모델이 형식을 어긴 응답(비-JSON·필수 필드 누락·tasksToPlace 밖 taskId) → 502
"""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orchestrator import orchestrator as orchestrator_module
from app.router.model_router import RouterResult, Tier

TASK_ID = "11111111-1111-1111-1111-111111111111"


def _valid_payload() -> dict:
    return {
        "snapshot": {
            "weekStartDate": "2026-08-03",
            "zone": "Asia/Seoul",
            "referenceTime": "2026-08-01T09:00:00Z",
            "blocks": [],
            "activeFixedSchedules": [],
            "availabilities": [
                {"weekday": "MON", "startTime": "09:00", "endTime": "18:00", "active": True},
            ],
            "taskFacts": {
                TASK_ID: {
                    "dueDate": "2026-08-07",
                    "wbsStart": "2026-08-03",
                    "wbsEnd": "2026-08-06",
                    "estimatedMinutes": 60,
                    "priority": 2,
                }
            },
        },
        "tasksToPlace": [TASK_ID],
    }


@pytest.fixture
def client(monkeypatch):
    # 실제 .env 에 진짜 키가 있어도(없어도) 테스트는 항상 이 더미 값으로 503 분기를 피한다.
    # 503 케이스는 이 값을 delenv 로 다시 지워서 별도로 검증한다.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    with TestClient(app) as c:
        yield c


def _stub_complete(monkeypatch, client, output: str = None, exc: Exception = None, delay: float = 0.0):
    """router.complete 을 모델 호출 없이 대체한다."""

    async def fake_complete(prompt, tier=Tier.COMPLEX):
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return RouterResult(tier=Tier.COMPLEX, model="gemini/gemini-3.6-flash", output=output)

    monkeypatch.setattr(client.app.state.router, "complete", fake_complete)


# ── 1. 정상 요청 → §4 응답 스키마 충족 ──────────────────────────────────


def test_success_matches_response_contract(client, monkeypatch):
    output = json.dumps(
        {
            "proposedBlocks": [
                {
                    "type": "TASK",
                    "taskId": TASK_ID,
                    "scheduleId": None,
                    "startAt": "2026-08-04T00:00:00Z",
                    "endAt": "2026-08-04T01:00:00Z",
                }
            ],
            "unplacedTaskIds": [],
            "reason": "마감이 가까운 태스크를 가용 시간 앞쪽에 배치했습니다.",
        }
    )
    _stub_complete(monkeypatch, client, output=output)

    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"proposedBlocks", "unplacedTaskIds", "reason", "meta"}
    assert body["reason"] == "마감이 가까운 태스크를 가용 시간 앞쪽에 배치했습니다."
    assert body["unplacedTaskIds"] == []
    assert body["meta"]["model"] == "gemini/gemini-3.6-flash"
    assert isinstance(body["meta"]["latencyMs"], int)
    block = body["proposedBlocks"][0]
    assert block["taskId"] == TASK_ID
    assert block["startAt"].endswith("Z")
    assert "blockId" not in block  # §4: 저장 전 제안이라 ID 없음


# ── 2. 스키마 위반 → 422 ────────────────────────────────────────────────


def test_schema_violation_422_bad_weekday():
    with TestClient(app) as c:
        bad_payload = _valid_payload()
        bad_payload["snapshot"]["availabilities"][0]["weekday"] = "MONDAY"  # MON..SUN 아님
        resp = c.post("/plans/draft", json=bad_payload)
    assert resp.status_code == 422


def test_schema_violation_422_missing_tasks_to_place():
    with TestClient(app) as c:
        bad_payload = _valid_payload()
        del bad_payload["tasksToPlace"]
        resp = c.post("/plans/draft", json=bad_payload)
    assert resp.status_code == 422


def test_schema_violation_422_non_utc_instant():
    with TestClient(app) as c:
        bad_payload = _valid_payload()
        bad_payload["snapshot"]["referenceTime"] = "2026-08-01T09:00:00+09:00"  # UTC 아님
        resp = c.post("/plans/draft", json=bad_payload)
    assert resp.status_code == 422


# ── 3. 키 미설정 → 503 ──────────────────────────────────────────────────


def test_missing_api_key_503(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 503
    assert "detail" in resp.json()


# ── 4. 모델 호출 자체 실패 → 502 ─────────────────────────────────────────


def test_model_call_failure_502(client, monkeypatch):
    _stub_complete(monkeypatch, client, exc=RuntimeError("simulated network failure"))
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 502
    assert "detail" in resp.json()


# ── 5. 타임아웃 → 504 ────────────────────────────────────────────────────


def test_timeout_504(client, monkeypatch):
    monkeypatch.setattr(orchestrator_module, "DRAFT_TIMEOUT_SECONDS", 0.05)
    _stub_complete(monkeypatch, client, output="{}", delay=0.3)
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 504


# ── 6. reason 빈 문자열 → 실패(502) 취급 ─────────────────────────────────


def test_empty_reason_is_failure_502(client, monkeypatch):
    output = json.dumps({"proposedBlocks": [], "unplacedTaskIds": [TASK_ID], "reason": ""})
    _stub_complete(monkeypatch, client, output=output)
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 502


def test_whitespace_only_reason_is_failure_502(client, monkeypatch):
    output = json.dumps({"proposedBlocks": [], "unplacedTaskIds": [TASK_ID], "reason": "   "})
    _stub_complete(monkeypatch, client, output=output)
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 502


# ── 7. 응답에 blockId 없음 (모델이 섞어 보내도 버려짐) ────────────────────


def test_hallucinated_block_id_is_dropped(client, monkeypatch):
    output = json.dumps(
        {
            "proposedBlocks": [
                {
                    "blockId": "should-be-ignored",
                    "type": "TASK",
                    "taskId": TASK_ID,
                    "scheduleId": None,
                    "startAt": "2026-08-04T00:00:00Z",
                    "endAt": "2026-08-04T01:00:00Z",
                }
            ],
            "unplacedTaskIds": [],
            "reason": "이유",
        }
    )
    _stub_complete(monkeypatch, client, output=output)
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 200
    assert "blockId" not in resp.json()["proposedBlocks"][0]


# ── 8. 모델 출력 형식 위반 → 502 ─────────────────────────────────────────


def test_non_json_output_502(client, monkeypatch):
    _stub_complete(monkeypatch, client, output="이건 JSON이 아니라 그냥 문장입니다.")
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 502


def test_markdown_fenced_json_is_still_accepted(client, monkeypatch):
    inner = json.dumps({"proposedBlocks": [], "unplacedTaskIds": [TASK_ID], "reason": "가용 시간 부족"})
    _stub_complete(monkeypatch, client, output=f"```json\n{inner}\n```")
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 200


def test_missing_required_fields_in_model_output_502(client, monkeypatch):
    # proposedBlocks 만 있고 unplacedTaskIds·reason 누락
    _stub_complete(monkeypatch, client, output=json.dumps({"proposedBlocks": []}))
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 502


def test_malformed_block_in_model_output_502(client, monkeypatch):
    # startAt 누락
    output = json.dumps(
        {
            "proposedBlocks": [{"type": "TASK", "taskId": TASK_ID, "scheduleId": None, "endAt": "2026-08-04T01:00:00Z"}],
            "unplacedTaskIds": [],
            "reason": "이유",
        }
    )
    _stub_complete(monkeypatch, client, output=output)
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 502


def test_unknown_task_id_outside_tasks_to_place_502(client, monkeypatch):
    # tasksToPlace 에 없는 taskId 를 모델이 지어낸 경우
    output = json.dumps(
        {
            "proposedBlocks": [
                {
                    "type": "TASK",
                    "taskId": str(uuid4()),
                    "scheduleId": None,
                    "startAt": "2026-08-04T00:00:00Z",
                    "endAt": "2026-08-04T01:00:00Z",
                }
            ],
            "unplacedTaskIds": [],
            "reason": "이유",
        }
    )
    _stub_complete(monkeypatch, client, output=output)
    resp = client.post("/plans/draft", json=_valid_payload())
    assert resp.status_code == 502
