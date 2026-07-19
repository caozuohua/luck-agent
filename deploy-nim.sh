#!/bin/bash
# deploy-nim.sh — 在 GCP VPS 上用 NVIDIA NIM 起 OpenAI 兼容推理服务
# 供 luck-agent V2 的 LLM_BASE_URL 对接（newAPI）。
#
# 用法：
#   bash deploy-nim.sh            # 首次安装 + 启动
#   bash deploy-nim.sh --update  # 仅重启服务（模型/端口变更后）
#
# 前置：
#   - Ubuntu/Debian + Docker 已装（NIM 以容器运行，需 GPU 或 CPU 回退）
#   - 已登录 nvcr.io（NGC）：`docker login nvcr.io -u '$oauthtoken' -p <NGC_API_KEY>`
#
# 默认模型（冷门、响应快）：
#   nvidia/llama-3.1-nemotron-nano-8b-v1   （8B，指令微调，低延迟）
# 想换可改下方 NIM_MODEL。常见 NIM 模型：
#   nvidia/llama-3.1-nemotron-nano-8b-v1
#   nvidia/llama-3.3-70b-instruct
#   nvidia/qwen2.5-7b-instruct
set -euo pipefail

NIM_PORT="${NIM_PORT:-8000}"
NIM_MODEL="${NIM_MODEL:-nvidia/llama-3.1-nemotron-nano-8b-v1}"
NIM_CONTAINER_NAME="${NIM_CONTAINER_NAME:-luck-nim}"
NIM_DATA="/opt/nim"
SERVICE_FILE="/etc/systemd/system/nim.service"

info() { echo -e "\033[0;32m[INFO]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }

if ! command -v docker >/dev/null 2>&1; then
  warn "未检测到 docker，请先安装 Docker 引擎"
  exit 1
fi

info "部署 NIM 推理服务：模型=$NIM_MODEL  端口=$NIM_PORT"
sudo mkdir -p "$NIM_DATA"

# 拉取 NIM 镜像（NGC）
NIM_IMAGE="nvcr.io/nim/${NIM_MODEL}:latest"
info "拉取镜像 $NIM_IMAGE ..."
sudo docker pull "$NIM_IMAGE" || {
  warn "pull 失败，请确认 NGC_API_KEY 已 docker login nvcr.io"
  exit 1
}

# 写 systemd 单元（用 docker run，Restart=always 保活）
info "写入 $SERVICE_FILE"
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=NVIDIA NIM (${NIM_MODEL})
After=docker.service
Requires=docker.service

[Service]
TimeoutStartSec=0
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker rm -f ${NIM_CONTAINER_NAME}
ExecStart=/usr/bin/docker run --gpus all --name ${NIM_CONTAINER_NAME} \
  -p ${NIM_PORT}:8000 \
  -e NGC_API_KEY=\${NGC_API_KEY} \
  -v ${NIM_DATA}:/opt/nim/.cache \
  ${NIM_IMAGE}
ExecStop=/usr/bin/docker stop ${NIM_CONTAINER_NAME}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable nim
sudo systemctl restart nim

info "等待服务就绪（/v1/models）..."
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:${NIM_PORT}/v1/models" >/dev/null 2>&1; then
    info "NIM 已就绪 ✅  对接口：http://localhost:${NIM_PORT}/v1"
    curl -s "http://localhost:${NIM_PORT}/v1/models" | head -c 300; echo
    exit 0
  fi
  sleep 3
done
warn "NIM 30s 内未就绪，查看：sudo journalctl -u nim -n 50 --no-pager"
exit 1
