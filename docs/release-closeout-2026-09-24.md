# 已上线版本收尾记录

决策日期：2026-09-24。用户要求保存历史评估到 GitHub，并完成已上线版本收尾，不再接入更多 Lark API。

## 交付边界

- 现有上线功能进入维护阶段；不新增 Lark API、OAuth scope 或新的平台能力入口。
- 历史 roadmap 中日历、任务、邮件等扩展项延期，不自动推进。
- 保留现有功能与权限边界。后续仅做必要修复、可靠性、结果验证和部署可复现性工作。
- 本次将审计报告、只读审计器、离线评分函数和测试纳入 Git。评估工具不接入生产热路径。
- 修正集成测试的通知等待条件：分别等待 Goal DONE 和通知回调，超时仍失败；不通过 sleep 掩盖问题，也不改变运行时行为。

## 线上复核

实际运行主机为 AWS `aws-codex-vps`，GCP 是运维目标，并不运行 luck-agent。

- `luck-agent.service`：active/running；启动于 2026-09-23 06:53:04 UTC，当前 systemd NRestarts=0。
- 回环 `/health` HTTP 200，process ok，SQLite connected。
- Goal 快照仍为 38 条：25 DONE、13 FAILED。65.8% 只能称状态完成率。
- 路由器报告 primary / newapi-gemini ready，active provider 为 newapi-gemini；这不是新的模型调用验收。
- 本次不发送 Lark 消息、不新增接口、不重启服务、不更新生产 checkout。

## 版本差异与已知债务

生产 HEAD 为 `73d94be28aed8e9bac0fd4f68e7d8f4a8f26d138`，分支 `feat/goal-runtime-langgraph`。
生产目录存在未提交的 services、Lark cards/commands/ws、main、memory/db、Mem0、文档、测试和
soul/MEMORY.md 修改，以及未跟踪的 capture 模块/测试。它们不是本次审计提交的内容，未覆盖或提交。

因此本次是维护边界和评估记录的收尾，**不宣称生产目录已与 GitHub 完全一致，也不创建可复现发行 tag**。
后续整理生产差异必须单独审查其功能和数据边界，不得直接 git pull/reset 覆盖现场，亦不得顺势扩展 Lark 接入。

已知运行风险继续保留在 [完整评估](luck-agent-history-audit-2026-09-24.md)：工具参数校验、失败 episode
重试计数、权限/配置错误分流、独立副作用验收、通知持久化、记忆 scope 和 graceful shutdown。
这些风险没有因为服务 active 或本次归档而被标记解决。

## 记录与复核方式

- `runtime/history_audit.py`：只读 SQLite/WAL 快照与 checkpoint 历史分析。
- `runtime/evaluation.py`：工具选择、参数 schema、独立效果证据评分；缺证据为 unknown。
- `tests/unit/test_history_audit.py`：审计与影子升级策略边界用例。
- `tests/integration/test_graph_runtime.py`：真实通知回调同步，保留失败检测。
- 原始数据库、私有运行输出、凭据、线上 MEMORY.md 和本地 workspace 审计 JSON 不上传。

验证命令：`pwsh ./scripts/test-local.ps1`（unit + integration，离线）。
最终结果：83 passed，2 个 Lark SDK 弃用警告；`git diff --check` 通过。
历史评估保留初次发现通知时序测试失败的事实。本次只修正该测试的同步条件，生产通知持久化缺口仍未解决。
