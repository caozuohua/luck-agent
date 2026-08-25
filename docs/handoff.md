# Luck Agent 会话交接

更新时间：2026-08-24

本文件用于新 Codex 会话快速接手当前开发，不包含密钥、Token 或业务数据。

## 一、当前目标

Luck Agent 的产品定位已经从“Lark API/运维命令集合”收敛为：

> 以 Lark App 为统一入口，覆盖个人工作、生活、知识、日程和数字资产管理，同时保留安全的多云运维能力。

核心体验是：用户在手机上自然表达意图，系统识别并路由到对应能力，必要时用短卡片补充信息或请求确认，
然后把结果执行、整理或沉淀到合适位置。六类场景——工作纪要、英语学习、Agent 学习、Idea、个人博客、
日程/提醒——是首批高频样板，不是封闭功能边界。

## 二、当前实际数字资产

### 云与主机

- GCP：`gcp-free-vps-oregon`
- Azure：`az-free-vm`
- AWS：`aws-codex-vps`

### 三台 VPS 实际业务服务

- GCP：Hermes Gateway、Hermes A2A Bridge、A2A MCP companion、new-api、x-ui、Xray、Nginx、Docker。
- Azure：用户级 Hermes Gateway、用户级 Hermes A2A Bridge；无 Docker、Nginx、new-api、x-ui 在线服务。
- AWS：Luck Agent、Mem0 API、Mem0 Dashboard、Mem0 PostgreSQL/pgvector 三容器 Compose 栈。
- 三台均有 Tailscale、SSH、fail2ban/系统安全组件和 vps_sysops 监控；AWS 另有 Mem0 PostgreSQL 每日备份 timer。
- `transparent-agent`、`agent-framework-lab` 等目录目前是项目资产，不代表 active 服务。

### 其他个人资产

- GitHub：blog、portal 等项目，后续接入仓库状态、Actions、部署和博客工作流。
- Lark：日程、QPC 个人知识碎片多维表格。

## 三、架构和部署事实

- 正式入口：`/opt/luck-agent/main.py`，systemd 服务名 `luck-agent.service`。
- 正式生产执行模式：`EXECUTION_MODE=graph`。
- 普通自然语言：SQLite Goal → 有界队列 → LangGraph 单 Goal 执行 → 原会话回推。
- 快捷命令：无 LLM 的同步控制面。
- Legacy Web、`legacy_inline`、旧 `GoalManager`/`ExecutionEngine` 仅为兼容路径，不是生产主链。
- vps_sysops 是独立项目，Luck Agent 只能通过固定 allowlist/适配器调用，不能接受任意 Shell。
- Mem0 业务记忆由 Luck Agent 管理；vps_sysops 负责 Mem0 服务运维。
- 生产 LLM：primary `stepfun-ai/step-3.7-flash`，备用 `newapi-gemini` `gemini-2.5-flash`，当前均 ready。
- User OAuth Token 只驻留内存；服务重启后需要重新授权。

## 四、已完成并验收的能力

### 运行时、运维和安全

- Lark WebSocket、消息、Card 2.0、重启恢复和真实消息链路已验收。
- GCP/Azure/AWS 三目标只读资源、日志、服务目录和目标选择已完成。
- 已验证 vps_sysops 独立项目接入和目标路由。
- 已受控开放：Luck Agent restart、GCP new-api restart/backup、A2A restart、Azure Hermes Gateway restart。
- 变更入口均有目标/服务/操作 allowlist、一次性确认、审计、固定入口和回滚说明。
- Provider Router、429/余额/配额/超时/5xx 冷却和备用 Provider 已完成。
- 双 Mem0 project scope、用户/会话隔离、显式保存/删除确认和自然语言记忆提议已验收。

### Lark 平台只读链路

- `/lark chat`
- `/lark messages [数量]`
- `/lark chat members [数量]`
- `/lark chat announcement`
- `/lark auth` User OAuth
- `/lark wiki 关键词` 搜索：真实验收通过
- `/lark wiki get <链接>` 节点详情：真实验收通过
- `/lark wiki summary <链接>`：Bitable 摘要、Docx 文本摘要和 Docx 结构摘要，真实验收通过
- `/lark wiki records <链接> [表名]`：代码完成，脱敏和限制已实现，但尚未完成最后一次真实 Lark 消息验收

### 最近代码验证

- records 功能后的完整测试：`65 passed, 2 warnings`。
- 最近一次生产健康检查：systemd active、`/health` 200、SQLite connected、两个 LLM provider ready。
- 当前健康统计曾显示 `recent_total=29`、`done=21`、`failed=8`，成功率约 72.4%；这不是当前阻塞项，但后续应分析失败 Goal。

## 五、当前最大 GAP

### 1. 三台 VPS 服务资产目录尚不完整

`/vps service list` 目前是“受控操作目录”，不是完整资产目录。尚未统一呈现：

- systemd/用户级 systemd 服务
- Docker Compose 与容器
- 监听端口和访问边界
- 健康端点
- 服务依赖
- 日志入口
- 备份与恢复对象
- 哪些服务只读、哪些允许变更

下一步应先实现只读资产发现/服务目录，再决定是否开放新的 restart/backup 操作。不要因为发现服务就自动增加变更权限。

### 2. 统一意图入口尚未落地

工作纪要、Idea、日程、QPC、英语学习、Agent 学习、博客和 GitHub 还没有统一的意图注册与场景路由层。
需要避免每增加一个场景就新增一套复杂命令和确认交互。

### 3. 真实场景闭环尚未开始

首个建议闭环：工作纪要 + Idea 快速捕获。输入应尽量是一条手机消息，结构化处理异步完成，仅在持久化、
外部写入、日历写入或发布时确认。

## 六、建议新会话执行顺序

1. 先阅读本文件、`docs/roadmap.md`、`docs/current-state.md` 和 `docs/legacy-runtime.md`。
2. 只读实现三台 VPS 服务资产发现，输出统一服务模型和 `/vps service list` 展示；不要先开放更多变更操作。
3. 在浏览器方便时完成 `/lark wiki records` 的真实验收。
4. 设计统一意图注册/路由层，优先实现工作纪要和 Idea 快速捕获。
5. 接入 Lark 日程与 QPC，形成个人知识和提醒闭环。
6. 再按优先级推进英语学习、Agent 学习、GitHub/blog/portal、个人博客等场景。

## 七、版本和工作区注意事项

- Luck Agent 工作目录：`/opt/luck-agent`
- 当前分支：`feat/goal-runtime-langgraph`
- 远程已同步到：`bf2fcbb docs: inventory services across all vps`
- Luck Agent 当前唯一未提交文件：`soul/MEMORY.md`，属于用户已有变更，不能覆盖、回滚或混入提交。
- `/opt/vps_sysops` 当前存在大量用户未提交变更；本轮没有修改或整理它，接手时必须先查看其 `git status`，不要 reset/checkout/clean。
- 文档和代码文件可能由 `luck-agent` 用户持有；编辑前按现有权限流程临时授权，完成后恢复权限。
- 只读调研不要输出 `.env`、API key、OAuth token、数据库内容、x-ui 隐藏路径或个人知识正文。

## 八、交接判断

当前基础设施、运行时、权限、Mem0 和 Lark 第一批只读能力已经具备继续开发条件；主线已经从“补 API”转为：

> 完整掌握真实资产 → 建立统一只读资产目录 → 建立低复杂度意图入口 → 围绕个人工作与生活持续扩展。
