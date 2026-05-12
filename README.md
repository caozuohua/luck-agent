lark_agent/
├── agent.py              # 主入口，WebSocket + 路由
├── config.py             # 配置中心
├── requirements.txt
├── lark-agent.service
│
├── core/
│   ├── memory.py         # SQLite 持久化记忆
│   ├── task_queue.py     # 异步任务队列 + 状态机
│   ├── model_router.py   # 多模型路由 (pro/flash/lite)
│   └── session.py        # 用户会话管理
│
├── tools/
│   ├── github_tools.py   # GitHub 全套工具
│   ├── shell_tools.py    # Shell 执行 + 文件 I/O
│   └── file_bridge.py    # Lark ↔ VPS 文件收发
│
├── handlers/
│   ├── message.py        # 文本消息处理
│   ├── file_handler.py   # 文件消息处理
│   └── command.py        # /cmd 指令解析（大模型无关）
│
└── cards/
    └── builder.py        # Lark 消息卡片构建器
