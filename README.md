# Luck Agent

Luck Agent 是一款基于 Python 的 Lark 机器人智能代理系统。通过 WebSocket 实时连接 Lark，集成 Gemini 多模型路由（pro/flash/lite）、异步任务队列、GitHub/Shell/搜索工具，支持 ReAct 风格的工具调用闭环和持久化记忆，可自动处理文本、指令和文件。

## Thanks to

- **GCP VPS (e2-micro, Free Tier) + Ubuntu** — hosting
- **Lark** — messaging channel
- **Vertex AI / Gemini (gemini-2.5-pro/flash/flash-lite)** — AI models
- **Claude (Free Tier)** — code design & generation

## 功能特性

- **Lark WebSocket 长连接**：自动收发消息，处理文本/文件/图片，群聊 @mention 解析
- **多模型智能路由**：根据关键词和消息长度自动选择 pro/flash/lite，支持故障切换链
- **ReAct 工具调用**：模型 → 工具执行 → 结果注入 → 继续推理，最多 6 轮循环
- **持久化记忆**：SQLite (WAL 模式) 存储对话历史、用户画像、成功工具调用模式
- **异步任务队列**：优先级队列 + 多 Worker + 指数退避重试 + 超时处理
- **定时任务调度**：Cron 风格定时任务，持久化到 SQLite
- **健康监控**：错误日志回溯、SQLite VACUUM、WS 心跳、系统资源预警
- **扩展工具链**：
  - GitHub 工具：REST API v3，连接池复用，429/5xx 自动重试
  - Shell 工具：异步执行，危险命令黑名单，输出截断
  - 文件桥接：Lark ↔ VPS 文件双向传输
  - 搜索工具：多后端轮询（DuckDuckGo/SearXNG/Qwant）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

需要 Python 3.10+。

### 2. 配置环境变量

创建 `.env` 文件（参考 `.env.example`），必填项：

```
GCP_PROJECT=your-project-id
LARK_APP_ID=your-app-id
LARK_APP_SECRET=your-app-secret
GITHUB_TOKEN=your-token
```

可选：`GCP_LOCATION`、`GOOGLE_APPLICATION_CREDENTIALS`、`GITHUB_OWNER`、`LARK_DOMAIN`、`HUGO_REPO`、`DB_PATH`、`SHELL_WORK_DIR`、`FILE_DIR`

### 3. 启动服务

```bash
python agent.py
```

### 4. 部署到 GCP

```bash
bash deploy.sh          # 首次部署
bash deploy.sh --update # 仅更新代码
```

如需以 Systemd 服务方式部署，可参考 `luck-agent.service` 配置模板。

## 目录结构

```text
luck-agent/
├── agent.py                # 主入口，WebSocket + 组件装配 + 优雅退出
├── config.py               # 配置中心（.env 加载 + GCP 认证检测）
├── deploy.sh               # GCP VPS 一键部署脚本
├── requirements.txt        # Python 依赖
├── core/                   # 核心功能模块
│   ├── log.py              # 结构化 JSON 日志（GCP Cloud Logging 兼容）
│   ├── memory.py           # SQLite 持久化记忆（对话/画像/任务/成功模式）
│   ├── model_router.py     # 多模型路由 + 故障切换 + 工具调用
│   ├── task_queue.py       # 异步优先队列 + Worker + 重试状态机
│   ├── scheduler.py        # 定时任务调度（SQLite 持久化）
│   └── health.py           # 健康监控（日志回溯/VACUUM/资源预警）
├── tools/                  # 工具集成
│   ├── github_tools.py     # GitHub REST API v3（连接池 + 重试）
│   ├── shell_tools.py      # Shell 执行 + 文件 I/O
│   ├── file_bridge.py      # Lark ↔ VPS 文件桥
│   └── search_tools.py     # 多后端 Web 搜索（DuckDuckGo/SearXNG/Qwant）
├── handlers/               # 消息/指令处理
│   ├── message.py          # AI 消息处理（ReAct 工具调用闭环）
│   ├── command.py          # 直接指令（/sh, /git, /deploy, /schedule 等）
│   └── file_handler.py     # 文件消息处理
└── cards/
    └── builder.py          # Lark Card 2.0 消息卡片构建器
```

## 用户指令

| 指令 | 说明 |
|------|------|
| `/sh <命令>` | 执行 shell 命令 |
| `/git [路径] [message]` | add + commit + push |
| `/deploy [repo]` | 触发 GitHub Actions deploy |
| `/runs [repo]` | 查看 Actions 运行状态 |
| `/schedule list/pause/resume/cancel` | 定时任务管理 |
| `/pro\|/flash\|/lite <消息>` | 强制指定模型 |
| `/task <id>` / `/tasks` | 查看任务状态 |
| `/mem` | 查看/管理记忆 |
| `/status` | 系统状态 |
| `/logs [error\|warning]` | 查询错误日志 |
| `/help` | 显示帮助 |

## 贡献指南

欢迎提交 Issue 或 PR 一起完善 Luck Agent，如需定制开发或讨论企业落地场景，请联系仓库维护者。

## License

本项目采用 MIT License 开源。
