# 极简智能体框架构建规范
# Minimal Agent Framework — Build Specification for Codex

---

## 任务说明

你的任务是从零构建一个面向个人部署的极简 AI 智能体框架（命名为 **luck-agent**）。

本文档是完整的设计规范，涵盖架构原则、模块职责、数据结构、行为约束和实现优先级。
严格按照本文档实现，不得自行引入未说明的依赖或模式。

---

## 技术栈

| 层次 | 选型 | 说明 |
|------|------|------|
| 语言 | Python 3.12 | asyncio 原生异步 |
| LLM | Vertex AI Gemini API | 服务账号 JWT 认证，无 ADC |
| 存储 | SQLite + FTS5 | 全文检索 + JSON 字段，无向量数据库 |
| 接口 | Lark WebSocket | Card 2.0，双向通信 |
| 依赖原则 | 无 MCP，无本地推理 | 保持最小依赖树 |

---

## 架构总览（9 个支柱）

```
┌─────────────────────────────────────────────────────────────────┐
│  P1  身份锚          SOUL.md，行为边界，逃生阀                  │
│  P2  提示词分层      System / Task / Tool 三层，职责不交叉      │
│  P3  意图分类        CHAT / ACTION / CLARIFY 三态路由           │
│  P4  工具路由        零 AI 规则路由 → 3-5 工具子集预选          │
│  P5  双模输出        执行态强制 JSON / 对话态自然语言            │
│  P6  调用校验        schema 校验 → 执行 → 结果摘要 → 重试      │
│  P7  状态机          Goal Runtime，目标持久化，可重启恢复        │
│  P8  上下文治理      Token 预算分配，50% 触发压缩               │
│  P9  自我进化        短期 pattern + 长期记忆压缩（Curator）     │
└─────────────────────────────────────────────────────────────────┘
```

---

## P1 — 身份锚（Identity Anchor）

### 文件：`soul/SOUL.md`

运行时**只读**，禁止代码修改。内容结构如下：

```markdown
## 身份
我是 luck-agent，一个个人部署的 AI 智能体。
我的主要职责是：[在此填写具体职责]

## 能力边界
我能够：
- 调用已注册的工具执行具体任务
- 检索历史经验辅助决策
- 管理多轮目标执行

我不能：
- 访问未授权的外部系统
- 伪造工具执行结果

## 行为禁区
- 绝不伪造工具调用结果
- 绝不在未调用工具的情况下声称已完成需要工具的任务
- 绝不在一轮对话中追问超过 1 次

## 逃生阀（Uncertainty Channels）
当无法完成任务时，输出结构：
{"intent": "CANNOT_COMPLETE", "reason": "具体原因", "suggestion": "替代方案"}

当意图不明确时，输出结构：
{"intent": "CLARIFY", "question": "一个具体问题", "best_guess": "如无回答我会按此理解执行"}

诚实表达不确定性，优于伪造结果。
```

### 文件：`soul/MEMORY.md`

由 Curator 自动维护，字符数硬上限 **3000**。
记录跨会话的核心经验、高频工具用法、已知错误模式。
格式：纯文本，Curator 负责压缩，人类可读。

---

## P2 — 提示词三层架构（Prompt Layering）

**原则：每层只做一件事，禁止跨层写规则。**

```
┌─────────────────────────────────────────────────┐
│  Layer 1: System Prompt（静态，每次调用固定）    │
│    - SOUL.md 全文                               │
│    - MEMORY.md 全文（若存在）                   │
│    - 全局输出格式规范                           │
│    - 全局行为规则（有疏有堵）                   │
├─────────────────────────────────────────────────┤
│  Layer 2: Task Prompt（动态，每轮构建）          │
│    - 当前用户意图（经分类后）                   │
│    - 当前可用工具子集文档（P4 路由结果）         │
│    - 会话历史摘要（P8 治理后）                  │
│    - 相关 experience pattern（FTS5 检索，≤3条） │
├─────────────────────────────────────────────────┤
│  Layer 3: Tool Docstrings（工具级，随子集注入）  │
│    - L1: 静态说明（功能、参数、返回值）          │
│    - L2: 动态任务感知提示（当前场景下的用法）    │
│    - L3: 经验提示（历史成功/失败用法摘要）       │
└─────────────────────────────────────────────────┘
```

