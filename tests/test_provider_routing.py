"""제공자 배선 회귀 — 키 검사와 reasoning_effort 불일치.

여기서 고정하는 것은 **모델 품질이 아니라 배선**이다. 제공자를 갈아탈 때 조용히 깨지는
자리가 둘 있고, 둘 다 증상이 "AI 가 없는 것처럼 보인다" 로 같아서 화면으로는 안 보인다.

1. 접두사가 매핑에 없으면 `required_env_for` 가 빈 문자열을 돌려준다 → main 의 키 검사가
   **통째로 건너뛰어진다** → 키가 없어도 503 이 아니라 502(LLM 호출 실패)로 나온다.
2. 제공자가 안 받는 reasoning_effort 를 보내면 매 호출이 거부되는데, Spring 은 그것을
   "AI 없음" 으로 읽어 규칙 폴백한다 — 어디에도 실패로 안 남는다.
"""
from __future__ import annotations

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
