# Luck Agent 当前实现与路线

更新时间：2026-08-22

## 产品定位

Luck Agent 是基于 Lark 国际版的多云 VPS 运维与 Lark 平台助手。

目标是把高频操作从手机端打字，转化为 Lark 消息卡片、按钮、下拉框和确认表单：

- 管理 GCP、AWS、Azure 等多个云厂商下的 VPS；
- 查看状态、日志、资源和任务进度；
- 执行启动、停止、重启、部署、回滚、备份和恢复；
- 操作 Lark 消息、文档、多维表格、表格、日历、邮件、任务、会议和知识库；
- 在 LLM 限额或故障时，核心运维仍可继续工作。

当前测试目标 Bot：`cli_aaba382935b8de18`。

## 架构事实

- V2（`main.py`）是唯一正式架构；V1 已退出路线图。
- LLM 使用 OpenAI-compatible `/chat/completions`，没有配置模型时使用离线 FakeLLM，仅用于本地开发和测试。
- LLM 是增强层，不是控制面。无 LLM 时，规则化 VPS 运维和安全确认流程必须可用。
- OpenAI-compatible 客户端已支持可配置超时、有限重试、429/5xx/空响应处理和冷却熔断；401/403 等请求错误不重复重试。
- SQLite 保存 Goal、上下文、模式和运行时状态。
- Lark 普通自然语言已接入正式 Goal Runtime：消息先写入 SQLite Goal，再进入内存有界队列和
  LangGraph 执行器；Goal 状态由 `GoalStore` 维护，进程启动会恢复所有用户的非终态 Goal，终态结果
  按原 chat_id 回推 Lark。快捷命令仍走无 LLM 的同步控制面；本地 Web 暂保留直接 Agent 调试入口。
- LangGraph 已抽成单 Goal 执行边界，使用 `user_id:goal_id` 作为 checkpoint thread；Runtime 负责
  调度、并发上限、状态闭环、异常失败和通知，避免与旧同步 `core.goal.GoalManager` 链路混用。
- Runtime 执行前会按 `user_id + chat_id` 从已完成 Goal 恢复最近对话，并合并已有上下文摘要；
  旧数据库中尚未记录 chat_id 的历史会在首次迁移后兼容回退，避免服务重启造成会话断裂。
- 正式部署契约已统一为 `/opt/luck-agent`、`luck-agent` 系统用户、`/opt/luck-agent/data` 和
  `/opt/luck-agent/workspace`；systemd、Docker、部署脚本和备份/升级 wrapper 均按 V2 `main.py`
  入口和该路径执行，避免旧 V1 `agent.py`、`/home/agent` 和 `memory.db` 路径混用。
- LLM 已由 `LLMProviderRouter` 统一调度；`LLM_*` 继续作为 primary，备用 Provider 使用
  `LLM_PROVIDER_<NAME>_*`。Provider 自己维护普通故障熔断，429/余额/配额耗尽进入独立长冷却，
  Router 只在模型生成或 JSON 修复阶段切换 Provider。
- `/health` 现在报告 active Provider、各 Provider 模型、状态、失败次数和 cooldown 原因；
  不返回 endpoint 或密钥，便于在不调用 LLM 的情况下判断当前降级状态。
- `interface/` 当前包含 Lark 消息处理核心和本地 Web 测试接口；AWS 生产环境已完成真实 Lark WebSocket 收发和卡片发送验证。
- Lark 回复已使用 Card 2.0 标题、状态颜色和 Markdown 正文；`/targets` 会返回
  VPS 目标下拉框，`select_static` 回调已接入用户级目标切换；其他变更运维按钮仍未开放。
  `/vps logs` 及其他 vps_sysops 长输出超过单页长度时返回带上一页/下一页 callback 的分页卡片，
  分页令牌按用户隔离并在 10 分钟后过期。
- `/vps service list`、Mem0 状态/smoke/search、new-api 和 A2A 探针现在返回分段 Markdown
  Card 2.0，同时保留原始文本摘要供旧发送器和测试兼容。
- 后台 Goal 终态回推现在使用独立任务结果卡片：完成为绿色、失败为红色，Goal 标识和结果/错误
  分段展示；不改变 Goal 状态机和通知时机。
- 普通自然语言/LLM 回复现在使用分段 Markdown Card 2.0；超过单段长度时按段拆分，保留完整正文
  和尾部内容，不走首尾截断。
- `interface/lark_sdk.py` 已在 `main.py` 中完成生产 WebSocket 接线，并同时注册消息和卡片回调；
  `core/lark_ws_runner.py` 保留为独立生命周期测试封装。
- 生产 `EXECUTION_MODE` 使用 `graph`；本地 Web 直连 Agent、`legacy_inline` skill、旧
  `GoalManager` 和早期 `ExecutionEngine` 已明确登记为兼容路径，不是生产主链，边界见
  [`docs/legacy-runtime.md`](legacy-runtime.md)。
