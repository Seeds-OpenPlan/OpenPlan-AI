"""제공자 배선 회귀 — 키 검사와 reasoning_effort 불일치.

여기서 고정하는 것은 **모델 품질이 아니라 배선**이다. 제공자를 갈아탈 때 조용히 깨지는
자리가 둘 있고, 둘 다 증상이 "AI 가 없는 것처럼 보인다" 로 같아서 화면으로는 안 보인다.

1. 접두사가 매핑에 없으면 `required_env_for` 가 빈 문자열을 돌려준다 → main 의 키 검사가
   **통째로 건너뛰어진다** → 키가 없어도 503 이 아니라 502(LLM 호출 실패)로 나온다.
2. 제공자가 안 받는 reasoning_effort 를 보내면 매 호출이 거부되는데, Spring 은 그것을
   "AI 없음" 으로 읽어 규칙 폴백한다 — 어디에도 실패로 안 남는다.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import (
    PROVIDER_ENV_BY_PREFIX,
    Settings,
    invalid_reasoning_effort,
    required_env_for,
)


@pytest.mark.parametrize(
    "model, expected_env",
    [
        ("groq/openai/gpt-oss-120b", "GROQ_API_KEY"),
        ("groq/openai/gpt-oss-20b", "GROQ_API_KEY"),
        ("gemini/gemini-3.6-flash", "GEMINI_API_KEY"),
        ("zai/glm-4.7-flash", "ZAI_API_KEY"),
        ("zai/glm-4.5-flash", "ZAI_API_KEY"),
        ("ollama/qwen3:8b", ""),  # 로컬은 키 불필요
    ],
)
def test_키_검사가_제공자를_안다(model: str, expected_env: str) -> None:
    """매핑에 없으면 빈 문자열이 나오고, main 의 키 검사가 조용히 건너뛰어진다."""
    assert required_env_for(model) == expected_env


def test_groq_키가_환경변수_다리에_실린다() -> None:
    """lifespan 이 이 사전을 돌며 os.environ 에 넣는다. 빠지면 키를 넣어도 LiteLLM 이 못 본다.

    🔴 사전 전체를 단언에 넣지 않는다 — 실패하면 다른 제공자의 **실제 키까지 로그에 찍힌다.**
    """
    bridged = Settings(groq_api_key="gsk_test").api_keys_by_env()
    assert "GROQ_API_KEY" in bridged, "GROQ_API_KEY 가 환경변수 다리에 없다"
    assert bridged["GROQ_API_KEY"] == "gsk_test"


def test_groq_는_minimal_을_거부한다() -> None:
    """Gemini 에서 채택한 값을 그대로 들고 Groq 로 넘어가면 매 호출이 거부된다."""
    reason = invalid_reasoning_effort("groq/openai/gpt-oss-120b", "minimal")
    assert reason is not None
    assert "minimal" in reason
    assert "low" in reason  # 사람이 바로 고칠 수 있게 허용값을 알려준다


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_groq_허용값은_통과한다(effort: str) -> None:
    assert invalid_reasoning_effort("groq/openai/gpt-oss-120b", effort) is None


def test_빈_값은_언제나_통과한다() -> None:
    """빈 문자열이면 라우터가 파라미터 자체를 안 붙인다 — 모르는 제공자용 탈출구."""
    assert invalid_reasoning_effort("groq/openai/gpt-oss-120b", "") is None


def test_모르는_제공자는_막지_않는다() -> None:
    """여기서 막는 것은 **알려진 불일치**뿐이다. 문서가 낡아 우리가 틀릴 수도 있다."""
    assert invalid_reasoning_effort("cerebras/gpt-oss-120b", "minimal") is None


def test_기본값이_1024_로_돌아가지_않는다() -> None:
    """1024 면 상용 thinking 모델이 예산을 추론에 다 써 JSON 이 중간에서 잘린다.

    2026-08-23 에 /plans/draft 가 502 로, 2026-08-24 에 /plans/replan 이 char 2062 에서
    잘린 것이 같은 원인이었다. 코드 기본값이 다시 내려가면 여기서 잡는다.

    🔴 `Settings()` 를 만들어 검사하지 않는다 — 그러면 로컬 `.env` 를 읽어 환경마다 결과가
    달라지고, 무엇보다 **실패 메시지에 Settings 전체가 찍혀 API 키가 로그에 남는다.**
    여기서 고정할 것은 코드의 기본값 하나뿐이므로 필드 정의만 본다.
    """
    default = Settings.model_fields["max_tokens"].default
    assert default >= 2048, f"코드 기본 max_tokens 가 {default} 로 내려갔다"


def test_문서화된_제공자는_전부_키_이름을_갖는다() -> None:
    """로컬(ollama)만 예외다. 새 제공자를 넣고 키 이름을 빼면 검사가 무력해진다."""
    for prefix, env_name in PROVIDER_ENV_BY_PREFIX.items():
        if prefix == "ollama/":
            assert env_name == ""
        else:
            assert env_name, f"{prefix} 에 키 환경변수 이름이 없다"


def test_zai_키가_환경변수_다리에_실린다() -> None:
    """빠지면 .env 에 키를 넣어도 LiteLLM 이 못 본다 — 인증 실패 → 503 → 조용한 규칙 폴백."""
    bridged = Settings(zai_api_key="zai_test").api_keys_by_env()
    assert "ZAI_API_KEY" in bridged, "ZAI_API_KEY 가 환경변수 다리에 없다"
    assert bridged["ZAI_API_KEY"] == "zai_test"


@pytest.mark.parametrize("effort", ["low", "minimal", "high"])
def test_zai_는_어떤_effort_도_거부한다(effort: str) -> None:
    """2026-09-15 실측 — LiteLLM 이 UnsupportedParamsError 로 호출 전에 막는다.

    groq 와 달리 "맞는 값" 이 없다. 그래서 메시지가 허용값을 나열하는 대신 비우라고 말해야 한다.
    """
    reason = invalid_reasoning_effort("zai/glm-4.7-flash", effort)
    assert reason is not None
    assert "어떤 값도 받지 않습니다" in reason, f"고치는 방법이 안 적혀 있다: {reason}"


def test_zai_도_빈_값은_통과한다() -> None:
    assert invalid_reasoning_effort("zai/glm-4.5-flash", "") is None


def test_코드_기본값끼리_모순이_없다() -> None:
    """기본 모델과 기본 reasoning_effort 가 서로 맞는가.

    제공자를 갈아탈 때 **둘 중 하나만 고치는 것**이 이 저장소에서 반복된 실수다(D-87).
    어긋나면 매 호출이 거부되는데 Spring 이 규칙 폴백해서 화면은 멀쩡하다 — 그래서 테스트가 본다.

    🔴 `Settings()` 를 만들지 않는다 — 로컬 `.env` 를 읽어 환경마다 결과가 달라지고,
    실패 메시지에 Settings 전체가 찍혀 API 키가 로그에 남는다. 필드 기본값만 본다.
    """
    fields = Settings.model_fields
    effort = fields["reasoning_effort"].default
    for key in ("complex_model", "light_model"):
        model = fields[key].default
        reason = invalid_reasoning_effort(model, effort)
        assert reason is None, f"코드 기본값이 서로 어긋난다 — {key}={model}: {reason}"


def test_GLM_은_thinking_을_끄고_호출한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """GLM 은 thinking 이 기본 켜짐이라, 안 끄면 max_tokens 를 추론에 먼저 쓰고 JSON 이 잘린다.

    Qwen3(2026-08-11)·gemini(2026-08-23)에 이어 **같은 함정의 세 번째 제공자**다. 앞의 둘은
    실서버에서 하루씩 태우고 나서야 원인이 잡혔으므로, 여기서는 배선을 테스트로 고정한다.
    """
    import litellm

    from app.config import Settings
    from app.router.model_router import ModelRouter, Tier

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)

        class R:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

        return R()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    settings = Settings(complex_model="zai/glm-4.7-flash", reasoning_effort="")
    asyncio.run(ModelRouter(settings).complete("p", Tier.COMPLEX))

    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}, (
        f"GLM 인데 thinking 을 안 껐다: {captured.get('extra_body')}"
    )
    assert "reasoning_effort" not in captured, "zai 는 이 파라미터를 받지 않는다"