### `core/prompt_builder.py` 职责

```python
class PromptBuilder:
    def build_system_prompt(self) -> str:
        """合并 SOUL.md + MEMORY.md + 全局规则"""

    def build_task_prompt(
        self,
        intent: IntentType,
        tool_subset: list[Tool],
        history_summary: str,
        experience_patterns: list[Pattern],
    ) -> str:
        """组装 Layer 2，注入 Layer 3 tool docstrings"""

    def get_tool_docstring(self, tool: Tool, task_context: str) -> str:
        """L1 + L2(动态) + L3(经验) 拼接"""
```

### 全局行为规则（写入 Layer 1）

**疏（鼓励）：**
- 优先调用工具获取实时信息，再回答
- 对复杂任务拆分步骤，逐步执行
- 不确定时使用逃生阀，不猜测

**堵（禁止）：**
- 禁止在未调用工具的情况下声称已执行操作
- 禁止连续追问超过 1 次
- 禁止将原始 JSON 数据直接返回用户

**防冲突检查（开发期）：**
新增提示词片段前，检查是否与其他层存在语义冲突（如"简洁"vs"详细说明每步"）。

---

## P3 — 意图分类（Intent Classification）

用户输入在进入 LLM 前，先经规则分类（零 LLM 调用）：

```python
class IntentType(Enum):
    CHAT    = "chat"     # 普通对话，不需要工具
    ACTION  = "action"   # 需要工具执行
    CLARIFY = "clarify"  # 意图不明，需追问（但 LLM 决定是否追问）
```

分类规则写在 `config/routing_rules.yaml`，基于关键词/正则匹配。
分类结果影响：工具路由（P4）、输出模式（P5）、校验策略（P6）。

---

## P4 — 工具路由（Zero-AI Intent Routing）

**核心原则：路由层不调用 LLM，纯规则匹配。**

```python
class ToolRouter:
    def route(self, user_input: str, intent: IntentType) -> list[Tool]:
        """
        基于 routing_rules.yaml 规则树，返回 3-5 个最相关工具。
        工具总池无上限，路由层屏蔽无关工具。
        路由失败 → fallback 到默认通用工具集（≤5个）。
        路由决策异步写入 SQLite（供 P9 分析）。
        """
```

### `config/routing_rules.yaml` 结构

```yaml
rules:
  - name: "日程相关"
    patterns: ["日程", "会议", "calendar", "schedule"]
    tools: ["calendar_query", "calendar_create", "reminder_set"]

  - name: "文件操作"
    patterns: ["文件", "文档", "file", "document", "上传"]
    tools: ["file_read", "file_write", "file_search"]

  # ... 更多规则

fallback_tools:
  - "general_search"
  - "ask_clarification"
  - "show_capabilities"
```

支持热更新：`router.reload_rules()` 无需重启进程。

---

## P5 — 双模输出（Dual Output Mode）

LLM 输出**必须**符合以下三种 schema 之一，由 `output_parser.py` 负责解析和校验。

### ACTION 模式（强制 JSON）

```json
{
  "intent": "ACTION",
  "plan": "一句话说明要做什么（用户可读）",
  "tool_call": {
    "name": "tool_name",
    "args": {
      "param1": "value1"
    }
  },
  "fallback": "工具失败时的降级方案描述"
}
```

### CHAT 模式（自然语言）

```json
{
  "intent": "CHAT",
  "message": "自然语言回复内容（中文或英文，跟随用户语言）"
}
```

### CLARIFY 模式（有限追问）

```json
{
  "intent": "CLARIFY",
  "question": "一个具体问题",
  "best_guess": "如果你不回答，我将按此理解执行"
}
```

### CANNOT_COMPLETE 模式（逃生阀）

```json
{
  "intent": "CANNOT_COMPLETE",
  "reason": "无法完成的具体原因",
  "suggestion": "替代方案或建议"
}
```

