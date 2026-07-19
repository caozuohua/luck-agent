from __future__ import annotations

# ReAct LLM output contract.
#
# The planner asks the model to return ONE JSON object per turn. This
# extends the existing core.output_parser intent set with an explicit
# `is_goal_complete` flag so the loop knows when to stop. The
# OutputParser in core.output_parser still does the actual parsing; this
# string is only injected into the system prompt as guidance for models
# that need it (small models especially).

REACT_SYSTEM_HINT = (
    "You operate inside a ReAct loop (Thought -> Action -> Observation). "
    "Each turn you MUST return exactly ONE JSON object with these fields:\n"
    "  intent: one of ACTION | CHAT | DONE | CLARIFY | CANNOT_COMPLETE\n"
    "  plan: short string describing your current step\n"
    "  tool_call: { \"name\": <tool name>, \"args\": { ... } }  (required when intent=ACTION)\n"
    "  is_goal_complete: true ONLY when the user's original goal is fully achieved\n"
    "  message: user-facing text (required for CHAT/DONE/CLARIFY/CANNOT_COMPLETE)\n"
    "After a tool returns an Observation, reason about it and decide the next Action. "
    "Do not claim success without verifying via a tool."
)

# The four intents the loop understands.
INTENT_ACTION = "ACTION"
INTENT_CHAT = "CHAT"
INTENT_DONE = "DONE"
INTENT_CLARIFY = "CLARIFY"
INTENT_CANNOT_COMPLETE = "CANNOT_COMPLETE"

# Supervisor decisions that end or continue the loop.
DECISION_DONE = "done"
DECISION_PASS = "pass"
DECISION_RETRY = "retry"
DECISION_BLOCK = "block"
DECISION_FAIL = "fail"
