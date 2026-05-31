# Luck Agent

基于 Python 的 Lark 飞书智能体，部署在 GCP e2-micro（永久免费层），通过 WebSocket 实时连接 Lark，集成 Gemini 多模型路由、GitHub/Shell/搜索工具链、ReAct 工具调用闭环和 SQLite 持久化记忆。

---

## 架构概览

```
Lark WebSocket
      │
      ▼
  agent.py ──── 组件装配 + 消息分发
      │
      ├─ command.py ──── /sh /git /deploy /mem /status ...（零 AI，纯规则）
      ├─ message.py ──── AI ReAct 工具调用闭环（意图路由 → 模型 → 工具 → 推理）
      └─ file_handler.py ─ Lark 文件/图片 ↔ VPS 双向传输
              │
              ▼
  tools/ ──── github_tools / shell_tools / search_tools / file_bridge
  core/  ──── model_router / memory / task_queue / scheduler / health / log
  cards/ ──── Lark Card 2.0 消息卡片构建器
```

**设计原则：意图路由（零 AI）优先，工具调用最小化，记忆持久化，指令与 AI 完全解耦。**

---

## 环境要求

| 组件 | 版本 |
|------|------|
| Python | 3.10+ |
| GCP | e2-micro 或更高（VPC + Cloud Logging 可选） |
| Lark 应用 | 飞书开放平台自建应用（含机器人能力） |
| GitHub Token | `Contents:Read & Write`（博客写入）、`Actions:Read & Write`（CI 触发）|
| Gemini API | Vertex AI 或 Google AI Studio API Key |

---

## 目录结构

```
luck-agent/
├── agent.py                # 主入口：WebSocket + 组件装配 + 优雅退出
├── config.py               # 配置中心（.env 加载 + GCP 认证检测）
├── requirements.txt        # 4 个外部依赖（零额外运行时依赖）
├── CLAUDE.md               # 项目约定（AI 协作上下文）
│
├── core/                   # 基础设施
│   ├── intent_router.py    # 意图路由（正则 + 关键词，零 AI）
│   ├── log.py              # 结构化 JSON 日志（GCP Cloud Logging 兼容）
│   ├── memory.py           # SQLite WAL 持久化（对话/画像/任务/成功模式）
│   ├── model_router.py     # 多模型路由 + 故障切换 + 工具调用
│   ├── task_queue.py       # 异步优先队列 + Worker + 指数退避重试
│   ├── scheduler.py        # Cron/Interval 定时任务（SQLite 持久化）
│   └── health.py           # 健康监控（日志回溯/VACUUM/资源预警）
│
├── tools/                  # 工具执行层
│   ├── github_tools.py     # GitHub REST API v3（连接池 + 429/5xx 重试）
│   ├── shell_tools.py      # Shell 异步执行 + VPS 文件读写
│   ├── search_tools.py     # 多后端搜索（Tavily/DuckDuckGo/SearXNG/Qwant）
│   └── file_bridge.py      # Lark ↔ VPS 文件双向传输（自动 Token 续期）
│
├── handlers/               # 消息路由层
│   ├── command.py          # 直接指令（18+ 条，零 AI 依赖）
│   ├── message.py          # AI 消息处理器（ReAct 工具调用闭环）
│   └── file_handler.py     # 文件/图片消息接收 + VPS 本地存储
│
└── cards/
    └── builder.py          # Lark Card 2.0 消息卡片构建（模板化复用）
```

---

## 部署方法

### 方式一：VPS 手动部署（推荐）

**1. 克隆项目**

```bash
sudo mkdir -p /opt/luck-agent
sudo chown $USER:$USER /opt/luck-agent
git clone https://github.com/caozuohua/luck-agent.git /opt/luck-agent
cd /opt/luck-agent
```

**2. 创建虚拟环境并安装依赖**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**3. 配置 .env**

```bash
cp .env.example .env
# 编辑 .env，填入以下必填项
```

必填配置：
```ini
# GCP
GCP_PROJECT=your-gcp-project-id
GCP_LOCATION=us-central1              # Vertex AI 区域

# Lark（飞书开放平台获取）
LARK_APP_ID=cli_xxxxxxxxxx
LARK_APP_SECRET=xxxxxxxxxx
LARK_DOMAIN=https://open.feishu.cn   # 飞书国内版

# GitHub
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
GITHUB_OWNER=caozuohua               # 默认仓库 owner

# Hugo 博客（可选）
HUGO_REPO=caozuohua/caozuohua.github.io

# Tavily 搜索（可选，无则自动 fallback 到 DuckDuckGo）
TAVILY_API_KEY=tvly-xxxxxxxx
```

**4. 配置 GCP 认证（二选一）**

```bash
# 方式 A：服务账号 Key（推荐用于 VPS）
export GOOGLE_APPLICATION_CREDENTIALS=/opt/luck-agent/credentials/gcp-key.json
# 方式 B：GCE 实例 ADC（GCP 实例默认自带，无需额外配置）
```

**5. 启动**

```bash
python agent.py
```

**6. 后台运行（systemd）**

