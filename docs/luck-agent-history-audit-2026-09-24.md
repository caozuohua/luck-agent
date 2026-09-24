# luck-agent 历史运行审查（2026-09-24）

归档补记：后续同日收尾修正了集成测试的通知等待条件，unit + integration 为 83 passed。
下文保留初次评估时 82 passed / 1 failed 的历史结果；生产运行时未因此改动。
已停止新增 Lark API，见 [上线版本收尾记录](release-closeout-2026-09-24.md)。

结论：当前系统的状态完成率不能代表任务真实成功率。自然语言图执行通道的工具样本全部失败；快捷运维通道多数返回成功，但缺少统一的任务关联和独立验收。最急需补齐的是结果验收、错误分类、恢复事件和记忆证据，而不是增加模型重试次数。

## 1. 实际部署与审查边界

- 已实际登录 GCP `gcp-free-vps-oregon`（us-west1-a）。没有 `/opt/luck-agent`、luck-agent systemd unit 或该 unit 的 journal 条目。旧参考实例 `instance-20260413-080555` 已不存在。
- 根据仓库现状清单继续登录 AWS `aws-codex-vps`，确认 `/opt/luck-agent` 是实际生产部署。服务 active/running，本次启动时间为 2026-09-23 06:53:04 UTC。
- AWS HEAD 为 `73d94be28aed8e9bac0fd4f68e7d8f4a8f26d138`，分支 `feat/goal-runtime-langgraph`，有未提交修改和新增 capture 模块。未覆盖这些改动，未重启、升级或重新执行任何历史工具。
- 本地分析的 graph nodes、Supervisor、ToolExecutor、PatternStore、Curator、GraphRuntime 六个文件均与线上 SHA-256 一致。main.py 线上有改动，不能把本地启动接线完全视为生产事实。
- `/opt/luck-agent/data/agent.db`：38 个 Goal；`data/graph_state.db`：252 个 checkpoint、38 个线程，38/38 精确按 `user_id:goal_id` 关联。
- 根目录另一份 `graph_state.db` 有 546 个 checkpoint，观察到 `loop` / fake reply 等测试轨迹。为避免污染指标，未将它混入业务数据。
- Goal 时间范围：2026-08-20 02:02:22 至 2026-09-24 00:50:50 UTC。日志 `/var/log/luck-agent.log` 共 2333 行，覆盖 2026-08-20 01:32:14 至 2026-09-24 06:53:32 UTC。历史记录跨多个版本，不能把每次历史失败归因于当前代码或当前配置。
- 数据库使用 SQLite 只读连接并备份到内存，包含已提交 WAL。两库分别一致，不宣称跨库原子快照。输出不含用户 ID、原始参数、模型全文或凭据。

## 2. 指标与分母

| 指标 | 实测 | 含义与限制 |
|---|---:|---|
| Goal 状态 DONE | 25/38，65.8% | 流程状态，不是经验证成功率 |
| Goal 状态 FAILED | 13/38，34.2% | 无未终结 Goal |
| 无工具调用的 DONE | 24/25 | 多数为聊天、能力说明和记忆问答 |
| 工具失败后仍 DONE | 1 | 回答能力清单；不能仅因此断言最终文字回答失败，但工具并未成功 |
| 图执行工具调用 | 9 次，分属 8 个 Goal | shell 7 次，web_search 2 次 |
| 图工具返回成功 | 0/9 | 不涵盖快捷命令，不可表述为全系统工具成功率 0% |
| 工具显式错误 | 权限 5、缺配置 2、缺参数 2 | 错误码会掩盖其他问题 |
| 按当前工具 schema 复核参数 | 5/9 合法，4/9 缺必填 command | 其中 2 次缺参数被权限拒绝掩盖；语法合法不代表目标/权限/语义正确 |
| Supervisor retry 决策 | 8 次 | 再次规划，不等于实际重试了原工具 |
| 同工具同参数失败后重复调用 | 1 次 | 缺失 command 的相同调用再次失败 |
| 可观察的相同调用恢复 | 0 | 无法估计“成功恢复通常需几次重试” |
| Goal.tool_calls 非空 | 0/38 | 不能直接用该列统计调用 |
| Goal.retry_count 非零 | 0/38 | 不能用该列证明没有重试 |
| 快捷运维执行 | 13 次 | 12 次 restart + 1 次 backup；另外 13 条 approved 不能重复计入执行 |
| 快捷运维返回成功 | 12/13，92.3% | 11 restart + 1 backup；1 restart 返回 error；不是独立验收通过率 |
| patterns | 9 条，全部 error | shell 7、web_search 2 |
| context_summaries | 0 | 不能据此断言所有记忆为空，另有 Goal 历史、MEMORY.md、外部 Mem0 |

