# Luck Agent

Luck Agent 是一款基于 Python 的 Lark 机器人智能代理系统。支持 WebSocket 实时通信，集成多种大模型路由、任务队列、GitHub 和 Shell 工具，可自动处理文本、指令文件，实现高效的企业自动化协作。

- **GCP VPS(e2-micro, Free Tier)+Ubuntu**
- **Lark**
- **Vertexai/Agent Platform**

## 功能特性

- **Lark 消息对接**：自动收发 Lark 消息，处理文本和文件，支持自定义命令解析。
- **多模型路由**：内置多模型（pro/flash/lite）智能切换，按需分发任务。
- **持久化记忆**：基于 SQLite 实现历史记录和用户会话记忆。
- **任务和状态管理**：异步任务调度与状态机，保证任务按序执行。
- **扩展工具链**：
  - GitHub 工具：仓库操作与自动化接口
  - Shell 工具：服务器脚本与文件 I/O
  - 文件桥接：Lark 与 VPS 之间的文件分发

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 自定义配置

编辑 `config.py`，配置 Lark App、数据库路径等参数。

### 3. 启动服务

```bash
python agent.py
```

如需以 Systemd 服务方式部署，可参考 `lark-agent.service` 配置模板。

## 目录结构

```text
luck-agent/
├── agent.py                # 主入口，WebSocket + 路由
├── config.py               # 配置中心
├── requirements.txt        # Python 依赖
├── lark-agent.service      # Systemd 启动模板
├── core/                   # 核心功能模块
│   ├── memory.py           # SQLite 持久化记忆
│   ├── task_queue.py       # 异步任务队列 + 状态机
│   ├── model_router.py     # 多模型路由 (pro/flash/lite)
│   └── session.py          # 用户会话管理
├── tools/                  # 工具集成
│   ├── github_tools.py     # GitHub 工具
│   ├── shell_tools.py      # Shell 执行 + 文件处理
│   └── file_bridge.py      # Lark ↔ VPS 文件桥
├── handlers/               # 消息/指令处理
│   ├── message.py          # 文本消息
│   ├── file_handler.py     # 文件消息
│   └── command.py          # 指令解析
└── cards/
    └── builder.py          # Lark 消息卡片构建器
```

## 贡献指南

欢迎提交 Issue 或 PR 一起完善 Luck Agent，如需定制开发或讨论企业落地场景，请联系仓库维护者。

## License

本项目采用 MIT License 开源。
