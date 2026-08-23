"""
모델 라우터 = "콘센트".

작업 난이도(티어)에 따라 모델을 고르고 LiteLLM으로 호출한다. 티어→모델 매핑을 한 곳에
모아두므로, 상용↔로컬 전환은 **매핑(=env)만 바꾸면 되고 호출 코드는 안 바뀐다(드롭인)**.

- COMPLEX(복합 추론: 계획 생성·재계획) → 상용 (기본: Google AI Studio 무료 티어)
- LIGHT(경량: 분류·요약)            → 현재 상용, W5+ 에서 로컬 Qwen(Ollama)으로 교체

제공자(Gemini·Claude·GPT)는 .env 의 모델 접두사로만 정해진다 — 이 파일은 제공자를 모른다.

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
        # 🔴 상용 thinking 모델(gemini-3.x 등)도 max_tokens 를 추론에 먼저 쓴다 — 아래 Qwen 분기와
        #    같은 문제이고 해법만 다르다(상용은 API 파라미터가 think 가 아니라 reasoning_effort).
        #    끄지 않으면 /plans/draft 의 JSON 이 문자열 중간에서 잘려 502 가 되고, Spring 은 매번
        #    규칙으로 폴백한다 — "AI 를 켰는데 규칙만 도는" 증상이 되고 어디에도 실패로 안 남는다.
        #    2026-08-23 실측(gemini-3.6-flash · /plans/draft 실제 프롬프트 · max_tokens=4096):
        #      기본     20.4초 · 완료 2359(thinking 1914)  ← DRAFT_TIMEOUT 20초 초과
        #      minimal  10.9초 · 완료  502(thinking 없음)
        if not model.startswith("ollama/") and self._settings.reasoning_effort:
            kwargs["reasoning_effort"] = self._settings.reasoning_effort

        # 로컬(Ollama) 경로일 때만 base_url 전달
        if model.startswith("ollama/"):
            if self._settings.ollama_base_url:
                kwargs["api_base"] = self._settings.ollama_base_url
            # Qwen3 는 추론(thinking) 모델이라 기본값이 "<think> 블록을 먼저 생성"이다.
            # 끄지 않으면 max_tokens 를 추론에 다 쓰고 **본문이 빈 채로** 돌아온다.
            # 2026-08-11 실측(qwen3:1.7b, max_tokens=256, CPU):
            #   기본            25.8초 · 생성 256토큰(한도 소진) · 출력 ''   ← 빈 응답
            #   think=False      3.9초 · 생성  19토큰            · 정상
            #   프롬프트 /no_think 18.5초 · 생성 214토큰          · 정상이나 낭비
            # 프롬프트에 /no_think 를 붙이는 방식은 추론을 줄일 뿐 끄지 못한다 — API 파라미터를 쓴다.
            if "qwen3" in model:
                kwargs["extra_body"] = {"think": False}

        resp = await litellm.acompletion(**kwargs)
        output = resp.choices[0].message.content or ""
        return RouterResult(tier=tier, model=model, output=output)
