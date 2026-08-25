"""환경 설정 — 전부 env(.env)에서 주입. 비밀키 하드코딩 없음."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# 모델 접두사 → LiteLLM 이 그 제공자에서 읽는 환경변수 이름.
# 제공자를 바꾸는 일 = .env 의 모델 문자열과 키를 바꾸는 일. 호출 코드는 안 바뀐다.
PROVIDER_ENV_BY_PREFIX: dict[str, str] = {
    "gemini/": "GEMINI_API_KEY",        # Google AI Studio (무료 티어 있음)
    "groq/": "GROQ_API_KEY",            # Groq (무료 티어·카드 불필요) — reasoning_effort 주의, 아래 참조
    "anthropic/": "ANTHROPIC_API_KEY",
    "openai/": "OPENAI_API_KEY",
    "ollama/": "",                      # 로컬 — 키 불필요
}

# 🔴 제공자마다 reasoning_effort 가 받는 값이 다르다. 모르는 값을 보내면 호출이 통째로 거부되고,
#    Spring 은 그것을 "AI 없음" 으로 읽어 규칙 폴백한다 — 화면은 멀쩡한데 AI 가 아닌 상태가 된다.
#    2026-08-25 공식 문서 확인:
#      gemini/  … "minimal" 사용 중(실측 채택값)
#      groq/    … gpt-oss 계열은 "low" | "medium" | "high" 만. **"minimal" 은 무효**
#    제공자를 바꾸면 .env 의 REASONING_EFFORT 도 함께 보라. 확신이 없으면 빈 문자열로 두면
#    파라미터 자체를 붙이지 않는다(탈출구).
REASONING_EFFORT_VALUES_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "groq/": ("low", "medium", "high"),
}


def invalid_reasoning_effort(model: str, effort: str) -> str | None:
    """이 모델에 이 effort 를 보내면 거부되는가. 문제없으면 None, 아니면 사람이 읽을 사유.

    모르는 제공자는 통과시킨다 — 여기서 막는 것은 **알려진 불일치**뿐이다.
    """
    if not effort:
        return None
    for prefix, allowed in REASONING_EFFORT_VALUES_BY_PREFIX.items():
        if model.startswith(prefix) and effort not in allowed:
            return (
                f"모델 '{model}' 은 reasoning_effort='{effort}' 를 받지 않습니다 "
                f"(허용: {', '.join(allowed)}). .env 의 REASONING_EFFORT 를 고치거나 비우십시오."
            )
    return None


def required_env_for(model: str) -> str:
    """이 모델을 호출하려면 어떤 환경변수가 필요한가. 모르는 제공자면 빈 문자열."""
    for prefix, env_name in PROVIDER_ENV_BY_PREFIX.items():
        if model.startswith(prefix):
            return env_name
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── 상용 LLM 키 ────────────────────────────────────────
    # 쓰는 제공자의 것만 채우면 된다. LiteLLM 이 모델 접두사를 보고 알아서 고른다.
    gemini_api_key: str = ""
    groq_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # ── 티어별 모델 ────────────────────────────────────────
    # 기본값은 Google AI Studio 무료 티어(카드 불필요). 유료 제공자로 바꾸려면 .env 만 고친다.
    # 2026-07-25 실제 왕복 검증된 조합. 모델은 은퇴하면 404("no longer available to new users")가
    # 나므로, 실패하면 README 의 모델 목록 조회로 현재 가용 모델을 확인하고 갱신할 것.
    complex_model: str = "gemini/gemini-3.6-flash"        # 복합 추론: 계획 생성·재계획
    light_model: str = "gemini/gemini-3.5-flash-lite"     # 경량: 분류·요약

    # ── 로컬 (W5+, 지금 미사용). 설정되면 경량 티어가 로컬로 라우팅됨.
    local_model: str | None = None                       # 예: "ollama/qwen3:8b"
    ollama_base_url: str | None = None

    # ── 서비스 ─────────────────────────────────────────────
    port: int = 8000
    # 🔴 상용 thinking 모델은 이 예산을 추론에 먼저 쓴다. 1024 로는 /plans/draft 의 JSON 이
    #    문자열 중간에서 잘려 502 가 된다(2026-08-23 실측 — 완료 1020 중 thinking 984, 본문 80자).
    #    reasoning_effort=minimal 과 함께라면 502 토큰이면 끝나지만 여유를 둔다.
    max_tokens: int = 2048

    # 🔴 상용 모델의 추론(thinking) 강도. Qwen 의 think=False(model_router)와 같은 목적이고,
    #    상용 쪽은 이 파라미터를 쓴다. 2026-08-23 실측(gemini-3.6-flash, /plans/draft 실제 프롬프트):
    #      기본     20.4초 · 완료 2359(thinking 1914)  ← DRAFT_TIMEOUT 20초를 넘겨 폴백한다
    #      low      16.4초 · 완료 1455(thinking 1031)
    #      minimal  10.9초 · 완료  502(thinking 없음)  ← 채택
    #    계획 배치는 규칙 엔진이 다시 검증하므로(계약 §4) 모델의 긴 추론이 값을 더하지 않는다.
    #    빈 문자열이면 파라미터를 붙이지 않는다(추론 제어가 없는 모델·제공자용 탈출구).
    reasoning_effort: str = "minimal"

    def api_keys_by_env(self) -> dict[str, str]:
        """LiteLLM 이 읽는 환경변수 이름 → 설정값."""
        return {
            "GEMINI_API_KEY": self.gemini_api_key,
            "GROQ_API_KEY": self.groq_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "OPENAI_API_KEY": self.openai_api_key,
        }


def get_settings() -> Settings:
    return Settings()
