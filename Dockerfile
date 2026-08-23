# OpenPlan AI 서비스 컨테이너 — Spring 이 내부망에서만 부른다(계약 §2, 외부 미노출).
#
# 🔴 파이썬은 3.11 로 고정한다. requirements.txt 가 litellm 을 <1.92 로 묶어 두었고,
#    그 상한의 이유가 "1.92+ 는 Python 3.11 용 wheel 이 없어 sdist 빌드로 떨어지고 Rust/Cargo 를
#    요구해 설치가 깨진다"(2026-07-25 실측)이다. 3.12+ 로 올리려면 그 상한부터 풀어야 한다.
FROM python:3.11-slim AS base

# tzdata 는 requirements 에 있지만 OS 레벨 IANA DB 도 함께 둔다 — zone 검증(ZoneInfo)이
# 두 경로 어느 쪽으로도 살아 있어야 한다.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 의존성을 먼저 받는다 — app/ 만 바뀔 때 이 층이 캐시에 남아 재빌드가 짧아진다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# 🔴 .env 는 이미지에 넣지 않는다. 키는 compose 의 environment 로 주입한다 —
#    이미지에 구우면 레지스트리에 올라가는 순간 유출이고, 바꿀 때마다 재빌드가 된다.
#    pydantic-settings 는 .env 가 없으면 환경변수를 읽으므로 그대로 동작한다.

# 루트로 돌지 않는다. 이 서비스는 외부 입력(스냅샷 JSON)을 파싱하므로 권한을 최소로 둔다.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# compose 가 이 값을 보고 backend 를 늦게 띄운다(depends_on: condition: service_healthy).
# /health 는 모델을 부르지 않는다 — 키가 없어도 200 이라 "떴는가"만 정확히 답한다.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
