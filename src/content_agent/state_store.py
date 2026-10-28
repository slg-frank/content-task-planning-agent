"""Execution state transitions and JSON persistence."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import json
from json import JSONDecodeError
from pathlib import Path
import re
from typing import Any

from content_agent.models import (
    ExecutionState,
    ExecutionStatus,
    FinalStatus,
    PlanStep,
    ToolResult,
)
from content_agent.tools.common import DEFAULT_RUNTIME_ROOT


SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

ALLOWED_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.PENDING: frozenset(
        {ExecutionStatus.RUNNING, ExecutionStatus.SKIPPED, ExecutionStatus.BLOCKED}
    ),
    ExecutionStatus.RUNNING: frozenset(
        {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED}
    ),
    ExecutionStatus.FAILED: frozenset({ExecutionStatus.RUNNING}),
    ExecutionStatus.BLOCKED: frozenset({ExecutionStatus.PENDING}),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.SKIPPED: frozenset(),
}


class InvalidStateTransition(ValueError):
    """Raised when a step attempts an unsupported lifecycle transition."""


class StatePersistenceError(ValueError):
    """Raised when persisted state is invalid or cannot be decoded."""


def _find_step(state: ExecutionState, step_id: str) -> PlanStep:
    matches = [step for step in state.steps if step.step_id == step_id]
    if not matches:
        raise KeyError(f"unknown step_id: {step_id}")
    if len(matches) > 1:
        raise ValueError(f"duplicate step_id: {step_id}")
    return matches[0]


def _validate_tool_result(result: ToolResult, target: ExecutionStatus) -> None:
    if target is ExecutionStatus.SUCCEEDED and not result.success:
        raise InvalidStateTransition("a failed tool result cannot mark a step succeeded")
    if target is ExecutionStatus.FAILED and result.success:
        raise InvalidStateTransition("a successful tool result cannot mark a step failed")
    if not result.incurred_cost.is_finite() or result.incurred_cost < 0:
        raise ValueError("tool result cost must be finite and non-negative")
    if result.duration_seconds < 0:
        raise ValueError("tool result duration must be non-negative")


def transition_step(
    state: ExecutionState,
    step_id: str,
    new_status: ExecutionStatus,
    *,
    result: ToolResult | None = None,
) -> PlanStep:
    """Apply one validated transition and update execution totals once."""
    step = _find_step(state, step_id)
    allowed = ALLOWED_TRANSITIONS[step.status]
    if new_status not in allowed:
        raise InvalidStateTransition(
            f"invalid transition for {step_id}: {step.status.value} -> {new_status.value}"
        )

    terminal_tool_status = new_status in {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
    }
    if terminal_tool_status and result is None:
        raise InvalidStateTransition(
            f"transition to {new_status.value} requires a tool result"
        )
    if not terminal_tool_status and result is not None:
        raise InvalidStateTransition(
            f"transition to {new_status.value} cannot attach a tool result"
        )

    if result is not None:
        _validate_tool_result(result, new_status)

    if new_status is ExecutionStatus.RUNNING:
        step.result = None
    elif result is not None:
        step.result = result
        state.incurred_cost += result.incurred_cost
        state.elapsed_duration_seconds += result.duration_seconds
        state.tool_call_count += 1

    step.status = new_status
    return step


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StatePersistenceError(f"invalid decimal in {field_name}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise StatePersistenceError(f"{field_name} must be finite and non-negative")
    return parsed


def _tool_result_from_dict(raw: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        tool_name=str(raw["tool_name"]),
        success=bool(raw["success"]),
        summary=str(raw["summary"]),
        data=dict(raw.get("data", {})),
        incurred_cost=_decimal(raw.get("incurred_cost", "0"), "tool incurred_cost"),
        duration_seconds=float(raw.get("duration_seconds", 0.0)),
        error_code=(str(raw["error_code"]) if raw.get("error_code") is not None else None),
        retryable=bool(raw.get("retryable", False)),
        partial_success=bool(raw.get("partial_success", False)),
        warnings=[str(item) for item in raw.get("warnings", [])],
    )


def _step_from_dict(raw: Mapping[str, Any]) -> PlanStep:
    result_raw = raw.get("result")
    result = _tool_result_from_dict(result_raw) if isinstance(result_raw, Mapping) else None
    return PlanStep(
        step_id=str(raw["step_id"]),
        name=str(raw["name"]),
        status=ExecutionStatus(str(raw["status"])),
        tool_name=(str(raw["tool_name"]) if raw.get("tool_name") is not None else None),
        result=result,
    )


def execution_state_from_dict(raw: Mapping[str, Any]) -> ExecutionState:
    """Reconstruct and minimally validate a persisted execution state."""
    if int(raw.get("schema_version", 0)) != 1:
        raise StatePersistenceError("unsupported execution state schema version")

    task_id = str(raw["task_id"])
    if not SAFE_TASK_ID.fullmatch(task_id):
        raise StatePersistenceError("persisted task_id is invalid")

    steps_raw = raw.get("steps", [])
    if not isinstance(steps_raw, list):
        raise StatePersistenceError("steps must be a list")
    steps = [_step_from_dict(item) for item in steps_raw if isinstance(item, Mapping)]
    if len(steps) != len(steps_raw):
        raise StatePersistenceError("every step must be an object")
    step_ids = [step.step_id for step in steps]
    if len(step_ids) != len(set(step_ids)):
        raise StatePersistenceError("step IDs must be unique")

    elapsed = float(raw.get("elapsed_duration_seconds", 0.0))
    estimated_duration = float(raw.get("estimated_total_duration_seconds", 0.0))
    deadline_gap = float(raw.get("deadline_gap_seconds", 0.0))
    call_count = int(raw.get("tool_call_count", 0))
    decision_count = int(raw.get("decision_step_count", 0))
    if (
        elapsed < 0
        or estimated_duration < 0
        or deadline_gap < 0
        or call_count < 0
        or decision_count < 0
    ):
        raise StatePersistenceError("duration and execution counts must be non-negative")

    return ExecutionState(
        task_id=task_id,
        task_fingerprint=(
            str(raw["task_fingerprint"])
            if raw.get("task_fingerprint") is not None
            else None
        ),
        status=FinalStatus(str(raw.get("status", FinalStatus.INCOMPLETE.value))),
        steps=steps,
        incurred_cost=_decimal(raw.get("incurred_cost", "0"), "incurred_cost"),
        estimated_total_cost=_decimal(
            raw.get("estimated_total_cost", "0"),
            "estimated_total_cost",
        ),
        estimated_total_duration_seconds=estimated_duration,
        estimated_plan_profile=(
            str(raw["estimated_plan_profile"])
            if raw.get("estimated_plan_profile") is not None
            else None
        ),
        budget_gap=_decimal(raw.get("budget_gap", "0"), "budget_gap"),
        deadline_gap_seconds=deadline_gap,
        elapsed_duration_seconds=elapsed,
        tool_call_count=call_count,
        decision_step_count=decision_count,
        selected_plan_profile=str(raw.get("selected_plan_profile", "standard")),
        incomplete_reason=(
            str(raw["incomplete_reason"])
            if raw.get("incomplete_reason") is not None
            else None
        ),
        schema_version=1,
    )


class StateStore:
    """Persist execution state as one JSON file per task."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or (DEFAULT_RUNTIME_ROOT / "state")).resolve()

    def path_for(self, task_id: str) -> Path:
        if not SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError("task_id is not safe for state persistence")
        return self.root / f"{task_id}.json"

    def save(self, state: ExecutionState) -> Path:
        path = self.path_for(state.task_id)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary_path = self.root / f".{state.task_id}.tmp"
        payload = json.dumps(
            state.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary_path.write_text(f"{payload}\n", encoding="utf-8")
        temporary_path.replace(path)
        return path

    def load(self, task_id: str) -> ExecutionState:
        path = self.path_for(task_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise StatePersistenceError("state file must contain an object")
            return execution_state_from_dict(raw)
        except (OSError, JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, StatePersistenceError):
                raise
            raise StatePersistenceError(f"unable to load state for {task_id}: {exc}") from exc

    def exists(self, task_id: str) -> bool:
        return self.path_for(task_id).is_file()
