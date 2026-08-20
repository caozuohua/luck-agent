# Luck Agent 当前实现与路线

更新时间：2026-08-20

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
- SQLite 保存 Goal、上下文、模式和运行时状态。
- `interface/` 当前包含 Lark 消息处理核心和本地 Web 测试接口；AWS 生产环境已完成真实 Lark WebSocket 收发和卡片发送验证。
- `core/lark_ws_runner.py` 已有生命周期封装，但尚未在 `main.py` 中完成生产接线。
- `tools/vps_sysops.py` 通过固定 allowlist 调用独立的 vps_sysops 项目，不接受用户任意 Shell。
- `tools/mem0_client.py` 提供 Mem0 health、smoke 和 search；Mem0 业务记忆仍由 Agent 负责，vps_sysops 只负责服务运维。
- Lark 入口已支持用户/群聊 allowlist 和一次性高风险请求确认；AWS 当前配置为已验证测试群。

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

## 当前阻塞项

1. 确认机制目前是消息级前置拦截，工具执行级审批和审计日志仍待完善。
2. AWS 适配器已可用，但 Provider → Account → Region → Target → Service 模型尚未统一。
3. LLM Provider Router、配额熔断和多 Provider 降级尚未完成。
4. Dockerfile、systemd 用户和部署脚本仍存在配置一致性待核对项。
5. 旧 README、SPEC、CLAUDE、AGENTS 和 DOCX 手册仍包含部分 V1/Gemini/单 VPS 描述。

## 实施顺序

详细基线见 [`docs/roadmap.md`](roadmap.md)。当前顺序为：

1. 工具执行级审批、审计和 LLM 容错；
2. AWS 重启恢复、运行时配置一致性和多云目标模型；
3. 无 LLM VPS 运维控制面扩展；
4. Mem0 记忆策略与 LLM Provider Router；
5. 再逐步开放 Lark 平台能力和高风险写操作。
