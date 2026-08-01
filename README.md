# OpenPlan AI 서비스 (W3: 계획 초안 생성 동작)

AI 계획 생성을 담당하는 **별도 Python 서비스**. Spring 백엔드가 REST로 호출한다.

> **경계 (엄수): 만드는 건 AI, 판정은 규칙.**
> 이 서비스는 계획 **초안 생성**만 한다. 적정성 **검증(판정)은 Spring 규칙엔진** 소관.
> 흐름: `사용자 요청 → (이 서비스) AI 초안 → Spring 규칙검증 → 사용자 확정 → 저장`

현재 범위(W3): **AI 계획 초안 생성(`POST /plans/draft`)이 동작한다** — 스냅샷 + 배치할
태스크 목록을 받아 초안을 제안한다. 재계획·설명(`replan`·`explain`, W4)과 태스크평가·개인화
(`evaluate_task`·`personalize`, W5)는 아직 스텁이다.

## 구조
```
OpenPlan-AI/
├── requirements.txt          의존성 (앱 + 테스트)
├── .env.example               환경변수 템플릿 (실제 값은 로컬 .env 에만)
├── app/
│   ├── config.py              env 설정 (제공자별 키·티어별 모델·포트)
│   ├── main.py                FastAPI 앱 · /health · /ping · /plans/draft
│   ├── models/schemas.py      요청·응답 스키마 (PlanSnapshot 등은 Spring rule/model/* 와 동형)
│   ├── router/model_router.py "콘센트" — 티어→모델 라우팅 (상용/로컬)
│   └── orchestrator/orchestrator.py  메인 에이전트 — generate_plan(계획 초안, W3) 구현.
│       replan·explain(W4)·evaluate_task·personalize(W5)는 아직 스텁
└── tests/test_plans_draft.py  /plans/draft 테스트 — 모델 호출은 전부 monkeypatch, API 키 불필요
```

## 제공자 선택 — 코드가 아니라 `.env` 가 정한다

`ModelRouter`는 제공자를 모른다. **모델 문자열의 접두사**가 제공자를 결정하고, LiteLLM이 그에 맞는 환경변수를 읽는다.

| 접두사 | 제공자 | 필요한 키 |
| --- | --- | --- |
| `gemini/` | Google AI Studio | `GEMINI_API_KEY` |
| `anthropic/` | Anthropic | `ANTHROPIC_API_KEY` |
| `openai/` | OpenAI | `OPENAI_API_KEY` |
| `ollama/` | 로컬 (W5+) | 없음 |

**기본값은 Google AI Studio 무료 티어**다(카드 등록 없이 시작할 수 있어 W2~W4 개발용으로 적합).
유료 제공자로 갈아탈 때 고치는 건 `.env` 뿐이고 호출 코드는 그대로다.

> ⚠️ **무료 티어의 대가**: Google AI Studio 무료 티어는 EEA·EU·영국·스위스 **밖**에서는
> 프롬프트와 출력이 모델 학습에 사용된다. 지금처럼 시드·더미 데이터로 개발할 때는 괜찮지만,
> **실제 사용자 일정 데이터가 들어가는 시점에는 유료 키나 로컬 모델로 바꿔야 한다.**
> (W5의 "경량·개인정보 티어는 로컬 Qwen" 방향과 연결된다)

## 준비 — Google AI Studio 키 발급

1. `https://aistudio.google.com/apikey` 접속 → Google 계정 로그인
2. **Create API key** → 프로젝트 선택(없으면 새로 생성)
3. 발급된 키(`AIza...`)를 복사 → 로컬 `.env` 의 `GEMINI_API_KEY` 에 붙여넣기

카드 등록이 필요 없다. 무료 티어에는 분당·일일 요청 한도가 있으므로, W3에서 서브에이전트를
여러 개 돌리기 시작하면 한도에 걸릴 수 있다(그때는 유료 키로 교체).

## 실행법
```bash
cd OpenPlan-AI
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# .env 열어서 GEMINI_API_KEY 를 실제 키로 채우기

uvicorn app.main:app --reload --port 8000
```

> **WSL에서 `ensurepip is not available` 로 실패하면** 두 가지 길이 있다.
> ① `sudo apt install python3-venv` 후 재시도, 또는 ② **Windows Python 으로 만들기**(sudo 불필요):
> ```cmd
> cd /d C:\dev\openplan\OpenPlan-AI
> python -m venv .venv
> .venv\Scripts\python -m pip install --only-binary=:all: -r requirements.txt
> .venv\Scripts\python -m uvicorn app.main:app --port 8000
> ```
> `--only-binary=:all:` 은 소스 빌드(Rust 요구)로 떨어지는 것을 막는다. 2026-07-25 검증은 ②로 했다.
> 이때 서버는 Windows 쪽에 바인딩되므로 **WSL 셸에서 `curl localhost:8000` 은 닿지 않는다** —
> Windows 터미널이나 `cmd.exe /c curl ...` 로 호출할 것.

