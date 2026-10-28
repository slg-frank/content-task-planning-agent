"""Provider-neutral model interface and strict action validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


ALLOWED_TOOLS = frozenset(
    {
        "material_search",
        "script_generation",
        "cost_estimation",
        "plan_save",
    }
)


class ActionKind(str, Enum):
    """Actions a model may request from the stage-06 orchestrator."""

    CALL_TOOL = "call_tool"
    SELECT_ALTERNATIVE = "select_alternative"
    STOP = "stop"
    COMPLETE = "complete"


class ModelError(RuntimeError):
    """Base class for model adapter failures."""


class ModelConfigurationError(ModelError):
    """Raised when a real provider is requested without valid configuration."""


class ModelOutputError(ModelError):
    """Raised when model output is not a safe, supported action."""


class ModelProviderError(ModelError):
    """Raised when a configured provider call fails."""


@dataclass(frozen=True, slots=True)
class ModelContext:
    """Provider-neutral information supplied for the next decision."""

    task: Mapping[str, Any]
    execution_state: Mapping[str, Any]
    last_observation: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": dict(self.task),
            "execution_state": dict(self.execution_state),
            "last_observation": (
                dict(self.last_observation) if self.last_observation is not None else None
            ),
            "allowed_tools": sorted(ALLOWED_TOOLS),
        }


@dataclass(frozen=True, slots=True)
class ModelAction:
    """A validated action that is safe to hand to an orchestrator."""

    kind: ActionKind
    tool_name: str | None
    parameters: dict[str, Any] = field(default_factory=dict)
    decision_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.kind.value,
            "tool_name": self.tool_name,
            "parameters": dict(self.parameters),
            "decision_reason": self.decision_reason,
        }


class ModelAdapter(Protocol):
    """Interface implemented by offline and real model providers."""

    def choose_action(self, context: ModelContext) -> ModelAction:
        """Return one validated next action."""
        ...


@dataclass(frozen=True, slots=True)
class ToolParameterRule:
    required: frozenset[str]
    optional: frozenset[str] = frozenset()


TOOL_PARAMETER_RULES: dict[str, ToolParameterRule] = {
    "material_search": ToolParameterRule(
        required=frozenset({"topic"}),
        optional=frozenset({"limit", "minimum_results"}),
    ),
    "script_generation": ToolParameterRule(
        required=frozenset({"topic", "materials"}),
        optional=frozenset({"variant"}),
    ),
    "cost_estimation": ToolParameterRule(required=frozenset({"plan_profile"})),
    "plan_save": ToolParameterRule(required=frozenset({"task_id", "plan"})),
}


def _require_non_empty_string(parameters: Mapping[str, Any], field_name: str) -> None:
    value = parameters.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ModelOutputError(f"parameter '{field_name}' must be a non-empty string")


def _validate_tool_parameters(tool_name: str, parameters: Mapping[str, Any]) -> None:
    rule = TOOL_PARAMETER_RULES[tool_name]
    keys = set(parameters)
    missing = sorted(rule.required - keys)
    if missing:
        raise ModelOutputError(
            f"tool '{tool_name}' is missing required parameter(s): {', '.join(missing)}"
        )
    unknown = sorted(keys - rule.required - rule.optional)
    if unknown:
        raise ModelOutputError(
            f"tool '{tool_name}' received unknown parameter(s): {', '.join(unknown)}"
        )

    if tool_name == "material_search":
        _require_non_empty_string(parameters, "topic")
        for field_name in ("limit", "minimum_results"):
            value = parameters.get(field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ModelOutputError(f"parameter '{field_name}' must be a positive integer")
    elif tool_name == "script_generation":
        _require_non_empty_string(parameters, "topic")
        if not isinstance(parameters.get("materials"), (list, tuple)):
            raise ModelOutputError("parameter 'materials' must be an array")
        variant = parameters.get("variant")
        if variant is not None and variant not in {"standard", "economy"}:
            raise ModelOutputError("parameter 'variant' must be 'standard' or 'economy'")
    elif tool_name == "cost_estimation":
        _require_non_empty_string(parameters, "plan_profile")
    elif tool_name == "plan_save":
        _require_non_empty_string(parameters, "task_id")
        if not isinstance(parameters.get("plan"), Mapping):
            raise ModelOutputError("parameter 'plan' must be an object")


def parse_model_action(raw: Mapping[str, Any] | Any) -> ModelAction:
    """Parse untrusted model output into a validated action.

    This function never executes a tool. Unknown fields, actions, tools, or
    parameters are rejected before an orchestrator receives the action.
    """
    if not isinstance(raw, Mapping):
        raise ModelOutputError("model output must be an object")

    allowed_top_level = {"action", "tool_name", "parameters", "decision_reason"}
    unknown_top_level = sorted(set(raw) - allowed_top_level)
    if unknown_top_level:
        raise ModelOutputError(
            f"model output contains unknown field(s): {', '.join(unknown_top_level)}"
        )

    try:
        kind = ActionKind(str(raw["action"]))
    except KeyError as exc:
        raise ModelOutputError("model output is missing 'action'") from exc
    except ValueError as exc:
        raise ModelOutputError(f"unknown action: {raw.get('action')}") from exc

    decision_reason = raw.get("decision_reason")
    if not isinstance(decision_reason, str) or not decision_reason.strip():
        raise ModelOutputError("decision_reason must be a non-empty string")
    decision_reason = decision_reason.strip()
    if len(decision_reason) > 500:
        raise ModelOutputError("decision_reason must not exceed 500 characters")

    parameters_raw = raw.get("parameters", {})
    if not isinstance(parameters_raw, Mapping):
        raise ModelOutputError("parameters must be an object")
    parameters = dict(parameters_raw)
    tool_name_raw = raw.get("tool_name")

    if kind is ActionKind.CALL_TOOL:
        if not isinstance(tool_name_raw, str) or tool_name_raw not in ALLOWED_TOOLS:
            raise ModelOutputError(f"unknown or missing tool: {tool_name_raw}")
        _validate_tool_parameters(tool_name_raw, parameters)
        tool_name: str | None = tool_name_raw
    elif kind is ActionKind.SELECT_ALTERNATIVE:
        if tool_name_raw is not None:
            raise ModelOutputError("select_alternative must not specify a tool")
        if set(parameters) != {"plan_profile"}:
            raise ModelOutputError(
                "select_alternative requires only the 'plan_profile' parameter"
            )
        _require_non_empty_string(parameters, "plan_profile")
        tool_name = None
    else:
        if tool_name_raw is not None:
            raise ModelOutputError(f"{kind.value} must not specify a tool")
        if parameters:
            raise ModelOutputError(f"{kind.value} must not include parameters")
        tool_name = None

    return ModelAction(
        kind=kind,
        tool_name=tool_name,
        parameters=parameters,
        decision_reason=decision_reason,
    )