### `core/output_parser.py` 职责

```python
class OutputParser:
    def parse(self, raw_output: str) -> ParsedOutput:
        """
        1. 尝试 JSON 解析（容错：去除 markdown 代码块包裹）
        2. 校验 intent 字段存在
        3. 按 intent 类型做 schema 校验
        4. 校验失败 → 抛出 ParseError（由调用方决定重试）
        """

    def repair_and_retry(self, raw_output: str, error: ParseError) -> ParsedOutput:
        """
        将原始输出 + 错误信息拼接，重新请求 LLM 修正。
        最多重试 2 次，超出后降级为 CANNOT_COMPLETE。
        """
```

---

## P6 — 工具调用 + 结果校验

### 完整调用流程

```
用户输入
  → IntentClassifier（P3）
  → ToolRouter（P4）→ 工具子集
  → PromptBuilder（P2）→ 构建完整 prompt
  → LLM 调用（Vertex AI Gemini）
  → OutputParser（P5）→ schema 校验
      校验失败 → repair_and_retry（max 2次）→ 仍失败 → CANNOT_COMPLETE
  → ToolExecutor → 执行工具
      执行失败 → 写入 error pattern → 执行 fallback
  → ResultSummarizer → 摘要，用户语言输出
  → PatternWriter（P9）→ 写入成功/失败 pattern
  → GoalStore（P7）→ 更新 Goal 状态
```

### 工具统一返回格式

所有工具函数必须返回：

```python
@dataclass
class ToolResult:
    status: Literal["ok", "error"]
    data: Any              # 成功时的结果
    error: str | None      # 失败时的错误信息
    metadata: dict         # 执行时间、tool_name 等
```

### 结果摘要（解决"只说不做"的最后一环）

```python
class ResultSummarizer:
    async def summarize(
        self,
        tool_result: ToolResult,
        user_intent: str,
        user_language: str,  # "zh" | "en"
    ) -> str:
        """
        将 raw ToolResult 转化为用户可读的自然语言摘要。
        禁止将原始 JSON 直接透传给用户。
        摘要需包含：执行结果 + 对用户意图的回应。
        """
```

### 重试与超时策略

- schema 校验失败：附带错误信息重新请求 LLM，max 2 retry
- 工具执行异常：捕获 exception，写入 error pattern，执行 `fallback` 方案
- 工具执行超时：asyncio timeout 30s，超时后返回 TIMEOUT_ERROR ToolResult

---

## P7 — 状态机 + 目标运行时（Goal Runtime）

### 状态定义

```
IDLE
  → ROUTING       （意图分类 + 工具路由完成）
  → PLANNING      （LLM 生成执行计划）
  → EXECUTING     （工具调用中）
  → AWAITING_RESULT（等待异步工具结果）
  → EVALUATING    （校验结果，决定下一步）
  → DONE          （成功完成）
  → FAILED        （超出重试，降级处理）
```

### SQLite 表：goals

```sql
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,          -- uuid4
    user_id     TEXT NOT NULL,
    status      TEXT NOT NULL,             -- 状态枚举值
    intent_type TEXT,                      -- CHAT / ACTION / CLARIFY
    raw_input   TEXT,
    plan        TEXT,
    tool_calls  TEXT,                      -- JSON array of ToolCall
    result      TEXT,
    error       TEXT,
    retry_count INTEGER DEFAULT 0,
    created_at  INTEGER NOT NULL,          -- unix timestamp
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goals_user_status ON goals(user_id, status);
```

### `memory/goal_store.py` 职责

```python
class GoalStore:
    async def create(self, user_id: str, raw_input: str) -> Goal: ...
    async def update_status(self, goal_id: str, status: GoalStatus, **kwargs) -> None: ...
    async def get_in_progress(self, user_id: str) -> list[Goal]: ...  # 重启恢复用
    async def get_recent(self, user_id: str, limit: int = 10) -> list[Goal]: ...
```

Agent 启动时调用 `get_in_progress()`，恢复未完成的 Goal 继续执行。

