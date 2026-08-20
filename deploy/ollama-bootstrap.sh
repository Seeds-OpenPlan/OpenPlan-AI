#!/usr/bin/env bash
# GPU 인스턴스 부팅 스크립트 — EC2 launch wizard 의 "User data" 에 그대로 붙여넣는다.
#
# 전제: NVIDIA 드라이버가 이미 있는 AMI (Deep Learning Base OSS Nvidia Driver GPU AMI, Ubuntu).
#       드라이버 없는 순정 Ubuntu 를 쓰면 이 스크립트 앞에 드라이버 설치 + 재부팅이 붙어
#       "한 번에 끝내고 AMI 로 굽는다"는 목적이 깨진다.
#
# 이 스크립트는 성공하면 조용히 끝나고, 실패하면 /var/log/ollama-bootstrap.log 에 이유를 남긴다.
# 로그: sudo tail -f /var/log/cloud-init-output.log

set -euo pipefail
exec > >(tee -a /var/log/ollama-bootstrap.log) 2>&1

MODEL="${MODEL:-qwen3:8b}"   # .env.example:27 의 문서화된 목표. 바꾸려면 여기만.

echo "== [1/5] GPU 인식 확인 =="
# 드라이버가 없으면 Ollama 는 조용히 CPU 로 떨어진다. 여기서 먼저 죽이는 편이 낫다 —
# GPU 를 빌려놓고 CPU 로 추론하는 것이 이 작업에서 가장 비싼 실패다.
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

echo "== [2/5] Ollama 설치 =="
curl -fsSL https://ollama.com/install.sh | sh

echo "== [3/5] 외부 접근 허용 + 모델 상주 =="
# 기본값은 127.0.0.1 바인딩이라 배포 서버에서 못 닿는다. 이 override 가 없으면
# 보안그룹을 열어도 연결이 거부된다 (접근 통제는 보안그룹이 전담).
# OLLAMA_KEEP_ALIVE: 기본 5분이면 VRAM 에서 내려가 다음 요청이 모델 로딩부터 다시 한다.
install -d /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=24h"
EOF
systemctl daemon-reload
systemctl enable --now ollama
systemctl restart ollama

# 데몬이 뜰 때까지 대기 (pull 이 연결 실패로 죽는 것을 막는다)
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:11434/api/version >/dev/null && break
  sleep 2
done

echo "== [4/5] 모델 내려받기: ${MODEL} =="
# 실패하면 대개 ⑴ 태그 오타 ⑵ 루트 볼륨 부족이다. 8b 는 약 5GB, AMI 자체도 크다.
ollama pull "${MODEL}"

echo "== [5/5] 왕복 검증 — think 가 실제로 꺼졌는지까지 =="
# 이 프로젝트가 당한 결함은 "think 를 안 끄면 <think> 가 max_tokens 를 다 써서
# 200 + 빈 본문이 나간다" 였다. 다만 2026-08-11 재측정 결과 **빈 본문은 프롬프트 의존적**이다 —
# 추론이 짧게 끝나면 한도에 안 걸려 본문이 멀쩡히 나온다.
# 따라서 "본문이 비었나"로는 회귀를 못 잡는다. think 가 꺼졌으면 사소한 질문의
# 생성 토큰이 한 자릿수고, 안 꺼졌으면 수백이다 — **토큰 수가 판별식이다.**
RESP=$(curl -sf http://127.0.0.1:11434/api/chat -d "{
  \"model\": \"${MODEL}\",
  \"messages\": [{\"role\": \"user\", \"content\": \"한 단어로 답하라: 대한민국의 수도는?\"}],
  \"stream\": false,
  \"think\": false
}")

read -r CONTENT TOKENS <<EOF
$(printf '%s' "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["message"]["content"].strip().replace(" ","_") or "«빈본문»", d.get("eval_count", -1))')
EOF

if [ "$CONTENT" = "«빈본문»" ]; then
  echo "🔴 실패: 200 을 받았으나 본문이 비었다."; echo "원본: $RESP"; exit 1
fi
if [ "$TOKENS" -gt 40 ]; then
  echo "🔴 실패: think 가 꺼지지 않았다 — 사소한 질문에 ${TOKENS} 토큰을 썼다(기대: 한 자릿수)."
  echo "   Ollama 가 think 파라미터를 무시하고 있다. 이 상태로 배포하면 토큰 예산이 추론에 샌다."
  exit 1
fi

echo "✅ 왕복 정상. 응답=${CONTENT} · 생성토큰=${TOKENS}"
cat <<'NEXT'

== 부팅 완료 — 다음 순서로 마무리할 것 ==
  1. 이 상태에서 AMI 생성 (Actions > Image and templates > Create image)
  2. AMI 상태가 "Available" 이 된 것을 확인
  3. 인스턴스를 **종료(Terminate)** — 정지(Stop)가 아니다

  정지는 EBS 루트 볼륨이 계속 과금된다(60GB gp3 기준 월 $5 안팎). AMI 에 이미
  전부 들어 있으므로 그 볼륨은 내용이 중복될 뿐이다. 스냅샷은 실사용분만 압축
  과금이라 훨씬 싸다.
  더 중요한 이유: 크레딧이 g4dn.xlarge 기준 140시간뿐이라 **켜둔 채 잊는 사고**가
  치명적인데, 종료된 인스턴스는 실수로 시작할 수가 없다.

  다시 쓸 때는 이 AMI 로 새 인스턴스를 띄우면 된다(약 2분, 부팅 스크립트 불필요).
NEXT
