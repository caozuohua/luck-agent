# Luck Agent 开发基线

更新时间：2026-08-22

本文是 `luck-agent` 当前开发的任务基线。除非明确调整产品定位，后续
开发按本文顺序推进；历史 V1、Vertex、单 VPS 和 Arkclaw 方案不属于当前
主线。

## 1. 产品目标

Luck Agent 是基于 Lark 国际版的多云 VPS 运维助手和 Lark 平台助手：

- 以 Lark 消息、卡片和确认交互为主要入口；
- 管理 AWS、GCP、Azure 等多个 VPS 和服务；
- 通过消息、文档、多维表格、表格、日历、邮件、任务、会议和知识库，
  降低手机端重复输入成本；
- 核心运维不依赖 LLM；
- LLM 只负责理解、规划和编排，不能成为单点控制面；
- 用户、群聊、目标主机和危险操作必须有明确权限边界。

## 2. 项目边界

### luck-agent 负责

- Lark WebSocket、消息卡片、命令路由和用户交互；
- Goal Runtime、任务状态、LLM 调用和免费额度容错；
- Mem0 记忆的业务语义、写入、搜索和删除；
- VPS 目标选择、权限确认和操作结果编排；
- Lark 平台 API 的业务流程。

### vps_sysops 负责

- 主机、Docker、systemd、网络、资源、日志、备份和安全运维；
- A2A、Mem0、new-api 等基础服务的部署和健康检查；
- 多云主机 profile 和可重复执行的运维脚本。

`vps_sysops` 保持独立项目。Agent 通过受控适配器调用固定能力，不能把
用户输入直接拼接为任意 Shell 命令，也不能把业务记忆写入运维项目。

## 3. 已完成基线

- V2（`main.py`）作为唯一正式架构；
- Lark 国际版 Bot `cli_aaba382935b8de18` 已完成真实 WebSocket 收发、卡片发送和重启恢复验收；
- AWS VPS 已部署当前 Agent；受控服务重启能力已通过固定入口、权限、审计和真实执行验收；
- 已有免 LLM 命令：`/ping`、`/health`、`/vps`；
- 已接入独立 vps_sysops 的只读适配器：
  `/vps status|resources|services|logs`；
- 适配器结果已统一为 `ok/partial/error` 三态；AWS、GCP、Azure 的资源路由已完成线上验证，
  日志权限不足时仍返回可读内容并标注为 `partial`；
- 已接入 Mem0：`/mem0 status`、`/mem0 list`、`/mem0 smoke`、`/mem0 search 关键词`；
- 已增加固定服务目录：`/vps service list`、`/vps service mem0 status|list|smoke|search`，以及
  A2A、new-api、Luck Agent 的宿主机服务清单入口；服务名和服务 allowlist 均固定校验；
- 已增加 A2A Agent Card 和 new-api `/models` 独立只读健康检查；A2A 探针通过目标 SSH 执行固定
  命令，不开放任意远程 Shell；GCP/Azure A2A 与 AWS new-api 已完成线上验证；
- Mem0 API Key、API health 和写入/搜索/清理 smoke 已验证；
- 本地新增适配器测试已通过；
- Graph 多步基线和 GoalStore 关闭竞态已修复，当前离线全套测试为 56 passed；
- Lark 已增加用户/群聊 allowlist 和一次性高风险请求确认；AWS 当前限制到已验证测试群；
- 危险工具已接入执行层二次审批：未带有效确认码不会执行，确认码一次性消费；
- 审批拒绝、放行和实际执行结果写入 SQLite `operation_audit`，Shell 审计不记录完整命令；
- 可选 `OPS_ALLOWED_TARGETS/SERVICES/OPERATIONS` 已接入工具执行层，越界操作在审批前拒绝；
- `OPS_ALLOWED_TARGETS` 同时限制 `/targets`、`/vps` 和 vps_sysops 只读入口，避免只读路径绕过目标授权；
- 可选 `OPS_ALLOWED_USER_IDS` 已接入运维权限层，生产已启用并按可靠 Lark `open_id` 限制 VPS/服务操作，普通 LLM 工具不受影响；
- `vps_sysops` 继续作为独立项目维护，不并入 Agent 仓库。
- 已开放受控的 `/vps service luck-agent restart`：一次性确认码、目标/服务/操作 allowlist、
  SQLite 审计和固定 sudo wrapper 均已接入；确认结果先发送，再由 systemd 延迟重启。
