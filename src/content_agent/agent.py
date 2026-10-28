"""Core model-action, tool-observation execution loop."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Protocol

from content_agent.model import (
    ActionKind,
    ModelAction,
    ModelAdapter,
    ModelContext,
    ModelError,
)
from content_agent.models import (
    ExecutionState,
    ExecutionStatus,
    FinalStatus,
    PlanStep,
    TaskInput,
    ToolResult,
)
from content_agent.state_store import InvalidStateTransition, StateStore, transition_step
from content_agent.tools import (
    CostEstimationRequest,
    FailureInjector,
    MaterialSearchRequest,
    PlanSaveRequest,
    ScriptGenerationRequest,
    estimate_cost,
    generate_script,
    save_plan,
    search_materials,
)
from content_agent.tools.common import DEFAULT_RUNTIME_ROOT
from content_agent.trace import TraceRecorder


STEP_NAMES = {
    "material_search": "素材检索",
    "script_generation": "脚本生成",
    "cost_estimation": "成本估算",
    "plan_save": "方案保存",
}


class ToolExecutor(Protocol):
    """Boundary used by the loop to execute a validated tool action."""

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        ...


class LocalToolExecutor:
    """Dispatch validated actions to the four local business tools."""

    def __init__(
        self,
        *,
        runtime_root: Path | None = None,
        data_dir: Path | None = None,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self.runtime_root = (runtime_root or DEFAULT_RUNTIME_ROOT).resolve()
        self.data_dir = data_dir
        self.failure_injector = failure_injector

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        if tool_name == "material_search":
            return search_materials(
                MaterialSearchRequest(
                    topic=str(parameters["topic"]),
                    limit=int(parameters.get("limit", 3)),
                    minimum_results=int(parameters.get("minimum_results", 1)),
                ),
                data_dir=self.data_dir,
                failure_injector=self.failure_injector,
            )
        if tool_name == "script_generation":
            return generate_script(
                ScriptGenerationRequest(
                    topic=str(parameters["topic"]),
                    materials=tuple(parameters["materials"]),
                    variant=str(parameters.get("variant", "standard")),
                ),
                data_dir=self.data_dir,
                failure_injector=self.failure_injector,
            )
        if tool_name == "cost_estimation":
            return estimate_cost(
                CostEstimationRequest(plan_profile=str(parameters["plan_profile"])),
                data_dir=self.data_dir,
                failure_injector=self.failure_injector,
            )
        if tool_name == "plan_save":
            return save_plan(
                PlanSaveRequest(
                    task_id=str(parameters["task_id"]),
                    plan=parameters["plan"],
                ),
                output_dir=self.runtime_root / "plans",
                allowed_output_root=self.runtime_root,
                data_dir=self.data_dir,
                failure_injector=self.failure_injector,
            )
        raise ValueError(f"unsupported tool: {tool_name}")


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_steps: int = 10
    max_tool_calls: int = 8

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be at least 1")


@dataclass(slots=True)
class AgentRunResult:
    task_id: str
    status: FinalStatus
    state: ExecutionState
    trace_path: Path
    saved_plan_path: Path | None = None
    incomplete_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "state": self.state.to_dict(),
            "trace_path": str(self.trace_path),
            "saved_plan_path": (
                str(self.saved_plan_path) if self.saved_plan_path is not None else None
            ),
            "incomplete_reason": self.incomplete_reason,
        }


class AgentRunner:
    """Run the validated model -> tool -> observation loop."""

    def __init__(
        self,
        *,
        model: ModelAdapter,
        tool_executor: ToolExecutor,
        state_store: StateStore,
        trace_recorder: TraceRecorder,
        limits: AgentLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.model = model
        self.tool_executor = tool_executor
        self.state_store = state_store
        self.trace = trace_recorder
        self.limits = limits or AgentLimits()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _initial_state(task_id: str, task_fingerprint: str) -> ExecutionState:
        return ExecutionState(
            task_id=task_id,
            task_fingerprint=task_fingerprint,
            steps=[
                PlanStep(step_id=tool_name, name=name, tool_name=tool_name)
                for tool_name, name in STEP_NAMES.items()
            ],
        )

    @staticmethod
    def _task_fingerprint(task: TaskInput) -> str:
        """Hash stable business inputs while excluding execution-only settings."""
        payload = {
            "topic": task.topic,
            "budget": format(task.budget.normalize(), "f"),
            "deadline_utc": task.deadline.astimezone(timezone.utc).isoformat(),
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _existing_result(
        self,
        state: ExecutionState,
        *,
        status: FinalStatus | None = None,
        reason: str | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            task_id=state.task_id,
            status=status or state.status,
            state=state,
            trace_path=self.trace.path,
            saved_plan_path=self._saved_plan_path(state),
            incomplete_reason=(reason if reason is not None else state.incomplete_reason),
        )

    @staticmethod
    def _step_for_action(
        state: ExecutionState,
        tool_name: str,
        parameters: Mapping[str, Any],
    ) -> PlanStep:
        matching_steps = [step for step in state.steps if step.tool_name == tool_name]
        for step in matching_steps:
            if step.status in {ExecutionStatus.PENDING, ExecutionStatus.FAILED}:
                return step
        if tool_name == "cost_estimation" and matching_steps:
            profile = str(parameters.get("plan_profile", "alternative"))
            step = PlanStep(
                step_id=f"cost_estimation:{len(matching_steps) + 1}",
                name=f"成本估算（{profile}）",
                tool_name=tool_name,
            )
            state.steps.append(step)
            return step
        if matching_steps:
            return matching_steps[0]
        raise KeyError(f"no plan step exists for tool: {tool_name}")

    @staticmethod
    def _saved_plan_path(state: ExecutionState) -> Path | None:
        for step in state.steps:
            if step.tool_name == "plan_save" and step.result and step.result.success:
                path = step.result.data.get("saved_path")
                if isinstance(path, str):
                    return Path(path)
        return None

    @staticmethod
    def _completion_requirements_met(state: ExecutionState) -> bool:
        required_tools = set(STEP_NAMES)
        succeeded_tools = {
            step.tool_name
            for step in state.steps
            if step.status is ExecutionStatus.SUCCEEDED
        }
        return required_tools <= succeeded_tools

    @staticmethod
    def _update_constraint_gaps(
        task: TaskInput,
        state: ExecutionState,
        started_at: datetime,
    ) -> dict[str, Any]:
        available_duration = max(
            0.0,
            (task.deadline - started_at.astimezone(task.deadline.tzinfo)).total_seconds(),
        )
        estimation_current = (
            state.estimated_plan_profile is not None
            and state.estimated_plan_profile == state.selected_plan_profile
        )
        if estimation_current:
            state.budget_gap = max(
                Decimal("0"),
                state.estimated_total_cost - task.budget,
            )
            projected_duration = (
                state.elapsed_duration_seconds
                + state.estimated_total_duration_seconds
            )
            state.deadline_gap_seconds = max(
                0.0,
                projected_duration - available_duration,
            )
        else:
            state.budget_gap = Decimal("0")
            state.deadline_gap_seconds = 0.0
            projected_duration = state.elapsed_duration_seconds

        return {
            "budget": str(task.budget),
            "estimated_total_cost": str(state.estimated_total_cost),
            "budget_gap": str(state.budget_gap),
            "available_duration_seconds": available_duration,
            "elapsed_duration_seconds": state.elapsed_duration_seconds,
            "estimated_total_duration_seconds": (
                state.estimated_total_duration_seconds
            ),
            "projected_duration_seconds": projected_duration,
            "deadline_gap_seconds": state.deadline_gap_seconds,
            "selected_plan_profile": state.selected_plan_profile,
            "estimated_plan_profile": state.estimated_plan_profile,
            "estimation_current": estimation_current,
            "requires_reestimation": not estimation_current,
        }

    @classmethod
    def _context(
        cls,
        task: TaskInput,
        state: ExecutionState,
        last_observation: Mapping[str, Any] | None,
        started_at: datetime,
    ) -> ModelContext:
        state_payload = state.to_dict()
        state_payload["constraints"] = cls._update_constraint_gaps(
            task,
            state,
            started_at,
        )
        return ModelContext(
            task=task.to_dict(),
            execution_state=state_payload,
            last_observation=last_observation,
        )

    @staticmethod
    def _prerequisites_succeeded(state: ExecutionState) -> bool:
        required = {"material_search", "script_generation", "cost_estimation"}
        succeeded = {
            step.tool_name
            for step in state.steps
            if step.status is ExecutionStatus.SUCCEEDED
        }
        return required <= succeeded

    @classmethod
    def _plan_save_blockers(
        cls,
        task: TaskInput,
        state: ExecutionState,
        parameters: Mapping[str, Any],
        started_at: datetime,
    ) -> tuple[list[str], dict[str, Any]]:
        constraints = cls._update_constraint_gaps(task, state, started_at)
        blockers: list[str] = []
        if not cls._prerequisites_succeeded(state):
            blockers.append("prerequisites_not_succeeded")
        if not constraints["estimation_current"]:
            blockers.append("reestimation_required")
        if state.budget_gap > 0:
            blockers.append("budget_exceeded")
        if state.deadline_gap_seconds > 0:
            blockers.append("deadline_exceeded")

        plan = parameters.get("plan")
        plan_profile = plan.get("profile") if isinstance(plan, Mapping) else None
        if plan_profile != state.selected_plan_profile:
            blockers.append("plan_profile_mismatch")
        return blockers, constraints

    def _finish_incomplete(
        self,
        state: ExecutionState,
        reason: str,
    ) -> AgentRunResult:
        state.status = FinalStatus.INCOMPLETE
        state.incomplete_reason = reason
        self.state_store.save(state)
        return AgentRunResult(
            task_id=state.task_id,
            status=state.status,
            state=state,
            trace_path=self.trace.path,
            saved_plan_path=self._saved_plan_path(state),
            incomplete_reason=reason,
        )

    def _record_action(
        self,
        *,
        action: ModelAction,
        state: ExecutionState,
        result_summary: str,
        status: str,
        next_action: str,
    ) -> None:
        self.trace.append(
            action=(action.tool_name if action.kind is ActionKind.CALL_TOOL else action.kind.value),
            tool_parameters=action.parameters,
            result_summary=result_summary,
            status=status,
            incurred_cost=state.incurred_cost,
            estimated_total_cost=state.estimated_total_cost,
            elapsed_duration_seconds=state.elapsed_duration_seconds,
            next_action=next_action,
            decision_reason=action.decision_reason,
        )

    def run(self, task: TaskInput) -> AgentRunResult:
        if self.trace.task_id != task.task_id:
            raise ValueError("trace recorder task_id must match the task")
        task_fingerprint = self._task_fingerprint(task)
        if self.state_store.exists(task.task_id):
            existing_state = self.state_store.load(task.task_id)
            if existing_state.task_fingerprint != task_fingerprint:
                return self._existing_result(
                    existing_state,
                    status=FinalStatus.CONFLICT,
                    reason="task_input_conflict",
                )
            return self._existing_result(existing_state)

        started_at = self.clock()
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("AgentRunner clock must return a timezone-aware datetime")
        state = self._initial_state(task.task_id, task_fingerprint)
        self.state_store.save(state)
        last_observation: Mapping[str, Any] | None = None

        while True:
            if state.decision_step_count >= self.limits.max_steps:
                return self._finish_incomplete(state, "step_limit_reached")
            if state.tool_call_count >= self.limits.max_tool_calls:
                return self._finish_incomplete(state, "tool_call_limit_reached")

            try:
                action = self.model.choose_action(
                    self._context(task, state, last_observation, started_at)
                )
            except ModelError as exc:
                return self._finish_incomplete(
                    state,
                    f"model_error:{type(exc).__name__}",
                )

            state.decision_step_count += 1

            if action.kind is ActionKind.CALL_TOOL:
                assert action.tool_name is not None
                if state.tool_call_count >= self.limits.max_tool_calls:
                    return self._finish_incomplete(state, "tool_call_limit_reached")
                if action.tool_name == "plan_save":
                    blockers, constraints = self._plan_save_blockers(
                        task,
                        state,
                        action.parameters,
                        started_at,
                    )
                    if blockers:
                        summary = "Plan save blocked: " + ", ".join(blockers)
                        last_observation = {
                            "action": action.to_dict(),
                            "result": {
                                "success": False,
                                "error_code": "constraint_violation",
                                "retryable": False,
                                "summary": summary,
                                "blockers": blockers,
                                "constraints": constraints,
                            },
                        }
                        self._record_action(
                            action=action,
                            state=state,
                            result_summary=summary,
                            status=ExecutionStatus.BLOCKED.value,
                            next_action="request_model_decision",
                        )
                        self.state_store.save(state)
                        continue

                try:
                    step = self._step_for_action(
                        state,
                        action.tool_name,
                        action.parameters,
                    )
                    transition_step(state, step.step_id, ExecutionStatus.RUNNING)
                except (InvalidStateTransition, KeyError, TypeError, ValueError) as exc:
                    return self._finish_incomplete(
                        state,
                        f"tool_execution_error:{type(exc).__name__}",
                    )

                try:
                    result = self.tool_executor.execute(action.tool_name, action.parameters)
                except Exception as exc:
                    result = ToolResult(
                        tool_name=action.tool_name,
                        success=False,
                        summary=(
                            "Unexpected tool exception: "
                            f"{type(exc).__name__}"
                        ),
                        error_code="unexpected_tool_exception",
                        retryable=False,
                    )

                target_status = (
                    ExecutionStatus.SUCCEEDED
                    if result.success
                    else ExecutionStatus.FAILED
                )
                try:
                    transition_step(
                        state,
                        step.step_id,
                        target_status,
                        result=result,
                    )
                except (InvalidStateTransition, KeyError, TypeError, ValueError) as exc:
                    return self._finish_incomplete(
                        state,
                        f"tool_execution_error:{type(exc).__name__}",
                    )

                if action.tool_name == "cost_estimation" and result.success:
                    estimated = result.data.get("estimated_total_cost")
                    try:
                        parsed_estimated = Decimal(str(estimated))
                        if parsed_estimated.is_finite() and parsed_estimated >= 0:
                            state.estimated_total_cost = parsed_estimated
                    except (InvalidOperation, ValueError):
                        pass
                    estimated_duration = result.data.get(
                        "estimated_total_duration_seconds"
                    )
                    try:
                        parsed_duration = float(estimated_duration)
                        if parsed_duration >= 0:
                            state.estimated_total_duration_seconds = parsed_duration
                    except (TypeError, ValueError):
                        pass
                    estimated_profile = result.data.get("plan_profile")
                    if isinstance(estimated_profile, str):
                        state.estimated_plan_profile = estimated_profile

                constraints = self._update_constraint_gaps(
                    task,
                    state,
                    started_at,
                )

                last_observation = {
                    "action": action.to_dict(),
                    "result": result.to_dict(),
                    "constraints": constraints,
                }
                self._record_action(
                    action=action,
                    state=state,
                    result_summary=result.summary,
                    status=target_status.value,
                    next_action="request_model_decision",
                )
                self.state_store.save(state)
                continue

            if action.kind is ActionKind.SELECT_ALTERNATIVE:
                state.selected_plan_profile = str(action.parameters["plan_profile"])
                state.estimated_total_cost = Decimal("0")
                state.estimated_total_duration_seconds = 0.0
                state.estimated_plan_profile = None
                state.budget_gap = Decimal("0")
                state.deadline_gap_seconds = 0.0
                last_observation = {
                    "action": action.to_dict(),
                    "result": {
                        "success": True,
                        "selected_plan_profile": state.selected_plan_profile,
                    },
                }
                self._record_action(
                    action=action,
                    state=state,
                    result_summary=(
                        f"Selected alternative profile: {state.selected_plan_profile}"
                    ),
                    status="succeeded",
                    next_action="request_model_decision",
                )
                self.state_store.save(state)
                continue

            if action.kind is ActionKind.STOP:
                self._record_action(
                    action=action,
                    state=state,
                    result_summary="Model stopped the task before completion",
                    status=FinalStatus.INCOMPLETE.value,
                    next_action="end",
                )
                return self._finish_incomplete(state, action.decision_reason)

            if not self._completion_requirements_met(state):
                self._record_action(
                    action=action,
                    state=state,
                    result_summary="Completion rejected because required steps are not succeeded",
                    status=FinalStatus.INCOMPLETE.value,
                    next_action="end",
                )
                return self._finish_incomplete(
                    state,
                    "completion_requirements_not_met",
                )

            state.status = FinalStatus.COMPLETED
            state.incomplete_reason = None
            self._record_action(
                action=action,
                state=state,
                result_summary="All required steps succeeded and the plan was saved",
                status=FinalStatus.COMPLETED.value,
                next_action="end",
            )
            self.state_store.save(state)
            return AgentRunResult(
                task_id=state.task_id,
                status=state.status,
                state=state,
                trace_path=self.trace.path,
                saved_plan_path=self._saved_plan_path(state),
            )
