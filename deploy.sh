#!/bin/bash
# deploy.sh — 一键部署 / 更新到 e2-micro
# 用法：bash deploy.sh [--update]
set -euo pipefail

PROJECT_ID="${GCP_PROJECT:-your-gcp-project}"
ZONE="${GCP_ZONE:-us-central1-a}"
INSTANCE="${INSTANCE_NAME:-lark-agent}"
REMOTE_DIR="/opt/lark-agent"
UPDATE_ONLY="${1:-}"

# ── 颜色输出 ──────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# ── Step 1: 写入 Secrets（首次部署时运行）────────────────────
setup_secrets() {
  info "配置 GCP Secret Manager..."

  for secret in lark-app-id lark-app-secret github-token; do
    if ! gcloud secrets describe $secret --project=$PROJECT_ID &>/dev/null; then
      read -rsp "请输入 $secret 的值: " val; echo
      echo -n "$val" | gcloud secrets create $secret \
        --project=$PROJECT_ID --data-file=-
    else
      warn "$secret 已存在，跳过"
    fi
  done

  # 给 Compute Engine 默认 SA 赋予读权限
  SA=$(gcloud iam service-accounts list \
    --project=$PROJECT_ID \
    --filter="displayName:Compute Engine default" \
    --format="value(email)")

  for secret in lark-app-id lark-app-secret github-token; do
    gcloud secrets add-iam-policy-binding $secret \
      --project=$PROJECT_ID \
      --member="serviceAccount:$SA" \
      --role="roles/secretmanager.secretAccessor" \
      --quiet
  done
  info "Secrets 配置完成"
}

# ── Step 2: 上传代码 ──────────────────────────────────────────
upload_code() {
  info "上传代码到 $INSTANCE..."

  FILES=(
    agent.py config.py requirements.txt lark-agent.service
    core/memory.py core/model_router.py core/task_queue.py
    tools/github_tools.py tools/shell_tools.py tools/file_bridge.py
    handlers/command.py handlers/message.py handlers/file_handler.py
    cards/builder.py
  )

  # 创建远端目录结构
  gcloud compute ssh $INSTANCE --zone=$ZONE -- \
    "sudo mkdir -p $REMOTE_DIR/{core,tools,handlers,cards} && \
     sudo chown -R \$USER:$USER $REMOTE_DIR"

  # 创建本地临时 tar
  tar czf /tmp/agent_code.tar.gz "${FILES[@]}"
  gcloud compute scp /tmp/agent_code.tar.gz \
    $INSTANCE:/tmp/agent_code.tar.gz --zone=$ZONE
  rm /tmp/agent_code.tar.gz

  gcloud compute ssh $INSTANCE --zone=$ZONE -- bash << REMOTE
    cd $REMOTE_DIR
    tar xzf /tmp/agent_code.tar.gz
    # 确保 __init__.py 存在
    for d in core tools handlers cards; do
      touch \$d/__init__.py
    done
    rm /tmp/agent_code.tar.gz
REMOTE
  info "代码上传完成"
}

# ── Step 3: 安装依赖 + 启动服务 ──────────────────────────────
install_and_start() {
  info "安装依赖并启动服务..."

  gcloud compute ssh $INSTANCE --zone=$ZONE -- bash << 'REMOTE'
    set -e
    # Python 3.12
    if ! python3.12 --version &>/dev/null; then
      sudo apt-get update -q
      sudo apt-get install -y python3.12 python3.12-venv python3-pip
    fi

    # virtualenv
    if [ ! -d /opt/lark-agent/venv ]; then
      python3.12 -m venv /opt/lark-agent/venv
    fi

    /opt/lark-agent/venv/bin/pip install -q --upgrade pip
    /opt/lark-agent/venv/bin/pip install -q -r /opt/lark-agent/requirements.txt

    # 创建系统用户（如不存在）
    id agent &>/dev/null || sudo useradd -r -s /bin/false agent

    # 目录权限
    sudo mkdir -p /opt/workspace /opt/lark-agent/files
    sudo chown -R agent:agent /opt/lark-agent /opt/workspace

    # systemd
    sudo cp /opt/lark-agent/lark-agent.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable lark-agent
    sudo systemctl restart lark-agent
    sleep 2
    sudo systemctl status lark-agent --no-pager
REMOTE
  info "部署完成 ✅"
}

# ── 主流程 ────────────────────────────────────────────────────
if [[ "$UPDATE_ONLY" == "--update" ]]; then
  upload_code
  gcloud compute ssh $INSTANCE --zone=$ZONE -- \
    "sudo systemctl restart lark-agent && sleep 1 && sudo systemctl status lark-agent --no-pager"
  info "热更新完成 ✅"
else
  [[ "$UPDATE_ONLY" != "--skip-secrets" ]] && setup_secrets
  upload_code
  install_and_start
fi
