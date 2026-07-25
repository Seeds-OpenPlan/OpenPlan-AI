"""환경 설정 — 전부 env(.env)에서 주입. 비밀키 하드코딩 없음."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# 모델 접두사 → LiteLLM 이 그 제공자에서 읽는 환경변수 이름.
# 제공자를 바꾸는 일 = .env 의 모델 문자열과 키를 바꾸는 일. 호출 코드는 안 바뀐다.
PROVIDER_ENV_BY_PREFIX: dict[str, str] = {
    "gemini/": "GEMINI_API_KEY",        # Google AI Studio (무료 티어 있음)
    "anthropic/": "ANTHROPIC_API_KEY",
    "openai/": "OPENAI_API_KEY",
    "ollama/": "",                      # 로컬 — 키 불필요
}


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
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # ── 티어별 모델 ────────────────────────────────────────
    # 기본값은 Google AI Studio 무료 티어(카드 불필요). 유료 제공자로 바꾸려면 .env 만 고친다.
    complex_model: str = "gemini/gemini-2.5-flash"       # 복합 추론: 계획 생성·재계획
    light_model: str = "gemini/gemini-2.5-flash-lite"    # 경량: 분류·요약

    # ── 로컬 (W5+, 지금 미사용). 설정되면 경량 티어가 로컬로 라우팅됨.
    local_model: str | None = None                       # 예: "ollama/qwen3:8b"
    ollama_base_url: str | None = None

    # ── 서비스 ─────────────────────────────────────────────
    port: int = 8000
    max_tokens: int = 1024

    def api_keys_by_env(self) -> dict[str, str]:
        """LiteLLM 이 읽는 환경변수 이름 → 설정값."""
        return {
            "GEMINI_API_KEY": self.gemini_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "OPENAI_API_KEY": self.openai_api_key,
        }


def get_settings() -> Settings:
    return Settings()