- 已开放受控的 `/vps service new-api restart`：固定 `new-api.service` 入口、目标/服务/操作
  allowlist、一次性确认和回滚说明均已登记；GCP 目标真实重启后 `active`，认证 `/v1/models`
  检查成功。
- 已开放受控的 `/vps service a2a restart`：GCP 使用固定 `hermes-a2a-bridge.service` 系统级入口，
  Azure 使用固定用户级 systemd 入口，AWS 目标在执行前拒绝；GCP 真实重启后 Agent Card probe
  返回 `gcp-hermeslite` `0.3.0`。
- 已开放 Azure-only `/vps service hermes-gateway restart`：使用固定用户级
  `hermes-gateway.service` 入口，真实重启后 `systemctl --user is-active` 返回 `active`；
  GCP/AWS 目标在执行前拒绝。
- 已开放受控的 `/vps service new-api backup` 契约：仅允许 `gcp-free-vps-oregon`，执行独立
  `vps_sysops` scoped backup 脚本；脚本生成 0600 的 `.env` 与 SQLite 在线快照归档，并在返回成功前
  完成 SHA-256、tar 可读性和 SQLite `integrity_check`。GCP 真实链路已验证成功；整机 profile backup
  因动态 Hermes 文件变化未采用。new-api restore/upgrade 仍保持关闭。
- 服务目录现在从固定操作契约动态展示可用动作和目标限制；新增受控操作不会再出现“已实现但目录不可发现”的状态。
- 变更契约现在强制包含固定入口、目标约束、前置条件、幂等性、回滚策略和验收标准；缺少任一
  核心字段的 backup/upgrade/rollback 操作不能登记。
- `/vps logs` 及其他 vps_sysops 长输出已支持短期、按用户隔离的 Card 2.0 分页；最多缓存 12 页，
  过期或越权令牌不会返回内容，原有日志回调保持兼容。
- `/vps service list`、Mem0 状态/smoke/search、new-api 和 A2A 探针已统一返回分段 Markdown
  Card 2.0，并保留文本摘要兼容旧发送器。
- Goal 后台任务终态回推已使用独立结果卡片，完成/失败分别采用绿色/红色状态和分段结果展示，
  不改变现有状态机、通知时机或执行语义。
- 普通自然语言/LLM 回复已统一使用分段 Markdown Card 2.0，超长正文按段拆分并保留完整内容。
- 正式 Lark 自然语言入口已接入 Goal Runtime + LangGraph：SQLite Goal、内存有界队列、启动恢复、
  `EXECUTING → AWAITING_RESULT → EVALUATING → DONE/FAILED` 状态闭环和终态回推均已有自动化测试；
  快捷命令继续保持独立的无 LLM 控制面。
- Goal Runtime 已补齐跨进程会话上下文恢复：按 Lark chat 读取历史 Goal/摘要并注入 LangGraph，
  服务重启后不再把连续对话当作新会话。
- 已统一 V2 运行时配置契约：systemd、Docker、部署脚本、数据库维护 wrapper 和 `.env.example`
  使用同一服务用户、入口、数据目录和工作目录，并增加脚本契约回归测试。
- 已接入多 Provider LLM Router：兼容原有 `LLM_*` primary 配置，支持按顺序配置备用
  `LLM_PROVIDER_<NAME>_*`；每个 Provider 独立维护重试、普通故障熔断和配额长冷却，
  fallback 只发生在 LLM 生成/修复阶段，不重复执行工具副作用。
- 已在 AWS 生产机真实验证 GCP new-api 的 `step-3.7-flash → gemini-2.5-flash` fallback；
  Hermes 的 OpenRouter key 读取 `/models` 成功但 completion 返回 402，已按配额故障排除。
- `/health` 已报告 active Provider、各 Provider 状态、失败次数和 cooldown 原因，且不暴露
  endpoint 或密钥；快捷 `/health` 同步显示当前可用 Provider 数量。

