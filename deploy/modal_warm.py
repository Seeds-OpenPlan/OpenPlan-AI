"""Modal Ollama 를 미리 깨워 두거나(up) 다시 0대로 내린다(down). 재배포 없이 바뀐다.

    python deploy/modal_warm.py up     # 최소 1대 유지 — 시연·심사 전에
    python deploy/modal_warm.py down   # 최소 0대 — 끝나면 반드시

🔴 up 상태는 요청이 없어도 GPU 요금이 계속 나간다(T4 약 $0.59/시간 + CPU·메모리).
   다음 `modal deploy` 를 하면 데코레이터의 정적 설정(0대)으로 되돌아간다.
"""

import sys

import modal

fn = modal.Function.from_name("openplan-ollama", "serve")

if len(sys.argv) != 2 or sys.argv[1] not in ("up", "down"):
    sys.exit("사용법: modal_warm.py up|down")

fn.update_autoscaler(min_containers=1 if sys.argv[1] == "up" else 0)
print(f"min_containers = {1 if sys.argv[1] == 'up' else 0}")
