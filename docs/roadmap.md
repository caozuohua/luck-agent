# Luck Agent 开发基线

更新时间：2026-08-20

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
- Lark 国际版 Bot `cli_aaba382935b8de18` 已完成 WebSocket 收发和卡片发送；
- AWS VPS 已部署当前 Agent，生产 commit 为 `cf6a9dc`；
- 已有免 LLM 命令：`/ping`、`/health`、`/vps`；
- 已接入独立 vps_sysops 的只读适配器：
  `/vps status|resources|services|logs`；
- 适配器结果已统一为 `ok/partial/error` 三态；AWS、GCP、Azure 的资源路由已完成线上验证，
  日志权限不足时仍返回可读内容并标注为 `partial`；
- 已接入 Mem0：`/mem0 status`、`/mem0 smoke`、`/mem0 search 关键词`；
- Mem0 API Key、API health 和写入/搜索/清理 smoke 已验证；
- 本地新增适配器测试已通过；
- Graph 多步基线和 GoalStore 关闭竞态已修复，当前离线全套测试为 49 passed；
- Lark 已增加用户/群聊 allowlist 和一次性高风险请求确认；AWS 当前限制到已验证测试群；
- 危险工具已接入执行层二次审批：未带有效确认码不会执行，确认码一次性消费；
- 审批拒绝、放行和实际执行结果写入 SQLite `operation_audit`，Shell 审计不记录完整命令；
- 可选 `OPS_ALLOWED_TARGETS/SERVICES/OPERATIONS` 已接入工具执行层，越界操作在审批前拒绝；
- `vps_sysops` 继续作为独立项目维护，不并入 Agent 仓库。

## 4. 当前执行顺序

### 阶段一：稳定性和真实验收（当前最高优先级）

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

当前阶段状态：Graph 基线、LLM 客户端基础容错、AWS 重启恢复和三目标只读路由已完成；
运行时配置一致性、多 Provider 路由和配额级熔断仍待完成。

### 阶段二：权限和无 LLM 运维控制面

已完成：

- Lark 用户/群聊 allowlist；
- 高风险请求的一次性确认码和过期机制；
- 未授权消息不进入 Agent，也不发送回复。

仍需完成：

1. 为目标主机、服务和操作建立细粒度权限检查；
2. 将命令结果继续统一为适合手机查看的结构化卡片，并补充长日志分页；
3. 为目标选择后的服务操作增加服务级权限和变更操作路由，避免仅切换展示上下文。

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

AWS、GCP、Azure 的只读目标路由已完成；下一步扩展服务级操作和变更审批。Agent 不直接实现
各云厂商的主机运维细节，而是调用 vps_sysops profile 和适配器。

### 阶段四：Mem0 和任务记忆策略

1. 明确自动记忆、用户确认记忆和临时上下文的边界；
2. 增加记忆保存、搜索、删除和失败降级的用户体验；
3. 避免每条消息都触发 LLM 或 Mem0 写入；
4. 记忆服务不可用时，不阻塞普通运维任务。

### 阶段五：Lark 平台能力

按优先级逐步接入消息卡片、文档、多维表格、表格、日历、任务、邮件、
会议和知识库。每次只引入一个可验收的只读或低风险能力，再开放写操作。

涉及个人邮件、日历、私信和个人任务时，单独设计 User OAuth，不直接扩大
Bot 身份权限。

### 阶段六：架构和文档收敛

- 更新 `README.md`、`docs/current-state.md` 和用户手册；
- 清理仍描述 V1/Gemini/单 VPS 的历史说明；
- 评估未接入的重复运行时、队列和健康检查实现；
- 保持一套正式 Goal Runtime 和一套任务执行路径。

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

如果新需求与本基线冲突，先明确调整目标和优先级，再开始实现，避免在
稳定性、权限和多云基础尚未完成前无序扩展平台功能。