## 확인
```bash
# 헬스체크 — 모델 호출 없음
curl http://localhost:8000/health
# → {"status":"ok","service":"openplan-ai"}

# 핑 (상용 LLM 왕복 검증)
curl -X POST http://localhost:8000/ping \
  -H "Content-Type: application/json" \
  -d '{"prompt":"한 문장으로 자기소개해줘","tier":"complex"}'
# → {"tier":"complex","model":"gemini/gemini-3.6-flash","output":"..."}

# 계획 초안 생성 (실제 모델을 호출한다 — GEMINI_API_KEY 한도를 소모)
curl -X POST http://localhost:8000/plans/draft \
  -H "Content-Type: application/json" \
  -d '{
    "snapshot": {
      "weekStartDate": "2026-08-03",
      "zone": "Asia/Seoul",
      "referenceTime": "2026-08-01T09:00:00Z",
      "blocks": [],
      "activeFixedSchedules": [],
      "availabilities": [
        {"weekday": "MON", "startTime": "09:00", "endTime": "18:00", "active": true}
      ],
      "taskFacts": {
        "11111111-1111-1111-1111-111111111111": {
          "dueDate": "2026-08-07", "wbsStart": "2026-08-03", "wbsEnd": "2026-08-06",
          "estimatedMinutes": 60, "priority": 2
        }
      }
    },
    "tasksToPlace": ["11111111-1111-1111-1111-111111111111"]
  }'
# → 200 시 {"proposedBlocks":[...],"unplacedTaskIds":[...],"reason":"...","meta":{"model":"...","latencyMs":...}}
```
Swagger UI: `http://localhost:8000/docs`

**검증 이력**: 2026-07-25 무료 티어에서 `complex`(gemini-3.6-flash)·`light`(gemini-3.5-flash-lite) 둘 다 200 왕복 확인.

> `zone`(위 예시는 `Asia/Seoul`) 검증은 파이썬 표준 `zoneinfo` 를 쓴다. **Windows 는 OS에 IANA
> tz DB가 없어** `tzdata` 패키지 없이는 이 필드에서 422가 난다(2026-08-02 실측) — `tzdata` 는
> `requirements.txt` 에 이미 있으니 `pip install -r requirements.txt` 를 다시 돌리면 된다.

### 모델이 은퇴했을 때 (404)
`no longer available to new users` 404 가 나면 모델이 내려간 것이다. **이 키로 실제 호출 가능한 목록**을 뽑아 `.env` 를 갱신한다:
```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY&pageSize=200" \
  | grep -o '"name": "models/[^"]*"'
```

## 테스트

`/plans/draft` 는 실제 모델을 호출하지 않고도(전부 monkeypatch) 15건이 통과하도록
`tests/test_plans_draft.py` 에 작성돼 있다 — `GEMINI_API_KEY` 가 없어도 통과한다.

```cmd
:: WSL엔 pip/venv가 없어(위 "ensurepip is not available"과 같은 이유) Windows venv로 돌렸다.
cd /d C:\dev\openplan\OpenPlan-AI
.venv\Scripts\python.exe -m pytest tests\ -v
```

> `cmd.exe` 콘솔에서 테스트 코드의 한글 문자열(reason 등)이 깨져(mojibake) 보이면 실제
> 실패가 아니라 콘솔 코드페이지 문제다 — 먼저 `chcp 65001` 을 실행하면 정상 출력된다
> (2026-08-02 확인).

**검증 이력**: 2026-08-02, Windows venv(Python 3.11.2) · pytest 9.1.1 에서 `15 passed, 1 warning`
(경고는 `starlette.testclient`+`httpx` 조합의 deprecation 경고이며 테스트 실패가 아니다).

### 실패했을 때 읽는 법

**`/ping`**
| 응답 | 뜻 |
| --- | --- |
| `503 ... 미설정` | 그 모델에 필요한 키가 `.env` 에 없다 |
| `502 LLM 호출 실패` | 키는 있는데 호출이 깨졌다 — 모델명 오타·한도 초과·네트워크 |
| `422 알 수 없는 tier` | `tier` 는 `complex` \| `light` 만 |

**`/plans/draft`** (계약 §4 실패 응답 표 그대로. `tier` 개념이 없어 422의 의미가 `/ping`과 다르다)
| 응답 | 뜻 |
| --- | --- |
| `422` | 요청 스키마 위반(`snapshot`/`tasksToPlace` 구조·표기 규약 오류) — 버그다, 재시도 금지 |
| `503` | 모델 키(`GEMINI_API_KEY` 등) 미설정 |
| `502` | 모델 호출 실패(한도·네트워크·모델 은퇴) **또는** 모델 출력이 계약 형식과 다름(비-JSON·필수 필드 누락·`tasksToPlace` 밖 taskId 등) |
| `504` | 20초 안에 모델 응답이 오지 않음 |

Spring은 이 넷 중 503·502·504를 규칙 엔진 first-fit 폴백으로 받는다(계약 §4·§5) — 422만 예외(버그 수정 대상).

## 모델 라우팅 ("콘센트")
- `tier=complex` → 복합 추론 (계획 생성·재계획)
- `tier=light`   → 경량 (분류·요약). **로컬 전환은 코드 무변경** — `.env`에 `LOCAL_MODEL=ollama/qwen3:8b` 를 넣으면 경량 티어가 자동으로 로컬로 라우팅된다.

## 남은 TODO
- **W3 완료**: `orchestrator.generate_plan` — 스냅샷 + `tasksToPlace` → 계획 초안 생성
  (`POST /plans/draft`). Spring 쪽에서 제안을 스냅샷에 반영해 규칙검증(`PlanValidationPort`)에
  접합하는 것은 Spring 몫(계약 §5 호출 흐름).
- **W4**: 재계획·설명 서브에이전트(`replan`·`explain`) — 아직 스텁.
- **W5**: 태스크평가·개인화(경량, `evaluate_task`·`personalize`) + **로컬 Qwen 배선**(g4dn GPU 임대 + Ollama) — 아직 스텁.
- **Spring REST 계약**: `../OpenPlan문서/2주차 작업내용/05. Spring ↔ AI 서비스 REST 계약 초안.md` 의
  §3(요청)·§4(응답)이 위 구현의 사양이다. 문서 자체의 합의 상태·§7 잔여 항목은 그 문서를 참조.
