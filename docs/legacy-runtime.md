# Legacy runtime boundary

更新时间：2026-08-22

本文明确兼容代码的边界。当前生产主链只有一套：

```text
Lark WebSocket 普通消息 → Goal Runtime → LangGraph → ToolExecutor
Lark 快捷命令/卡片回调 → 无 LLM 控制面 → 固定 adapter
```

以下路径不是生产主链，只为本地调试、历史数据库迁移和回归测试保留：

- `interface/web.py`：本地 Web 直连 Agent 的调试入口；生产配置使用 Lark WebSocket。
- `EXECUTION_MODE=legacy` 与 `MinimalAgent._run_turn_legacy()`：旧的同步 ReAct 兼容入口。
- `skills/legacy_react.py`、`legacy_inline` 分支：未命中正式 Goal Skill 时的兼容路由。
- `core/goal.py`、`core/execution_engine.py`：早期 Goal/Skill 生命周期实现；当前生产调度由
  `memory/GoalStore`、`runtime/GraphRuntime` 和 `core/graph/` 负责。

兼容边界规则：

1. 生产 `.env` 必须使用 `EXECUTION_MODE=graph`；不得新增依赖 legacy 入口的业务能力。
2. 新功能只能接入 `runtime/`、`core/graph/`、正式 Skill 或无 LLM 快捷命令控制面。
3. legacy 文件只接受安全修复、迁移兼容和测试所需变更；不再扩展产品功能。
4. `SPEC.md`、`CLAUDE.md`、`AGENTS.md` 和 `docs/superpowers/` 中的旧设计只作为历史材料，
   以本文件、`docs/roadmap.md` 和 `docs/current-state.md` 为当前事实来源。

后续删除条件：兼容测试和旧数据库迁移观察期结束、Web 调试入口有明确替代方案、并完成一次
独立回滚演练后，再单独提交删除 legacy 代码；本轮不做不可逆删除。