---

## P8 — 上下文治理（Context Governance）

### Token 预算分配

```python
CONTEXT_BUDGET = {
    "total":        32_000,   # 可在 settings.py 配置
    "soul":          1_000,   # SOUL.md + MEMORY.md（固定）
    "task_prompt":   1_500,   # 意图 + 工具文档
    "history":       3_000,   # 历史摘要
    "experience":      500,   # FTS5 检索到的 pattern
    "current_turn":  None,    # 剩余空间全给当前输入
}
```

### 压缩策略

触发条件：当前上下文使用率 > **50%**

压缩方式（Head-Middle-Tail）：
- **Head**：保留 System Prompt（SOUL.md）完整
- **Tail**：保留最近 **3 轮**对话完整
- **Middle**：调用 LLM 将中间历史压缩为摘要（200 tokens 以内）

压缩结果存 SQLite，下次会话复用：

```sql
CREATE TABLE IF NOT EXISTS context_summaries (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    turn_range  TEXT,   -- JSON: {"from": 1, "to": 20}
    created_at  INTEGER NOT NULL
);
```

---

## P9 — 自我进化（Self-Evolution）

### 短期 Pattern 学习（实时写入，当次会话即可检索）

```sql
CREATE TABLE IF NOT EXISTS patterns (
    id           TEXT PRIMARY KEY,
    pattern_type TEXT NOT NULL,    -- 'success' | 'error'
    trigger      TEXT NOT NULL,    -- 触发场景自然语言描述
    tool_name    TEXT,
    args_schema  TEXT,             -- JSON: 成功时的参数结构
    outcome      TEXT,             -- 结果描述
    user_id      TEXT,
    created_at   INTEGER NOT NULL
);
-- FTS5 虚拟表，供 P2 Task Prompt 构建时检索
CREATE VIRTUAL TABLE IF NOT EXISTS patterns_fts USING fts5(
    trigger, outcome, tool_name,
    content=patterns, content_rowid=rowid
);
```

每次工具调用完成后（成功或失败），异步写入 pattern。
下次路由时，用用户输入做 FTS5 查询，取 top-3 pattern 注入 Layer 3 Experience。

### 长期记忆压缩（Curator）

触发条件：每隔 **50 次**完整 Goal 执行（可配置）

```python
class Curator:
    async def run(self) -> None:
        """
        1. 读取全量 patterns（按 pattern_type 分组）
        2. 调用 LLM：去重、合并同类模式、提取核心经验
        3. 将压缩结果写入 soul/MEMORY.md（字符数 ≤ 3000 硬限制）
        4. 清理 90 天前的 patterns 记录（保留统计汇总）
        """
```

---

## 目录结构

```
luck-agent/
├── soul/
│   ├── SOUL.md                  # 身份锚（运行时只读）
│   └── MEMORY.md                # 长期经验（Curator 维护）
│
├── core/
│   ├── agent.py                 # 主循环，状态机驱动
│   ├── intent_classifier.py     # P3 意图分类（规则）
│   ├── router.py                # P4 工具路由（规则树）
│   ├── prompt_builder.py        # P2 三层提示词构建
│   ├── output_parser.py         # P5 输出解析 + schema 校验
│   ├── tool_executor.py         # P6 工具调用 + 超时 + 重试
│   └── result_summarizer.py     # P6 结果摘要（用户可读）
│
├── memory/
│   ├── db.py                    # SQLite 连接管理（连接池）
│   ├── goal_store.py            # P7 Goal 持久化
│   ├── pattern_store.py         # P9 Pattern 存储 + FTS5 检索
│   ├── context_store.py         # P8 历史摘要存储
│   └── curator.py               # P9 长期记忆压缩
│
├── tools/
│   ├── registry.py              # 工具注册表（自动发现）
│   ├── base.py                  # Tool 基类 + ToolResult dataclass
│   └── [tool_name].py           # 各工具实现（实现 base.Tool）
│
├── llm/
│   ├── vertex_client.py         # Vertex AI Gemini 封装（JWT 认证）
│   └── token_counter.py         # P8 Token 预算计算
│
├── interface/
│   └── lark_ws.py               # Lark WebSocket 接口（Card 2.0）
│
├── config/
│   ├── routing_rules.yaml       # P4 路由规则（支持热更新）
│   └── settings.py              # 全局配置（预算、超时、阈值等）
│
├── tests/
│   ├── test_output_parser.py
│   ├── test_router.py
│   └── test_tool_executor.py
│
└── main.py                      # 入口，初始化 + 启动
```