- `tools/vps_sysops.py` 通过固定 allowlist 调用独立的 vps_sysops 项目，不接受用户任意 Shell。
  非日志输出按 `VPS_SYSOPS_MAX_OUTPUT_CHARS` 限制并保留首尾；日志按单页长度切分，最多缓存 12 页；结果提供
  `ok/partial/error` 三态和 `as_dict()` 结构化契约；日志脚本因系统日志权限产生的部分失败会明确标注。
- `tools/mem0_client.py` 提供 Mem0 health、smoke 和 search；Mem0 业务记忆仍由 Agent 负责，vps_sysops 只负责服务运维。
- 已增加显式 Mem0 记忆边界：`/mem0 save|remember` 和 `/mem0 delete MEMORY_ID` 需要一次性确认；普通消息、搜索和 `/mem0 list` 不触发 Mem0 写入。`/mem0 list` 展示当前 scope；`MEM0_SCOPE_MODE=configured` 保持兼容的固定 user，`lark_user` 按 Lark open_id 隔离读写，并要求删除目标先在当前 scope 的 list/search/save 结果中出现。Mem0 不可用时仅返回降级提示。
- Mem0 项目 scope 现在通过 `/mem0 scope` 查看、`/mem0 scope PROJECT_ID` 切换；允许的项目由 `MEM0_PROJECTS` 配置，选择按 `user_id + chat_id` 持久化，所有保存/搜索/删除/smoke 均显式带入项目 scope。生产当前配置 `luck-agent,hermes-test`，双项目 smoke、真实 Lark scope 切换和重启恢复均已验收。
- 自然语言中的明确“请记住/个人偏好”现在只生成记忆提议卡，不调用 LLM 或 Mem0；用户点击“发起保存确认”后只需确认一次，仍复用 `/mem0 save` 的执行层校验。
- 临时上下文现在按 `user_id + chat_id` 隔离：Goal 历史和压缩摘要不会跨群聊/会话串用；临时上下文仍保存在 SQLite/运行时，只有显式确认的业务记忆才进入 Mem0。
- `core/services.py` 提供固定服务目录；`/vps service mem0 status|list|smoke|search` 调用 Mem0 API，
  `a2a` 通过目标 SSH 上的固定 Agent Card probe 检查，`new-api` 只读访问 `/v1/models`，
  `luck-agent` 可查看宿主机服务状态；`/vps service luck-agent restart`、
  `/vps service new-api restart`、`/vps service a2a restart` 和 Azure-only
  `/vps service hermes-gateway restart` 是当前开放的变更操作，必须通过一次性确认、目标/服务/操作
  allowlist 和固定入口；不接受任意服务名或 Shell。
- `core.services.SERVICE_OPERATIONS` 已为 `luck-agent`、`new-api`、`a2a` restart 以及 `new-api backup` 登记固定入口、前置条件、
  幂等性、回滚策略和验收定义；`new-api backup` 另有独立契约，仅绑定
  `gcp-free-vps-oregon`，通过 `/opt/vps_sysops/scripts/new_api_backup.sh` 生成 0600 归档，脚本内完成
  SHA-256、tar 可读性和 SQLite `integrity_check`，不开放升级或恢复。
- Lark 入口已支持用户/群聊 allowlist 和一次性高风险请求确认；重启确认卡已支持一键确认，真实
  Lark 已验证按钮回调、GCP `new-api` 重启和结果回卡；备用验证码已改为独立代码块展示，
  支持只粘贴验证码完成确认，并已通过真实 Lark 验收。
- 危险工具在执行层再次校验确认码；拒绝、批准和执行结果写入 SQLite `operation_audit`，确认码只消费一次。
- 可选 `OPS_ALLOWED_TARGETS`、`OPS_ALLOWED_SERVICES`、`OPS_ALLOWED_OPERATIONS` 已接入工具执行层；
  `OPS_ALLOWED_TARGETS` 同时限制 `/targets`、`/vps` 和 vps_sysops 只读入口，空配置保持兼容。
- 可选 `OPS_ALLOWED_USER_IDS` 已接入 VPS 运维权限层；生产已配置并按已核验的 Lark `open_id`
  限制 VPS、服务和变更操作，普通 LLM 工具不受影响。
- AWS 生产当前已显式允许 `aws-codex-vps`、`gcp-free-vps-oregon`、`az-free-vm` 三个目标；
  新增目标前必须同步更新 allowlist。
- VPS 已使用 `VpsTarget(provider/account/region/target_id/role)` 统一描述目标，并将
  元数据传给状态与 vps_sysops 适配器；`VPS_TARGETS` 支持注册多个目标，当前选择按
  `user_id + chat_id` 持久化到 SQLite；目标还可附带 `ssh_host/ssh_user/ssh_port/sysops_root`，
  用于受控远程执行。未配置远程通道时，Agent 会拒绝把 AWS 本机资源错误标记成 GCP/Azure。
