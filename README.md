# Luck Agent

基于 Python 的 Lark 智能体，面向 GCP e2-micro（永久免费层）和长期稳定运营场景：通过 WebSocket 实时连接 Lark，集成 Gemini 多模型路由、GitHub/Shell/搜索工具链、ReAct 工具调用闭环和 SQLite 持久化记忆。

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

**设计原则：意图路由（零 AI）优先，工具调用最小化，记忆持久化，指令与 AI 完全解耦，模型不可用时仍保留完整本地运维能力。**

---

## 核心特性

- 低资源：适配 e2-micro，尽量减少常驻内存和外部依赖
- 高韧性：`/status`、`/restart`、`/journal`、`/backup`、`/restore` 等大模型无关运维指令可独立工作
- 强检索：`/search` 支持单个 Tavily key 结合 Vercel 聚合，并在失败时自动 fallback 到 DuckDuckGo、SearXNG、Qwant
- 个人知识库：以 `#` 开头的消息可直接录入，`/pkb` 和模型工具可检索已记录内容
- 可运营：博客发布、GitHub Actions、Issue/PR、文件管理可通过统一工具链完成
- 可开源：命令、提示词、工具 schema 和卡片输出均尽量保持可读、可扩展

## 适合什么场景

- 你只有碎片时间，需要在 Lark 里快速搜资料、整理想法、推进博客发布
- 你部署在低成本 VPS 上，要求模型不可用时仍能继续运维
- 你想把个人知识体系和博客运营沉淀成可复用的开源项目，而不是一次性脚本

## 推荐工作流

1. 用 `/search` 收集信息，直接在对话里扫结果卡片
2. 用 `#` 消息录入灵感、问题和事实，再用 `/pkb` 快速检索
3. 用 `/mem set` 记录长期偏好、选题和成功模式
4. 用博客工具写入内容，必要时触发 `deploy-hugo.yml`
5. 用 `/status`、`/backup`、`/restore` 保持 VPS 稳定运行

## 设计原则

- 小原语优先：把高频运维动作做成稳定、可验证的本地指令，而不是依赖自然语言猜测
- 模型可替换：Vertex AI 只作为增强层，失败时系统仍可完整运转
- 卡片化输出：搜索、状态、博客发布都以 Lark 卡片呈现，减少碎片时间里的阅读成本

## 使用方式

1. 用 `/status` 快速判断服务是否健康
2. 用 `/search`、`#` 笔记和 `/pkb` 沉淀并检索碎片信息与个人知识体系
3. 用博客工具写入内容，必要时触发 `deploy-hugo.yml`
4. 用 `/backup`、`/restore`、`/restart` 保持低成本长期稳定运行
5. 当模型失效时，继续用大模型无关指令完成运维和发布

> `/search`、`/status`、博客发布和 `/schedule` 系列操作都会优先返回卡片，方便在 Lark 里快速扫读和确认结果。

## 开源协作

- 修改前先跑 `py -3 -m py_compile`，确保核心 Python 文件可编译
- 优先保持指令、卡片、工具 schema 的短小和一致，避免新增重复入口
- 提交 PR 时说明修改的用户路径，例如 `/search`、博客发布、恢复流程或状态页
- 如果要补配置，优先更新 `README.md` 和 `AGENTS.md`，让新贡献者能直接上手
- 仓库已通过 [`.editorconfig`](/D:/Geek/luck-agent/.editorconfig) 和 [`.gitattributes`](/D:/Geek/luck-agent/.gitattributes) 统一缩进、换行和尾随空格，尽量沿用现有格式，减少无意义 diff

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
ADMIN_USERS=ou_xxx,ou_yyy           # 可选；为空则不启用 Lark 用户白名单

# GitHub
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
GITHUB_OWNER=caozuohua               # 默认仓库 owner

# Hugo 博客（可选）
HUGO_REPO=caozuohua/caozuohua.github.io
BLOG_LOCAL_PATH=/var/www/blog

# Tavily 搜索（可选，无则自动 fallback 到 DuckDuckGo）
TAVILY_API_KEY=tvly-xxxxxxxx
TAVILY_API_KEY_2=tvly-yyyyyyyy

