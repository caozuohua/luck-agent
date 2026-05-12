## 项目总结

三轮对话，从零搭建了一个生产级 Lark × GCP 智能体系统。

---

### 第一轮：基础架构

设计了极简单进程框架，核心原则是在 e2-micro（1GB 内存）上跑稳：

- **Lark WebSocket 长连接**，lark-oapi 内置断线重连
- **ADC 鉴权**，实例绑 Service Account，无 key 文件
- **Secret Manager** 运行时拉取 Lark 凭证
- **TTLCache** 管理 Agent Session，无需 Redis
- **systemd** 保障进程存活，`Restart=always`
- 内存占用约 60MB RSS

---

### 第二轮：功能完善（3159 行，12 个文件）

在基础框架上扩展了完整能力栈：

| 模块 | 功能 |
|---|---|
| `core/memory.py` | SQLite WAL，5 张表：对话历史 / 用户画像 / 任务记录 / GitHub 日志 / KV |
| `core/model_router.py` | gemini-2.5-pro / flash / flash-lite 自动路由 + 故障降级 |
| `core/task_queue.py` | asyncio 优先队列，指数退避重试，完成后推卡片通知 |
| `tools/github_tools.py` | 11 个工具：Hugo 博文 / Actions / Issues / PR / 代码读写 |
| `tools/shell_tools.py` | 安全 Shell 执行（危险命令拦截）+ 文件 I/O |
| `tools/file_bridge.py` | Lark ↔ VPS 双向文件传输，纯 HTTP，无 AI 依赖 |
| `handlers/message.py` | ReAct 工具调用闭环，最多 6 轮 |
| `handlers/command.py` | `/sh` `/deploy` `/ls` `/send` 等直接指令，大模型不可用时仍工作 |
| `cards/builder.py` | 10 种 Lark 消息卡片（Shell 输出 / Actions 状态 / 任务进度等）|

**关键设计**：大模型不可用时，直接指令通道（`/` 前缀）完全独立运行，文件收发和 Shell 执行不受影响。

---

### 第三轮：重命名脚本

`rename.sh` 支持将项目彻底改名，自动处理四种命名变体：

```
lark-agent → luck-agent   (kebab-case，路径/服务名)
lark_agent → luck_agent   (snake_case，Python 模块)
LARK_AGENT → LUCK_AGENT   (UPPER_SNAKE，环境变量)
LarkAgent  → LuckAgent    (CamelCase，类名)
```

四步全自动：内容替换 → 文件重命名 → 目录重命名 → systemd 服务迁移重启。支持 `--dry-run` 预览和 `--local-only` 仅本地操作。

---

### 整体部署流程

```bash
# 1. 写入凭证
bash deploy.sh              # 首次，含 Secret Manager 配置

# 2. 热更新
bash deploy.sh --update     # 后续代码更新

# 3. 重命名
bash rename.sh lark-agent LuckAgent --dry-run   # 预览
bash rename.sh lark-agent LuckAgent             # 执行

# 4. 查看日志
journalctl -u luck-agent -f
```
