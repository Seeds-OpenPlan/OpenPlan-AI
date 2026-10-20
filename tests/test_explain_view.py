"""
설명 프롬프트의 재료 — 한국 시간·작업 번호로 바뀌어 실리는지.

UTC 를 그대로 실었을 때 두 모델(qwen3:4b·gpt-oss-20b) 모두 10시 블록을 "오전 1시"라고 쓰고,
UUID 를 작업 이름처럼 본문에 옮겼다(2026-10-20 실측). 표현이 다시 새면 같은 글이 나온다.
"""
from __future__ import annotations

from tests.test_replan_explain import TASK_A, _explain_payload

from app.models.schemas import ExplainRequest
from app.orchestrator.orchestrator import _build_explain_prompt, _explain_view


def _req(issues=None) -> ExplainRequest:
    return ExplainRequest.model_validate(_explain_payload(issues))


def test_블록_시각이_한국_시간으로_실린다() -> None:
    view = _explain_view(_req().snapshot)

    # 2026-08-03T01:00Z = 한국 시간 월요일 10:00
    assert view["배치된 블록"][0]["날짜"] == "8월 3일(월)"
    assert view["배치된 블록"][0]["시간"] == "10:00~11:00"


def test_작업은_번호로만_실린다() -> None:
    view = _explain_view(_req().snapshot)

    assert view["배치된 블록"][0]["작업"] == "작업 1"
    assert set(view["작업"]) == {"작업 1", "작업 2"}


def test_프롬프트에_UUID_와_UTC_표기가_없다() -> None:
    prompt = _build_explain_prompt(_req(["화요일 학습 시간이 하루 최대치를 넘습니다."]))

    assert TASK_A not in prompt
    assert "T01:00:00Z" not in prompt
    assert "화요일 학습 시간이 하루 최대치를 넘습니다." in prompt
