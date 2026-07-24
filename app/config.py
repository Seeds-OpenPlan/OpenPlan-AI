"""환경 설정 — 전부 env(.env)에서 주입. 비밀키 하드코딩 없음."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 상용 (Anthropic)
    anthropic_api_key: str = ""
    complex_model: str = "anthropic/claude-sonnet-5"     # 복합 추론
    light_model: str = "anthropic/claude-haiku-4-5"      # 경량(분류·요약) — 현재 상용

    # 로컬 (W5+, 지금 미사용). 설정되면 경량 티어가 로컬로 라우팅됨.
    local_model: str | None = None                       # 예: "ollama/qwen3:8b"
    ollama_base_url: str | None = None

    # 서비스
    port: int = 8000
    max_tokens: int = 1024


def get_settings() -> Settings:
    return Settings()
