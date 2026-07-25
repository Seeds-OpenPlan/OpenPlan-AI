# OpenPlan AI 서비스 (W2 뼈대)

AI 계획 생성을 담당하는 **별도 Python 서비스**. Spring 백엔드가 REST로 호출한다.

> **경계 (엄수): 만드는 건 AI, 판정은 규칙.**
> 이 서비스는 계획 **초안 생성**만 한다. 적정성 **검증(판정)은 Spring 규칙엔진** 소관.
> 흐름: `사용자 요청 → (이 서비스) AI 초안 → Spring 규칙검증 → 사용자 확정 → 저장`

현재 범위(W2): **"상용 LLM에 핑 → 응답"까지만 동작.** 계획 생성 로직은 W3.

## 구조
```
OpenPlan-AI/
├── requirements.txt          최소 의존성
├── .env.example              환경변수 템플릿 (실제 값은 로컬 .env 에만)
├── app/
│   ├── config.py             env 설정 (제공자별 키·티어별 모델·포트)
│   ├── main.py               FastAPI 앱 · /health · /ping
│   ├── models/schemas.py     요청·응답 스키마
│   ├── router/model_router.py  "콘센트" — 티어→모델 라우팅 (상용/로컬)
│   └── orchestrator/orchestrator.py  메인 에이전트 진입점 (서브에이전트 자리, W3+)
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
```
Swagger UI: `http://localhost:8000/docs`

**검증 이력**: 2026-07-25 무료 티어에서 `complex`(gemini-3.6-flash)·`light`(gemini-3.5-flash-lite) 둘 다 200 왕복 확인.

### 모델이 은퇴했을 때 (404)
`no longer available to new users` 404 가 나면 모델이 내려간 것이다. **이 키로 실제 호출 가능한 목록**을 뽑아 `.env` 를 갱신한다:
```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY&pageSize=200" \
  | grep -o '"name": "models/[^"]*"'
```

### 실패했을 때 읽는 법
| 응답 | 뜻 |
| --- | --- |
| `503 ... 미설정` | 그 모델에 필요한 키가 `.env` 에 없다 |
| `502 LLM 호출 실패` | 키는 있는데 호출이 깨졌다 — 모델명 오타·한도 초과·네트워크 |
| `422 알 수 없는 tier` | `tier` 는 `complex` \| `light` 만 |

## 모델 라우팅 ("콘센트")
- `tier=complex` → 복합 추론 (계획 생성·재계획)
- `tier=light`   → 경량 (분류·요약). **로컬 전환은 코드 무변경** — `.env`에 `LOCAL_MODEL=ollama/qwen3:8b` 를 넣으면 경량 티어가 자동으로 로컬로 라우팅된다.

## 남은 TODO
- **W3**: `orchestrator.generate_plan` — 스냅샷(태스크·주간계획·가용) → 계획 초안 생성. Spring 규칙검증 접합.
- **W4**: 재계획·설명 서브에이전트.
- **W5**: 태스크평가·개인화(경량) + **로컬 Qwen 배선**(g4dn GPU 임대 + Ollama).
- **Spring REST 계약**: 요청 스냅샷 / 응답 계획초안 스키마를 진소희(도메인)와 합의.