## 4. 当前执行顺序

### 阶段一：稳定性和真实验收（主体完成，持续线上观察）

1. 已修复 Graph 多步集成测试失败，并处理异步状态写入关闭竞态；
2. 完成 LLM 429、限额耗尽、超时、5xx、空响应和上下文过长的降级；
3. 为模型调用增加超时、重试上限、熔断和冷却，不重复执行有副作用工具；
4. 固化 AWS Lark 真实验收：收发、卡片、快捷命令、异常恢复和重启恢复；
5. 统一本地、测试、systemd 和部署脚本的运行时配置。

阶段完成标准：

- 离线全套测试无未解释失败；
- AWS systemd 服务 active，WebSocket 稳定连接；
- 核心快捷命令不调用 LLM；
- LLM 不可用时，健康检查和只读运维仍可用。

当前阶段状态：已完成。Graph 基线、Goal Runtime 首条生产执行链、LLM 客户端基础容错、多 Provider 路由与配额级熔断、
AWS 重启恢复、三目标只读路由、真实 Lark 基础链路和运行时配置一致性均已完成；后续只保留线上观察和故障回归。

### 阶段二：权限和无 LLM 运维控制面

已完成：

- Lark 用户/群聊 allowlist；
- 高风险请求的一次性确认码和过期机制；
- 未授权消息不进入 Agent，也不发送回复。

剩余收尾：

1. 核心命令结果已统一为结构化卡片，后续只做细分模板优化；
2. Luck Agent、new-api、A2A 和 Azure Hermes Gateway 已有受控变更入口；其他服务变更仍需逐项定义固定入口、回滚策略和验收测试，不能复用任意 Shell；
3. 生产运维用户白名单已启用；重启确认卡已支持一键确认并完成真实 Lark 验收，备用验证码已
   支持独立展示和只粘贴验证码确认，并完成真实 Lark 验收；
4. 确认卡已显示实际目标；`new-api` 已绑定 GCP 目标并在执行前拒绝错误目标；用户目标选择
   已按 `user_id + chat_id` 持久化，重启恢复和真实 Lark 一键重启均已验收。

已完成：

- 工具执行级审批拦截，覆盖危险工具和常见变更型 Shell；
- 操作者、操作摘要、审批决策、执行状态和时间写入审计表。
- 可选目标、服务和操作白名单已接入，未配置时不影响现有运行。
- 确认码已绑定请求中明确的操作、目标和服务；未明确字段按通配处理，工具不匹配时拒绝并消费确认码。
- Lark Card 2.0 已增加移动端标题和状态模板，保留 Markdown 正文兼容现有消息发送链路；
- `VpsTarget` 已统一 provider/account/region/target/role 元数据，并传入本机状态与 vps_sysops 适配器。
- `VPS_TARGETS` 已支持注册多个目标；`/targets` 返回 Card 2.0 下拉框，按 Lark 用户保存选择，
  同时保留 `/target TARGET_ID` 文本后备命令；Lark `select_static` 回调已接入。
- `VpsTarget` 已支持可选 `ssh_host/ssh_user/ssh_port/sysops_root`；vps_sysops 适配器可按固定 allowlist
  构造 SSH 远程脚本调用，未配置通道的远程目标会安全拒绝，不再误执行本机检查。
- AWS、GCP、Azure 三个目标的 `resources` 已通过 Agent 适配器真实执行验证；`logs` 的非零退出
  已按可读报告与权限不足场景归类为 `partial`，不再只显示原始退出码。

### 阶段三：多云目标模型

统一使用：

```text
Provider → Account → Region → Target → Service → Operation
```

AWS、GCP、Azure 的只读目标路由、固定服务目录和独立健康检查已完成；Luck Agent、GCP
new-api 的 restart/backup、A2A 与 Azure Hermes Gateway 的受控变更路径已完成。下一步继续扩展其他服务的健康检查/变更审批，并保持每项能力独立验收。
Agent 不直接实现各云厂商的主机运维细节，而是调用 vps_sysops profile 和适配器。

### 阶段四：Mem0 和任务记忆策略

