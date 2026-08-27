"""
파싱 실패 시 한 번 다시 묻는지 — 2026-08-23 실측으로 들어온 요구.

같은 재계획 요청을 세 번 보냈더니 두 번은 정상이고 한 번이 `Expecting ',' delimiter` 로
깨졌다. 예산 부족이 아니었다(완료 1021 토큰 / 상한 2048) — 그냥 그날의 출력이 어긋난 것이다.

재시도가 없으면 그 한 번이 502 가 되고, **재계획은 규칙 폴백이 3전략을 만들지 못하므로**
사용자가 대안 화면을 아예 못 본다. 초안과 달리 대체할 것이 없다.
"""
import asyncio

import pytest

from app.orchestrator.orchestrator import ModelOutputError, _complete_json


class _FakeRouter:
    """지정한 순서대로 출력을 돌려준다. 호출 횟수를 센다."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    async def complete(self, prompt, tier):
        self.calls += 1
        out = self._outputs.pop(0)

        class R:
            output = out
            model = "fake/model"

        return R()


def test_첫_시도가_깨지면_다시_묻는다():
    router = _FakeRouter(['{"broken": ', '{"ok": true}'])

    parsed, model, latency = asyncio.run(_complete_json(router, "p", "COMPLEX", 5.0))

    assert parsed == {"ok": True}
    assert router.calls == 2, "한 번 더 물었어야 한다"
    assert model == "fake/model"


def test_첫_시도가_정상이면_다시_묻지_않는다():
    router = _FakeRouter(['{"ok": true}'])

    parsed, _, _ = asyncio.run(_complete_json(router, "p", "COMPLEX", 5.0))

    assert parsed == {"ok": True}
    assert router.calls == 1, "정상인데 또 물으면 한도만 태운다"


def test_두_번_다_깨지면_포기한다():
    """무한 재시도는 하지 않는다 — 계속 같은 형식으로 실패하면 프롬프트 문제이지 운이 아니다."""
    router = _FakeRouter(['{"broken": ', 'also broken'])

    with pytest.raises(ModelOutputError):
        asyncio.run(_complete_json(router, "p", "COMPLEX", 5.0))

    assert router.calls == 2, "두 번까지만"


def test_JSON_객체가_아니면_재시도_대상이다():
    """배열이나 문자열이 와도 같은 취급 — 계약은 객체를 요구한다."""
    router = _FakeRouter(['[1, 2, 3]', '{"ok": true}'])

    parsed, _, _ = asyncio.run(_complete_json(router, "p", "COMPLEX", 5.0))

    assert parsed == {"ok": True}
    assert router.calls == 2
