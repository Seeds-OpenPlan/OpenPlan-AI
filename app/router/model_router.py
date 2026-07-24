"""
모델 라우터 = "콘센트".

작업 난이도(티어)에 따라 모델을 고르고 LiteLLM으로 호출한다. 티어→모델 매핑을 한 곳에
모아두므로, 상용↔로컬 전환은 **매핑(=env)만 바꾸면 되고 호출 코드는 안 바뀐다(드롭인)**.

- COMPLEX(복합 추론: 계획 생성·재계획) → 상용 Claude
- LIGHT(경량: 분류·요약)            → 현재 상용, W5+ 에서 로컬 Qwen(Ollama)으로 교체

주의: 이 서비스는 '초안 생성'만 한다. 계획의 적정성 **판정(검증)은 Spring 규칙엔진 소관**이다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import litellm

from app.config import Settings


class Tier(str, Enum):
    COMPLEX = "complex"
    LIGHT = "light"


@dataclass(frozen=True)
class RouterResult:
    tier: Tier
    model: str
    output: str


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model_by_tier: dict[Tier, str] = {
            Tier.COMPLEX: settings.complex_model,
            # local_model이 설정되면 경량은 자동으로 로컬로 라우팅(W5+). 없으면 상용 저가.
            Tier.LIGHT: settings.local_model or settings.light_model,
        }

    def model_for(self, tier: Tier) -> str:
        return self._model_by_tier[tier]

    async def complete(self, prompt: str, tier: Tier = Tier.COMPLEX) -> RouterResult:
        model = self.model_for(tier)
        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._settings.max_tokens,
        }
        # 로컬(Ollama) 경로일 때만 base_url 전달
        if model.startswith("ollama/") and self._settings.ollama_base_url:
            kwargs["api_base"] = self._settings.ollama_base_url

        resp = await litellm.acompletion(**kwargs)
        output = resp.choices[0].message.content or ""
        return RouterResult(tier=tier, model=model, output=output)