全系统真实任务成功率、工具选择总体准确率、记忆检索精确率和副作用验收率目前均不可可靠计算：缺少完整入口分母、版本化期望、独立结果证据和跨通道关联。

## 3. 工具选择、参数与决策样本

1. `23ab7a8b8c974772becc32aa06dd0d15`，“现在哪些服务可用”：两次 `shell({})`，均缺 command。工具种类可作为泛化检查手段，但调用没有可执行内容；第一次失败后没有修复参数就重复，应计为无进展重试。当前专用 service_health 更适合服务检查，但历史工具注册版本未保留，不能以当前工具清单倒推当时必须选择它。
2. `ffa57103b7cd4989b777835f299a1019`，“现在有什么工具、技能、mcp可用的”：选择 web_search，查询的是“2024年当前可用AI工具…”；与本机已注册能力清单不匹配，也有时间错位。应查工具注册表/配置/服务探测，外网结果不能证明本机能力。此例是明确的选择错误。
3. `dde60538b2e0412db05cfc64a324aec0`，“搜索这周广州的天气预报”：web_search 与任务相关，参数通过 schema；失败原因是缺 SERPER_API_KEY。应该归为依赖配置阻塞，不能归为工具选错或反复重试可恢复。
4. 两个“今天日期” Goal：`shell(date)` 参数合法，均 PERMISSION_DENIED。权限拒绝证明策略边界生效，不能推出宿主机完全无法运行 shell。需要记录具体拒绝规则和目标范围。
5. “工作目录”“当前系统上有定时任务吗”：均 `shell({})`，但返回权限拒绝。若只按错误字符串统计，会漏掉参数质量问题。
6. “网络搜索可用么”：没有任何工具调用，却回答当前可用；随后天气搜索证实配置缺失。属于未验证的能力声明。

生产代码可核对的问题：

- `core/tool_executor.py` 直接 `tool.run(**args)`，没有统一执行前 JSON Schema 校验；部分工具自行验证，部分依赖 Python 调用报错。
- `core/graph/nodes.py` 把 `step_count` 传为 `retry_count`：首次调用时已经为 1，后续步骤的首次失败也可能被当作预算用尽。需要按失败 episode / logical operation 计数，成功后重置。
- 同文件始终设置 `wrapped.blocking = False`，没有将权限、审批、配置、未知副作用转换为不可自动重试类别。
- `core/supervisor.py` 把 `ok=True` 直接判为 pass，confidence=0.9 是常量，未经校准；没有独立副作用验证器。
- CHAT/DONE 直接终止，缺少与原始任务验收条件的比对；表达“已记住”也可能只有文字。
- `runtime/graph_runtime.py` 通知异常只记 warning，缺乏持久化的送达确认、人工接手状态和通知重试。服务 active 不等于用户已获知失败。

## 4. 副作用核对

审计表中 13 次快捷执行均有 approved 记录，但没有 operation_id / approval_id，不能仅按相邻时间证明严格一一对应。

GCP 范围内可以识别到 4 次 new-api restart、1 次 A2A restart、1 次 new-api backup，均返回 status=ok。AWS 上有一次针对 new-api 的 restart 返回 error，随后 GCP 上同服务 restart 成功；目标不同且无恢复 episode 关联，不能算自动重试恢复。

