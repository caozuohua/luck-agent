"""Offline, explicit-oracle evaluation; missing evidence stays unknown.

Fixtures must be reviewed for the task and deployed tool schema version.
No tools or models are called by this module.
"""
from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator


def evaluate_call(
    *, selected_tool: str, arguments: Any,
    allowed_tools: list[str] | None = None,
    schema: dict | None = None,
    expected_effect: dict | None = None,
    observed_effect: dict | None = None,
    evidence_ref: str = "",
) -> dict:
    """Evaluate selection, syntax and independently observed postconditions.

    `observed_effect` must come from an independent read-back, never the
    mutation tool's own success flag. This provenance is a reviewer obligation.
    Target and scope constraints belong in the supplied versioned schema.
    """
    selection = None if allowed_tools is None else selected_tool in allowed_tools
    errors = []
    valid = None
    if schema is not None:
        Draft202012Validator.check_schema(schema)
        errors = [
            {"path": list(error.absolute_path), "rule": error.validator}
            for error in Draft202012Validator(schema).iter_errors(arguments)
        ]
        valid = not errors
    effect = None
    if expected_effect is not None and observed_effect is not None and evidence_ref:
        effect = all(key in observed_effect and observed_effect[key] == value for key, value in expected_effect.items())
    return {"tool_selection_correct": selection, "arguments_valid": valid,
            "argument_errors": errors, "side_effect_correct": effect,
            "evidence_present": bool(evidence_ref)}
