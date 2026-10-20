"""Modal 에 Ollama + Qwen3 를 올린다 — LIGHT 티어(LOCAL_MODEL)의 GPU 서빙처.

배포:   modal deploy deploy/modal_ollama.py
주소:   배포 출력의 https://<workspace>--openplan-ollama-serve.modal.run
        → 배포 서버 .env 의 OLLAMA_BASE_URL 에 그대로 넣는다(경로 없이).

이 파일은 EC2 부팅 스크립트(feat/ollama-bootstrap)의 Modal 판이다. 서빙처를 GCP 등으로 옮겨도
앱 쪽은 OLLAMA_BASE_URL(·OLLAMA_API_KEY) 두 값만 바뀐다 — 그래서 여기서도 Ollama HTTP API 를
그대로 노출하고, 앞에 다른 API 를 씌우지 않는다.

🔴 Ollama 에는 인증이 없다. Modal 주소는 공개 URL 이므로 requires_proxy_auth 를 끄면 누구나
   우리 GPU 로 추론할 수 있다. 호출 측은 `Authorization: Bearer <Token ID>.<Token Secret>` 을
   보내야 한다(Modal 대시보드 Settings → Proxy Auth Tokens 에서 발급).

🔴 평소에는 0대로 내려간다. 내려간 뒤 첫 요청은 컨테이너 기동 + 모델 적재로 수십 초가 걸려
   EXPLAIN_TIMEOUT(15초)을 넘는다 — 그 사이는 Groq 폴백이 받는다. 시연 때는 미리 깨워 둔다:
     modal_warm.py up   (min_containers=1, 켜 둔 시간만큼 과금)
     modal_warm.py down (0대로 복귀)
"""

import subprocess
import threading
import time
import urllib.request

import modal

# 🔴 `qwen3:4b` 가 아니다. 그 태그는 `qwen3:4b-thinking`(2507 Thinking판)과 같은 파일이라
#    think=False 를 줘도 사고 과정을 본문에 그대로 쓴다(2026-10-20 실측 — 3~4문장 요청에
#    영어 추론 400토큰). Instruct판은 사고 단계 자체가 없다.
MODEL = "qwen3:4b-instruct"
OLLAMA_VERSION = "0.32.15"  # 2026-08-21 g4dn 실측 버전과 맞춘다
PORT = 11434

image = (
    modal.Image.from_registry(f"ollama/ollama:{OLLAMA_VERSION}", add_python="3.12")
    .entrypoint([])  # 이미지 기본 ENTRYPOINT(ollama) 를 비운다 — 기동은 아래 함수가 한다
    .env(
        {
            "OLLAMA_HOST": f"0.0.0.0:{PORT}",
            # 컨테이너가 살아 있는 동안은 모델을 VRAM 에서 내리지 않는다.
            # 기본 5분이면 컨테이너는 떠 있는데 모델만 내려가 첫 요청이 다시 느려진다.
            "OLLAMA_KEEP_ALIVE": "-1",
        }
    )
    # 모델을 이미지에 굽는다. 기동 때마다 5GB 를 내려받으면 콜드스타트가 그만큼 늘어난다.
    # pull 에는 서버가 떠 있어야 하므로 잠깐 띄웠다가 내린다.
    .run_commands(
        f"bash -c 'ollama serve & PID=$!; sleep 5; ollama pull {MODEL}; kill $PID'"
    )
)

app = modal.App("openplan-ollama", image=image)


def _preload() -> None:
    """서버가 뜨면 모델을 VRAM 에 미리 올린다 — 첫 실요청이 적재 시간을 떠안지 않게."""
    body = f'{{"model": "{MODEL}", "keep_alive": -1}}'.encode()
    for _ in range(60):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/generate", data=body)
            urllib.request.urlopen(req, timeout=120).read()
            return
        except Exception:
            time.sleep(1)


@app.function(
    gpu="T4",
    cpu=2,
    memory=8192,
    # 마지막 요청 뒤 10분은 살려 둔다 — 연달아 오는 설명문 요청이 매번 콜드스타트를 맞지 않게.
    scaledown_window=600,
    timeout=3600,
)
@modal.concurrent(max_inputs=8)
@modal.web_server(PORT, startup_timeout=60, requires_proxy_auth=True)
def serve():
    subprocess.Popen(["ollama", "serve"])
    threading.Thread(target=_preload, daemon=True).start()