独立检查了 GCP 归档 `/var/backups/vps-sysops/new-api/new-api_20260822_125332.tar.gz`：

- 配套 `sha256sum -c` 返回 OK。
- tar 目录可读，包含 manifest、new-api 配置和 SQLite 数据库。
- 从归档读出数据库到内存，SQLite `PRAGMA quick_check` 返回 `ok`，大小 4,763,648 字节。
- 归档时间与已记录 backup 操作相近，但缺 operation_id 绑定；可以证明该归档真实存在且内部结构完整，不能证明完整业务恢复，也不能严格证明它就是那条审计记录唯一生成的归档。
- 没有做恢复演练、没有打开/输出归档中的凭据。

当前 new-api 容器和 A2A 服务运行正常，但这是当前状态，不能补证 8 月每一次 restart 当时的业务效果。历史 restart 需要旧/新进程标识、目标实例、开始/结束、健康探测和回滚证据。

## 5. 错误发现、恢复和人工升级

9 次错误从 executor 检查点到 Supervisor 检查点：中位 1.223 ms，p95/max 1.656 ms。这只是“已返回错误被流程消费”的延迟，**不是物理故障发现时间**，不包括发现静默错误。

从错误检查点到图结束：中位 14.724 秒，p95/max 29.582 秒，分母为 9 次失败调用；同一任务可贡献多个样本。大部分延迟发生在重新规划阶段。没有用户送达时间/人工确认时间，不能报 MTTA 或人类发现时间。

8 个 retry 决策中，仅 1 个产生同参数再次调用，且没有恢复。其余多以文字回答/失败结束。不能把“下一轮返回了答案”算作原故障恢复。

建议先以影子策略评估，再接入运行时：

| 场景 | 初始策略 | 升级所需信息 |
|---|---|---|
| 权限拒绝、缺配置、审批缺失 | 立即停止自动重试并交给有权限的人处理 | 目标、拒绝规则/缺失项、最小修复动作；不带凭据 |
| 参数/工具不存在 | 本地验证并修复一次；相同调用指纹再次失败即升级 | 错误字段、已尝试修复、正确 schema 版本 |
| 429/临时 5xx/只读超时 | 尊重 Retry-After，指数退避；最多 2 次重试或累计 120 秒 | 尝试次数、剩余预算、依赖健康、预计影响 |
| 写操作超时或提交结果不明 | 先独立 read-back；不能确认则升级，禁止盲目重放 | 幂等键、预期/实际状态、可能已生效范围、补偿方案 |
| 目标不匹配、不可逆变更、验收失败 | 立即阻断并升级 | 请求目标与实际目标、变更差异、回滚边界 |
| 通知发送失败 | 单独持久化并有限重试，最终走可用告警通道 | 任务状态、发送错误、待人工接手标识 |

“2 次 / 120 秒”是待校准的起始预算，不是当前实测最佳阈值。不得为了耗尽预算而重试不可恢复错误。升级应保留等待人工状态，并记录通知送达、接手、修复与恢复，而不是让模型不断解释失败。

运行日志还显示：22 次 LLM HTTP 503、1 次 410、7 条 provider_unavailable；5 次 Wiki 搜索失败（授权、解析、404/响应处理）；2 次 Lark 卡片发送失败。不能把 HTTP 请求条数当独立任务数。119 次 WebSocket disconnected 与 116 次 runtime_started 大量重合，不能全部归为自发掉线。journal 明确记录 2026-09-17 停止等待 90 秒后 SIGKILL；其后的成功启动不抵消优雅退出缺陷。

## 6. 记忆审查

