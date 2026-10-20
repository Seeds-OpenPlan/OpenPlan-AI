"""
모델 라우터 = "콘센트".

작업 난이도(티어)에 따라 모델을 고르고 LiteLLM으로 호출한다. 티어→모델 매핑을 한 곳에
모아두므로, 상용↔로컬 전환은 **매핑(=env)만 바꾸면 되고 호출 코드는 안 바뀐다(드롭인)**.

- COMPLEX(복합 추론: 계획 생성·재계획) → 상용 (기본: Google AI Studio 무료 티어)
- LIGHT(경량: 분류·요약)            → LOCAL_MODEL 이 있으면 로컬 Qwen(Ollama), 실패 시 상용 폴백

제공자(Gemini·Claude·GPT)는 .env 의 모델 접두사로만 정해진다 — 이 파일은 제공자를 모른다.

주의: 이 서비스는 '초안 생성'만 한다. 계획의 적정성 **판정(검증)은 Spring 규칙엔진 소관**이다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

import litellm

from app.config import Settings

logger = logging.getLogger(__name__)


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
        # 🔴 LiteLLM 은 ollama 모델의 비용표가 없으면 **호출마다** /api/show 를 동기로 2~3번 부른다.
        #    그것도 api_base 가 아니라 기본값 localhost:11434 로 가서 매번 실패하고, 동기라 이벤트
        #    루프까지 막는다. 2026-10-20 실측(Windows → Modal): 본 요청 2.2초 + 조회 8초 = 10.7초.
        #    비용표에 미리 올려 두면 조회가 사라진다(2.1~2.9초).
        if settings.local_model and settings.local_model.startswith("ollama/"):
            litellm.register_model({settings.local_model: {
                "litellm_provider": "ollama",
                "mode": "chat",
                "input_cost_per_token": 0.0,
                "output_cost_per_token": 0.0,
            }})

    def model_for(self, tier: Tier) -> str:
        return self._model_by_tier[tier]

    async def complete(self, prompt: str, tier: Tier = Tier.COMPLEX) -> RouterResult:
        model = self.model_for(tier)
        if tier is not Tier.LIGHT or not self._settings.local_model:
            return await self._call(prompt, tier, model)

        # 로컬이 늦거나 죽으면 light_model 로 한 번 더 묻는다. 로컬 서버(Modal)는 0대로 내려갔다
        # 깨는 데 수십 초가 걸리고, 그동안 사용자를 세워 두거나 504 를 낼 이유가 없다 —
        # 같은 프롬프트를 상용 경량 모델이 1초 안팎에 받는다. 어느 쪽이 답했는지는 result.model 에 남는다.
        try:
            result = await asyncio.wait_for(
                self._call(prompt, tier, model), timeout=self._settings.local_timeout_seconds
            )
            if result.output.strip():
                return result
            logger.warning("로컬 모델이 빈 응답을 돌려줌 — %s 로 폴백", self._settings.light_model)
        except Exception as e:  # 타임아웃·연결 거부·401 모두 같은 길로 보낸다
            logger.warning("로컬 모델 실패(%s: %s) — %s 로 폴백",
                           type(e).__name__, e, self._settings.light_model)
        return await self._call(prompt, tier, self._settings.light_model)

    async def _call(self, prompt: str, tier: Tier, model: str) -> RouterResult:
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

        # 🔴 GLM(zai) 은 thinking 이 **기본 켜짐**이다(공식 문서 default: "enabled").
        #    끄지 않으면 아래 Qwen3 와 똑같이 max_tokens 를 추론에 먼저 써 /plans/draft 의 JSON 이
        #    문자열 중간에서 잘리고 502 가 된다 — 같은 함정을 제공자만 바꿔 세 번째로 밟는 자리다.
        #    제공자마다 이름만 다르다: Qwen3 는 think, gemini 는 reasoning_effort, GLM 은 thinking.
        #    🔴 LiteLLM 의 정식 thinking 파라미터를 쓰지 않고 extra_body 로 보내는 이유:
        #       glm-4.7-flash 는 받지만 **glm-4.5-flash 는 UnsupportedParamsError 로 거부**한다
        #       (2026-09-15 실측). 허용 여부가 LiteLLM 의 모델 표에 달려 있어 모델을 바꾸면
        #       조용히 깨진다. extra_body 는 검사 없이 그대로 실려 두 모델에서 같게 동작한다.
        if model.startswith("zai/"):
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        # 로컬(Ollama) 경로일 때만 base_url 전달
        if model.startswith("ollama/"):
            if self._settings.ollama_base_url:
                kwargs["api_base"] = self._settings.ollama_base_url
            # api_key 인자로 줘도 LiteLLM 이 같은 헤더를 만든다(1.91.4 main.py `_complete_ollama`).
            # 헤더를 직접 싣는 쪽을 택한 것은 그 변환이 버전마다 위치가 바뀌어 왔기 때문이고,
            # 이 방식이 Modal 프록시 인증을 통과하는 것은 2026-10-20 실측으로 확인했다.
            if self._settings.ollama_api_key:
                kwargs["extra_headers"] = {
                    "Authorization": f"Bearer {self._settings.ollama_api_key}"
                }
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