---

## 实现优先级

### Phase 1 — 核心可运行（MVP）

目标：能接收用户输入，调用工具，返回摘要

- [ ] `soul/SOUL.md` 初始内容
- [ ] `llm/vertex_client.py` Gemini API 封装（JWT 认证）
- [ ] `core/prompt_builder.py` Layer 1 + 2 基础实现
- [ ] `core/output_parser.py` 三种 schema 解析 + 重试
- [ ] `tools/base.py` Tool 基类 + ToolResult
- [ ] `tools/registry.py` 工具自动注册
- [ ] `core/tool_executor.py` 基础调用 + 超时
- [ ] `core/result_summarizer.py` 基础摘要
- [ ] `core/agent.py` 主循环（同步版本）

### Phase 2 — 可靠执行

目标：工具路由准确，执行可重试，状态持久化

- [ ] `core/intent_classifier.py`
- [ ] `core/router.py` + `config/routing_rules.yaml`
- [ ] `memory/db.py` SQLite 初始化
- [ ] `memory/goal_store.py` + P7 状态机集成到 agent.py
- [ ] `core/tool_executor.py` 完整重试策略

### Phase 3 — 记忆与进化

目标：跨会话经验积累，上下文不爆窗口

- [ ] `memory/pattern_store.py` + FTS5
- [ ] P9 pattern 写入集成到工具调用流程
- [ ] P2 Layer 3 Experience 注入
- [ ] `memory/context_store.py` + P8 压缩逻辑
- [ ] `memory/curator.py` + 定时触发

### Phase 4 — 接口与运维

目标：生产可用，可监控

- [ ] `interface/lark_ws.py` Lark WebSocket
- [ ] 健康检查端点
- [ ] 结构化日志（JSON Lines）
- [ ] Curator 定时任务
- [ ] 热更新路由规则

---

## 关键约束（实现必须遵守）

1. **所有 LLM 输出必须经过 `output_parser`**，禁止直接使用 raw string response
2. **工具函数必须返回 `ToolResult`**，禁止抛出未捕获的 exception
3. **`SOUL.md` 运行时只读**，`MEMORY.md` 只能由 `Curator` 写入
4. **路由层零 LLM 调用**，纯规则匹配，路由决策延迟 < 10ms
5. **所有数据库操作异步执行**（`aiosqlite`），不阻塞主循环
6. **用户语言自动跟随**：检测用户输入语言，摘要层用同语言输出
7. **Pattern 写入异步非阻塞**：使用 `asyncio.create_task()`，不影响响应延迟

---

## 配置文件示例（`config/settings.py`）

```python
from dataclasses import dataclass

@dataclass
class AgentConfig:
    # LLM
    vertex_project: str = "your-gcp-project"
    vertex_location: str = "us-central1"
    vertex_model: str = "gemini-2.0-flash"
    service_account_key_path: str = "/path/to/sa-key.json"

    # Context
    context_budget_total: int = 32_000
    context_compress_threshold: float = 0.5  # 50%

    # Retry
    llm_parse_max_retry: int = 2
    tool_timeout_seconds: int = 30

    # Evolution
    curator_trigger_interval: int = 50   # 每50次Goal触发一次
    memory_md_max_chars: int = 3_000
    pattern_retention_days: int = 90

    # Router
    routing_rules_path: str = "config/routing_rules.yaml"
    fallback_tool_count: int = 5
```

---

*本规范版本：v1.0 | 生成日期：2026-06*
*RSS digest 模块暂不实现，预留 Phase 5 位置*
