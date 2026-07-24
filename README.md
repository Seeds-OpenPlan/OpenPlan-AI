# OpenPlan AI 서비스 (W2 뼈대)

AI 계획 생성을 담당하는 **별도 Python 서비스**. Spring 백엔드가 REST로 호출한다.

> **경계 (엄수): 만드는 건 AI, 판정은 규칙.**
> 이 서비스는 계획 **초안 생성**만 한다. 적정성 **검증(판정)은 Spring 규칙엔진** 소관.
> 흐름: `사용자 요청 → (이 서비스) AI 초안 → Spring 규칙검증 → 사용자 확정 → 저장`

현재 범위(W2): **"상용 Claude에 핑 → 응답"까지만 동작.** 계획 생성 로직은 W3.

## 구조
```
OpenPlan-AI/
├── requirements.txt          최소 의존성
├── .env.example              환경변수 템플릿 (실제 값은 로컬 .env 에만)
├── app/
│   ├── config.py             env 설정 (모델·키·포트)
│   ├── main.py               FastAPI 앱 · /health · /ping
│   ├── models/schemas.py     요청·응답 스키마
│   ├── router/model_router.py  "콘센트" — 티어→모델 라우팅 (상용/로컬)
│   └── orchestrator/orchestrator.py  메인 에이전트 진입점 (서브에이전트 자리, W3+)
```

## 실행법
```bash
cd OpenPlan-AI
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# .env 열어서 ANTHROPIC_API_KEY 를 실제 키로 채우기

uvicorn app.main:app --reload --port 8000
```

## 확인
```bash
# 헬스체크
curl http://localhost:8000/health
# → {"status":"ok","service":"openplan-ai"}

# 핑 (상용 Claude 왕복 검증)
curl -X POST http://localhost:8000/ping \
  -H "Content-Type: application/json" \
  -d '{"prompt":"한 문장으로 자기소개해줘","tier":"complex"}'
# → {"tier":"complex","model":"anthropic/claude-sonnet-5","output":"..."}
```
Swagger UI: `http://localhost:8000/docs`

## 모델 라우팅 ("콘센트")
- `tier=complex` → 상용 Claude (복합 추론: 계획 생성·재계획)
- `tier=light`   → 현재 상용 저가(Haiku). **로컬 전환은 코드 무변경** — `.env`에 `LOCAL_MODEL=ollama/qwen3:8b` 넣으면 경량 티어가 자동으로 로컬로 라우팅됨.

## 남은 TODO
- **W3**: `orchestrator.generate_plan` — 스냅샷(태스크·주간계획·가용) → 계획 초안 생성. Spring 규칙검증 접합.
- **W4**: 재계획·설명 서브에이전트.
- **W5**: 태스크평가·개인화(경량) + **로컬 Qwen 배선**(g4dn GPU 임대 + Ollama).
- **Spring REST 계약**: 요청 스냅샷 / 응답 계획초안 스키마를 진소희(도메인)와 합의.