```bash
sudo tee /etc/systemd/system/luck-agent.service << 'EOF'
[Unit]
Description=Luck Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/luck-agent
ExecStart=/opt/luck-agent/venv/bin/python agent.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/luck-agent.log
StandardError=append:/var/log/luck-agent.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable luck-agent
sudo systemctl start luck-agent
sudo systemctl status luck-agent
```

**7. 热更新**

```bash
cd /opt/luck-agent
git pull
sudo systemctl restart luck-agent
sudo journalctl -u luck-agent -n 50 --no-pager
```

---

### 方式二：Docker 部署（可选）

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "agent.py"]
```

---

## 直接指令（零 AI 依赖，Lark 前缀 `/`）

| 分类 | 指令 | 说明 |
|------|------|------|
| Shell | `/sh <命令>` | 执行 shell 命令（危险命令需 `/yes` 确认）|
| Shell | `/sh! <命令>` | 跳过确认强制执行 |
| 文件 | `/ls [路径]` | 列出目录 |
| 文件 | `/cat <路径>` | 读取文件内容 |
| 文件 | `/rm <路径>` | 删除文件（危险路径直接拦截，其他需确认）|
| 文件 | `/files` | 列出已接收文件 |
| 文件 | `/send <路径>` | 发送 VPS 文件到当前对话 |
| Git | `/git [路径] [message]` | add + commit + push |
| GitHub | `/deploy [repo]` | 触发 GitHub Actions 部署 |
| GitHub | `/runs [repo]` | 查看 Actions 运行状态 |
| GitHub | `/posts [repo]` | 列出博客文章 |
| GitHub | `/search <关键词>` | Web 搜索（多后端自动 failover）|
| 系统 | `/status` | 系统状态（内存/磁盘/进程）|
| 系统 | `/logs [error\|warning] [小时数]` | 查询错误日志回溯 |
| 任务 | `/task <id>` | 查看任务状态 |
| 任务 | `/tasks` | 查看任务列表 |
| 定时 | `/schedule list` | 查看定时任务 |
| 定时 | `/schedule pause\|resume\|cancel <id>` | 管理定时任务 |
| 记忆 | `/mem` | 记忆总览（画像+成功模式+对话，一条消息）|
| 记忆 | `/mem profile\|patterns\|history` | 查看单项记忆 |
| 记忆 | `/mem set <key> <value>` | 写入用户画像 |
| 记忆 | `/mem del <key\|profile\|patterns\|history>` | 删除记忆 |
| 其他 | `/pro\|/flash\|/lite <消息>` | 强制指定 AI 模型 |
| 其他 | `/yes` | 确认待执行的危险操作 |
| 其他 | `/help` | 显示帮助 |

---

## 意图路由（自动触发，无需用户输入）

智能体通过正则 + 关键词自动识别用户意图，注入专用 prompt 和最小工具子集，无需模型自行选择工具：

| 意图 | 触发示例 | 注入工具 |
|------|---------|---------|
| BLOG_WRITE | "写博客"、"发文章"、"hugo" | create_blog_post, list_blog_posts, trigger_workflow |
| BLOG_LIST | "列出文章"、"看看博客" | list_blog_posts |
| GITHUB_ACTION | "触发部署"、"跑ci"、"actions状态" | trigger_workflow, list_workflow_runs |
| GITHUB_ISSUE | "创建issue"、"pr列表" | list_items, create_issue |
| GITHUB_CODE | "读取文件"、"查看代码" | get_file, update_file |
| SHELL_RUN | "执行命令"、"安装"、"systemctl" | run_shell |
| FILE_OP | "查看日志"、"tail"、"/opt/" | run_shell, read_file, write_file |
| MEMORY_OP | "记住"、"你还记得" | remember, recall |
| GIT_PUSH | "推送代码"、"git push" | run_shell |
| SEARCH | "搜索"、"查一下" | search_web |
| GENERAL | 其他所有消息 | 全量工具 |

---

## 模型路由

根据消息长度和关键词自动选择模型，支持故障切换链：

```
长文本（>500字）或含"分析/写作/规划"关键词 → gemini-3.5-flash（或 pro 降级链）
短消息 → gemini-3.1-flash-lite（最快速）
前缀 /pro → 强制 pro | /flash → 强制 flash | /lite → 强制 lite

故障切换链（自动降级）：
  gemini-3.5-flash → gemini-2.5-pro → gemini-2.5-flash → gemini-2.5-flash-lite
  gemini-3.1-flash-lite → gemini-2.5-flash → gemini-2.5-flash-lite
```

配置：
```ini
MODEL_PRO=gemini-3.5-flash
MODEL_FLASH=gemini-3.1-flash-lite
MODEL_LITE=gemini-2.5-flash-lite
```

---

## 记忆系统

SQLite WAL 模式，自动积累三类记忆：

| 类型 | 存储内容 | 用途 |
|------|---------|------|
| 对话历史 | 所有 user/assistant 消息 | 上下文注入（最近 6 条）|
| 用户画像 | 用户偏好、习惯、重要信息 | 个性化（`/mem set` 写入）|
| 成功模式 | 历史工具调用的 intent + command | 减少模型决策负担 |

记忆自动注入系统 prompt，无需手动管理。

---

## 热更新

```bash
# 只更新代码，不重建 venv
git pull && sudo systemctl restart luck-agent
```

---

## 许可证

MIT License