- AWS、GCP、Azure 三个已配置目标的 `resources` 远程执行已在线验证成功；三个目标的 `logs`
  均可返回可读报告，权限不足部分以 `partial` 状态呈现。
- AWS 上的 `luck-agent` 受控重启已在线验证：确认结果先返回，3 秒后由 systemd 调度重启；
  wrapper 只允许 `luck-agent` 调用固定服务重启入口，服务重启后保持 `active`。
- GCP `new-api` 受控重启已在线验证：adapter 仅执行固定 `new-api.service` 入口，重启返回
  `active`，随后认证 `/v1/models` 检查成功。
- GCP A2A 受控重启已在线验证：adapter 仅执行固定 `hermes-a2a-bridge.service` 入口，重启返回
  `active`，随后 Agent Card probe 返回 `gcp-hermeslite` `0.3.0`；Azure 使用用户级 systemd
  入口，AWS 目标在审批前拒绝。
- Azure Hermes Gateway 受控重启已在线验证：adapter 仅执行固定用户级
  `hermes-gateway.service` 入口，重启后 `systemctl --user is-active` 返回 `active`；GCP/AWS
  目标在执行前拒绝。
- 重启确认卡会显示实际目标；生产 `new-api` 已绑定 `gcp-free-vps-oregon`，目标不匹配时在
  执行前拒绝；目标选择持久化、确认卡目标展示和真实 Lark 一键重启均已验收。
- GCP `new-api` scoped backup 已在独立 `vps_sysops` 项目提交并部署，真实归档生成、SHA-256 校验、
  tar 解包和 SQLite 完整性检查均通过；Luck Agent 路由已加入同一套一次性确认、目标/服务/操作
  allowlist 与审计链路。此前整机 profile backup 因 Hermes 动态文件变更而失败，未作为 new-api
  备份入口使用。

## 目标对象模型

多云 VPS 不应再以单台主机或单一 GCP 实例建模。后续统一使用：

```text
Provider  = GCP | AWS | Azure
Account   = 云账号或项目
Region    = 区域
Target    = VPS / instance
Role      = prod | staging | personal | other
```

凭证只存放在服务端配置、密钥管理系统或云厂商授权机制中，不进入 LLM 上下文、卡片内容和普通日志。

## 无 LLM 运维控制面

第一优先级能力必须由规则、权限和卡片驱动：

- 实例选择、连通性检查和资源状态；
- 服务启动、停止、重启；
- 日志查看、分页和错误摘要；
- Git 更新、部署和回滚；
- 备份、恢复和 SQLite 修复；
- 危险操作的用户隔离、二次确认和审计记录。

标准流程：

```text
Lark 卡片 → 选择云厂商/账号/实例 → 权限与参数校验 → 直接执行 → 卡片返回结果
```

## LLM 容错目标

后续应增加 Provider Router，而不是只依赖单个客户端重试：

- 多 Provider、API Key 和模型备用链；
- 识别 429、配额耗尽、超时、5xx 和上下文过长；
- Provider 熔断、冷却和恢复探测；
- 每个 Goal 的步骤、时长和 token 上限；
- 有副作用的工具不得因模型重试而重复执行；
- LLM 全部不可用时，降级到规则命令或人工确认。

## Lark 平台助手边界

Bot 身份用于公共团队资源和消息卡片；涉及个人邮件、日历、私信和个人任务时，应单独设计 User OAuth 与权限范围。lark-cli 作为候选平台工具层，暂不进入核心运维控制面。

## 当前待办与阻塞项

1. 其他服务的变更入口仍需逐项定义固定入口、回滚策略和验收测试；当前已开放 `luck-agent`、
   `new-api` restart/backup、`a2a` 和 Azure-only `hermes-gateway`；new-api restore/upgrade 仍未开放。
2. 目标选择、确认卡目标展示和 `new-api` 的 GCP 目标绑定已完成；后续可将卡片选择改为选择
   后立即落库，消除“选择后尚未发送下一条消息时进程重启”的极小窗口。
3. 旧 SPEC、DOCX 手册和 `docs/superpowers/` 仍保留历史设计，但已不作为当前事实来源；兼容代码进入观察期，满足条件后再删除。

## 实施顺序

详细基线见 [`docs/roadmap.md`](roadmap.md)。当前顺序为：

1. 修订路线和状态文档（已完成）；
2. 完成双项目 Mem0 scope 的测试环境及真实 Lark 验收（已完成）；
3. 核验并启用生产用户级运维白名单（已完成）；
4. 增加并验收一个固定入口、可回滚的低风险服务变更（已完成）；
5. 清理或隔离 legacy runtime 与历史文档（已完成隔离，进入观察期）。
