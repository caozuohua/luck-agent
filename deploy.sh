#!/bin/bash
# deploy.sh — V2 Goal Runtime 一键部署 / 更新到 GCP VPS (e2-micro+)
# 用法：bash deploy.sh [--update]
set -euo pipefail

PROJECT_ID="${GCP_PROJECT:-project-c1exx}"
ZONE="${GCP_ZONE:-us-central1-c}"
INSTANCE="${INSTANCE_NAME:-luck-agent}"
REMOTE_DIR="/opt/luck-agent"
UPDATE_ONLY="${1:-}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# ── Step 1: 上传 V2 代码（全量，保留运行时生成的 data/）─────────
upload_code() {
  info "上传 V2 代码到 $INSTANCE..."
  FILES=(
    main.py settings.py requirements.txt .env.example luck-agent.service
    llm core runtime skills memory interface tools
    config/routing_rules.yaml
  )
  tar czf /tmp/v2_code.tar.gz "${FILES[@]}"
  gcloud compute scp /tmp/v2_code.tar.gz "$INSTANCE:/tmp/v2_code.tar.gz" --zone="$ZONE"
  rm -f /tmp/v2_code.tar.gz
  gcloud compute ssh "$INSTANCE" --zone="$ZONE" -- bash <<REMOTE
    set -e
    cd $REMOTE_DIR
    tar xzf /tmp/v2_code.tar.gz
    rm -f /tmp/v2_code.tar.gz
    # 保证包 __init__.py 存在
    for d in llm core runtime skills memory interface tools; do
      touch "\$d/__init__.py"
    done
    # 运行时数据目录（持久化 SQLite）
    mkdir -p /opt/luck-agent/data /opt/luck-agent/workspace
    chown -R agent:agent /opt/luck-agent 2>/dev/null || true
REMOTE
  info "代码上传完成"
}

# ── Step 2: 安装依赖 + 配置 systemd + 启动 ─────────────────────
install_and_start() {
  info "安装依赖并启动 V2 服务..."
  gcloud compute ssh "$INSTANCE" --zone="$ZONE" -- bash <<'REMOTE'
    set -e
    if ! python3 --version >/dev/null 2>&1; then
      sudo apt-get update -q
      sudo apt-get install -y python3 python3-venv python3-pip
    fi
    if [ ! -d /opt/luck-agent/venv ]; then
      python3 -m venv /opt/luck-agent/venv
    fi
    /opt/luck-agent/venv/bin/pip install -q --upgrade pip
    /opt/luck-agent/venv/bin/pip install -q -r /opt/luck-agent/requirements.txt
    # 系统用户（如不存在）
    id agent >/dev/null 2>&1 || sudo useradd -r -s /bin/false agent
    sudo mkdir -p /opt/luck-agent/data /opt/luck-agent/workspace
    sudo chown -R agent:agent /opt/luck-agent
    sudo cp /opt/luck-agent/luck-agent.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable luck-agent
    sudo systemctl restart luck-agent
    sleep 2
    sudo systemctl status luck-agent --no-pager
REMOTE
  info "部署完成 ✅  (LLM 需另跑 deploy-nim.sh 起 NIM 服务)"
}

# ── 主流程 ────────────────────────────────────────────────────────
if [[ "$UPDATE_ONLY" == "--update" ]]; then
  gcloud compute ssh "$INSTANCE" --zone="$ZONE" -- \
    "sudo systemctl restart luck-agent && sleep 1 && sudo systemctl status luck-agent --no-pager"
  info "热更新完成 ✅"
else
  upload_code
  install_and_start
fi