# 个人知识库（Vercel + Supabase）
PKB_BASE_URL=https://your-vercel-app.vercel.app
PKB_API_SECRET=your-api-secret
PKB_TIMEOUT_MS=10000
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
| 文件 | `/ls [路径]` | 列出文件区目录 |
| 文件 | `/cat <路径>` | 读取文件区文件内容 |
| 文件 | `/rm <路径>` | 删除文件（危险路径直接拦截，其他需确认）|
| 文件 | `/files` | 列出已接收文件 |
| 文件 | `/send <路径>` | 发送 VPS 文件到当前对话 |
| 知识库 | `# 内容` | 录入个人知识库（`[fact|idea|task|question|code]` + `#Topic` 可选） |
| 知识库 | `/pkb <关键词>` | 检索个人知识库 |
| Git | `/git [路径] [message]` | add + commit + push |
| GitHub | `/deploy [repo]` | 触发 GitHub Actions 部署 |
| GitHub | `/runs [repo]` | 查看 Actions 运行状态 |
| GitHub | `/posts [repo]` | 列出博客文章 |
| 搜索 | `/search <关键词>` | Web 搜索（Tavily 优先，多后端自动 failover）|
| 系统 | `/status` | 系统状态（内存/磁盘/进程）|
| 系统 | `/health` | 同 `/status`，兼容旧入口 |
| 系统 | `/logs [error\|warning] [小时数]` | 查询错误日志回溯 |
| 系统 | `/restart` | 重启 luck-agent 服务 |
| 系统 | `/journal [小时数]` | systemd 日志回溯 |
| 系统 | `/backup` | 备份 SQLite 和记忆配置 |
| 系统 | `/restore <备份名>` | 恢复备份 |
| 系统 | `/repair` | SQLite checkpoint + vacuum |
| 系统 | `/upgrade` | 拉取远程并重启 |
| 定时 | `/schedule list` | 查看定时任务（卡片）|
| 定时 | `/schedule add cron|interval <名称> "<cron|秒数>" <prompt>` | 新建定时任务（cron 为 5 字段，interval 至少 60 秒）|
| 定时 | `/schedule pause|resume|cancel <id>` | 管理定时任务 |
| 系统 | `/rollback <commit>` | 回退到指定提交 |
| 任务 | `/task <id>` | 查看任务状态 |
| 任务 | `/tasks` | 查看任务列表 |
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

## 常见故障

- Lark 发消息失败：先看 `/status`，再查 `/logs error 24`
- 文件发送失败：优先确认 Lark 权限、上传目录和文件大小限制
- 搜索结果为空：先缩小关键词，必要时换具体实体名、版本号或时间范围
- 博客发布失败：先看 GitHub 返回的错误，再查 `/runs` 和本地 `git status`
- 数据库变慢或膨胀：用 `/repair`，必要时 `/backup` 后再 `/restart`

---

## 验收清单

在真实 Lark 环境里，建议按下面顺序做一次完整验收：

1. `/search <关键词>`：看搜索卡是否返回，重点确认标题、来源和前几条结果可在手机端一屏扫完。
2. `#` 录入 + `/pkb` 检索：先发一条 `#` 笔记，再重复发送并确认提示“知识库中已有该内容”，最后用 `/pkb` 查回同一条内容。
3. 博客发布：先发一篇小改动，检查创建/更新结果卡是否展示，随后确认 `deploy-hugo.yml` 已触发。
4. `/status`：检查总览卡是否显示 WS、SQLite、备份、内存、磁盘和最近任务；`/health` 应返回同一张卡。
5. `/schedule add|list|pause|resume|cancel`：先建一个 60 秒以上的 interval 任务，再试 `list`、`pause`、`resume`、`cancel`，确认都只作用于当前用户，并检查卡片里的下一次触发时间/ETA。
6. `/send <file>`：先发送一个小文件，确认 Lark 中能收到文件卡，并且本地路径与文件名正确。

如果以上五条都稳定，说明这套智能体已经具备长期碎片化使用的基础。

模型可使用 `pkb_save`、`pkb_search`、`pkb_get`、`pkb_list`、
`pkb_update`、`pkb_delete` 和 `pkb_restore` 完成知识生命周期操作。
删除默认且仅支持软删除，必须先确认；可用 `pkb_restore` 恢复。
PKB 不可用时智能体会继续基于当前上下文回答，但不会声称已经读取或写入知识库。

---

## 维护要点

- `/schedule add cron ...` 只接受 5 字段 cron，字段范围有限制，错误会直接提示。
- `/schedule add interval ...` 最少 60 秒，避免短间隔任务拖高 VPS 负载。
- `/schedule pause|resume|cancel` 只作用于当前用户的任务，按 `user_id + task_id` 隔离。
- `/schedule list` 按启用状态和下一次触发时间排序，顶部会显示最近触发时间 / ETA。
- 所有 `schedule` 卡片都优先显示任务名、状态和下次触发时间，方便在 Lark 快速扫读。

## 许可证

MIT License