1. 明确自动记忆、用户确认记忆和临时上下文的边界；
2. 增加记忆保存、搜索、删除和失败降级的用户体验；
3. 避免每条消息都触发 LLM 或 Mem0 写入；
4. 记忆服务不可用时，不阻塞普通运维任务。

当前进展：阶段四核心能力已完成。已完成显式命令边界、第一版浏览体验、可配置 scope 和自动记忆提议：`/mem0 save|remember`、`/mem0 delete MEMORY_ID` 仅在一次性确认后执行；`/mem0 list`、`/mem0 search` 和普通消息保持只读。明确的“请记住/个人偏好”只展示提议卡，不自动调用 LLM/Mem0；提议卡可一键发起确认，用户只需再确认一次。`MEM0_SCOPE_MODE=configured` 保持现有固定 user，`lark_user` 可按 Lark open_id 隔离读写，并要求删除目标先在当前 scope 被观察到。保存、删除和浏览失败会降级为提示，不阻塞其他任务。

临时上下文边界已补强：`context_summaries` 与 Goal 历史均按 `user_id + chat_id` 隔离，不跨群聊复用。Mem0 项目 scope 已支持 `/mem0 scope` 查看、`/mem0 scope PROJECT_ID` 切换，允许项目由 `MEM0_PROJECTS` 配置，选择按 `user_id + chat_id` 持久化并显示在操作结果中；临时上下文仍不会写入 Mem0。测试环境已配置 `luck-agent,hermes-test` 两个项目，真实 Lark scope 切换和重启恢复已验收。

### 阶段五：Lark 平台能力

当前进展：已完成 `/lark chat`、`/lark messages [数量]`、`/lark chat members [数量]` 和
`/lark chat announcement` 四个只读能力；仅读取当前事件会话，消息/成员最多返回 10 条摘要，不暴露原始 ID，
并已通过 Bot 测试会话的 REST 探测。Wiki 搜索需要 `user_access_token`，当前暂不引入 OAuth；后续继续按单能力、
单权限、单验收推进。

按优先级逐步接入消息卡片、文档、多维表格、表格、日历、任务、邮件、
会议和知识库。每次只引入一个可验收的只读或低风险能力，再开放写操作。

涉及个人邮件、日历、私信和个人任务时，单独设计 User OAuth，不直接扩大
Bot 身份权限。

### 阶段六：架构和文档收敛

- 更新 `README.md`、`docs/current-state.md` 和当前开发边界文档；
- 将仍描述 V1/Gemini/单 VPS 的材料明确标记为历史说明；
- 评估未接入的重复运行时、队列和健康检查实现；
- 保持一套正式 Goal Runtime 和一套任务执行路径。

当前状态：主体完成，进入兼容观察期。生产正式自然语言入口使用 Goal Runtime + LangGraph，快捷命令作为独立无 LLM 控制面保留；本地 Web、legacy_inline skill 和旧 GoalManager 已登记为兼容边界，历史文档已明确不再作为当前事实来源。满足删除条件后再单独移除旧代码。

## 5. 明确不做

- 暂不考虑 Arkclaw；
- 不把 vps_sysops 合并进 luck-agent；
- 不开放任意远程 Shell；
- 不一次性接入全部 Lark 平台 API；
- 不在免费额度不稳定时依赖 LLM 执行基础健康检查；
- 不为了兼容 V1 继续扩大旧架构。

## 6. 维护规则

每次开发任务结束时，至少更新以下内容之一：

- `docs/current-state.md`：事实状态、部署状态和已验证结果；
- 本文件：阶段进度、阻塞项和验收标准；
- 测试或部署记录：对应功能的可重复验证方式。

本轮执行顺序固定为：

1. 修订本文件和 `docs/current-state.md` 的状态描述；
2. 双 Mem0 项目测试环境与真实 Lark scope 验收；
3. 核验操作者 `open_id` 并评估生产用户白名单；
4. 扩展一个固定入口、可回滚的低风险服务变更；
5. 清理或隔离 legacy runtime 与历史文档。

如果新需求与本基线冲突，先明确调整目标和优先级，再开始实现，避免在
稳定性、权限和多云基础尚未完成前无序扩展平台功能。