- 本地 patterns 只有 9 条错误记录，没有任何 tool success 样本。经验覆盖高度偏向错误。
- 当前 soul/MEMORY.md 把权限拒绝概括为环境严重限制 shell，将缺搜索 key 概括为无法获取外部信息。缺少目标、用户、来源时间、有效期与再次验证条件；容易把一次局部失败泛化为永久能力限制。
- `PatternStore.search_patterns` 的 SQL 没有 user_id/chat_id/project_id 过滤；全局 Curator 读取全部 patterns 并写入同一 MEMORY.md。存在跨用户/跨项目污染风险，本次没有证据证明已发生泄露。
- FTS 查询按空白切词；中文整句可能难以命中英文 `tool execution completed: shell` 等通用触发词。缺检索事件，不能量化实际命中率或证明这些记录被用过。
- 验收代号有一次写入式回答、四次正确回忆；它们没有 memory_write 轨迹，只能作为短期/Goal 历史回忆证据，不能证明外部持久记忆写入成功。
- 对 ddgs 先有对话、后答“没有历史”；存在历史连续性问题。但缺当时的上下文检索清单，无法区分重启、scope 不同、截断、检索失败或模型忽略。
- Supervisor 的同步 save_lesson 路径不适用于异步 PatternStore；另一路 ToolExecutor 仍会写错误 patterns，不能简单说完全没有学习。

## 7. 已补充的评估手段与下一步

本次新增：

1. `runtime/history_audit.py`：只读快照、精确关联 Goal/checkpoint、去重累计 scratchpad、错误分类、参数指纹、决策和延迟分布、完成状态与验证状态分离、缺证据返回 null。可通过 SSH stdin 执行，无需修改线上 checkout。
2. `runtime/evaluation.py`：显式人工 oracle 评估工具选择、版本化 JSON Schema、独立 read-back 的预期副作用。缺证据保留 unknown，schema 错误输出字段/规则而不输出参数值。
3. `recovery_recommendation`：可测试的影子升级策略，涵盖权限/配置、参数修复、瞬态失败、预算与未知写入效果。**尚未接入生产 Supervisor**。
4. `tests/unit/test_history_audit.py`：18 个测试，覆盖重复检查点、真正重复调用、无关成功不能冒充恢复、未知指标、写操作超时、预算耗尽、错误目标、只读 WAL 快照、不存在数据库和损坏轨迹排除。新增测试全通过。离线 unit + integration 为 82 passed / 1 failed；失败为已有 `test_graph_runtime_accepts_executes_and_notifies_goal` 在看到 DONE 后立即读取尚未填充的 notifications，单独运行同样复现（1 failed / 3 passed）。这是状态与通知的时序假设缺陷，不能单凭该测试证明生产消息永久丢失。本次未修改其实现或测试。

复现（在具有生产依赖的 Python 环境中）：

```bash
python -m runtime.history_audit --db /opt/luck-agent/data/agent.db --graph /opt/luck-agent/data/graph_state.db
```

本次脱敏输出保存在本地 `workspace/audit-2026-09-24/history.json`（git 忽略）。评估不调用线上工具，不生成新用户消息。

优先级 P0：统一 tool schema 验证；修正每个失败 episode 的重试计数；权限/配置/未知写入直接分流；任务验收与文字 DONE 分离；为每次写操作增加独立验证。P1：持久化送达/人工接手、完整工具关联、记忆 scope 与来源/失效机制、可靠 shutdown。

下一阶段统一事件至少应包含：request_id、goal_id、run_id、step_id、logical_operation_id、attempt_id、parent_attempt_id、tool/schema/deploy 版本、目标、脱敏参数摘要及指纹、expected_effect、observed_effect/evidence_ref、policy/verification verdict、error_class、retry_reason、预算、idempotency_key，以及发生/观察/检测/升级/通知/接手/恢复时间。存储允许列表元数据和脱敏证据，不默认保存原始思维链、密码或审批 token。

每条记忆应补 memory_id、scope、来源 Goal/证据、created/verified/expires 时间、读写/召回清单和采用情况；用跨重启回忆、正确/错误 scope、过期错误经验、矛盾纠正等固定用例评分。

评估集应覆盖只读正常路径、错误工具、缺参/类型错误、错目标、403、缺 key、429/5xx、超时前后副作用、重复提交、通知失败和跨重启记忆。用工具桩和虚拟时钟注入故障；按人工标注的期望验证，不对生产执行破坏性故障注入。本次已落地评估函数与边界用例，持续埋点、自动巡检和完整端到端故障演练仍待实施。
