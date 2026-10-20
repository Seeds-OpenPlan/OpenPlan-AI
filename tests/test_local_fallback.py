"""
LIGHT 티어 로컬 경로 — 인증 헤더 배선과 상용 폴백.

로컬 서버(Modal)는 0대로 내려갔다 깨는 첫 요청이 35~96초였다(2026-10-20 실측). 폴백이
없으면 그동안의 설명 요청은 전부 504 다. 헤더가 빠지면 매 호출이 401 인데, 폴백이 있으면
**화면은 멀쩡하고 로컬만 조용히 안 쓰인다** — 그래서 둘 다 테스트로 고정한다.
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from app.config import Settings
from app.router.model_router import ModelRouter, Tier


def _response(content: str):
    class R:
        choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]

    return R()


def _settings(**overrides) -> Settings:
    base = dict(
        local_model="ollama/qwen3:4b-instruct",
        ollama_base_url="https://example.modal.run",
        ollama_api_key="wk-a.ws-b",
        light_model="groq/openai/gpt-oss-20b",
        local_timeout_seconds=0.05,
    )
    base.update(overrides)
    return Settings(**base)


def test_로컬_호출에_인증_헤더가_실린다(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _response("설명")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    result = asyncio.run(ModelRouter(_settings()).complete("p", Tier.LIGHT))

    assert result.model == "ollama/qwen3:4b-instruct"
    assert len(calls) == 1
    assert calls[0]["extra_headers"] == {"Authorization": "Bearer wk-a.ws-b"}
    assert calls[0]["api_base"] == "https://example.modal.run"


def test_키가_없으면_헤더를_싣지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """같은 PC 의 Ollama 처럼 인증 없는 서버에 빈 Bearer 를 보내지 않는다."""
    calls: list[dict] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _response("설명")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    asyncio.run(ModelRouter(_settings(ollama_api_key=None)).complete("p", Tier.LIGHT))

    assert "extra_headers" not in calls[0]


@pytest.mark.parametrize("failure", ["timeout", "error", "empty"])
def test_로컬이_실패하면_light_model_로_폴백한다(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    models: list[str] = []

    async def fake_acompletion(**kwargs):
        models.append(kwargs["model"])
        if kwargs["model"].startswith("ollama/"):
            if failure == "timeout":
                await asyncio.sleep(1)
            if failure == "error":
                raise RuntimeError("401 Unauthorized")
            return _response("   ")
        return _response("상용 설명")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    result = asyncio.run(ModelRouter(_settings()).complete("p", Tier.LIGHT))

    assert models == ["ollama/qwen3:4b-instruct", "groq/openai/gpt-oss-20b"]
    assert result.model == "groq/openai/gpt-oss-20b"
    assert result.output == "상용 설명"


def test_폴백_호출에는_로컬_헤더가_새지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """Modal 토큰이 상용 제공자로 실려 나가면 안 된다."""
    calls: list[dict] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        if kwargs["model"].startswith("ollama/"):
            raise RuntimeError("down")
        return _response("상용 설명")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    asyncio.run(ModelRouter(_settings()).complete("p", Tier.LIGHT))

    assert "extra_headers" not in calls[1]
    assert "api_base" not in calls[1]


def test_COMPLEX_는_로컬_설정과_무관하다(monkeypatch: pytest.MonkeyPatch) -> None:
    models: list[str] = []

    async def fake_acompletion(**kwargs):
        models.append(kwargs["model"])
        return _response("{}")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    asyncio.run(ModelRouter(_settings()).complete("p", Tier.COMPLEX))

    assert models == [Settings.model_fields["complex_model"].default]


def test_로컬_모델을_비용표에_올려_둔다() -> None:
    """빠지면 호출마다 /api/show 동기 조회가 붙어 2초짜리 응답이 10초가 된다(2026-10-20 실측)."""
    ModelRouter(_settings(local_model="ollama/qwen3:test-only"))

    # LiteLLM 은 제공자 접두사를 떼고 저장한다("qwen3:test-only").
    assert litellm.model_cost["qwen3:test-only"]["input_cost_per_token"] == 0.0
