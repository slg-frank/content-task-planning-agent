"""Domain models shared by the content task planning agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


ZERO_COST = Decimal("0")


class ExecutionStatus(str, Enum):
    """Lifecycle states for an executable plan step."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class FinalStatus(str, Enum):
    """Top-level outcomes returned to a caller."""

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"


class InputErrorCode(str, Enum):
    """Stable error codes produced by input validation."""

    INVALID_PAYLOAD = "invalid_payload"
    MISSING_TOPIC = "missing_topic"
    INVALID_TOPIC = "invalid_topic"
    MISSING_BUDGET = "missing_budget"
    INVALID_BUDGET = "invalid_budget"
    NEGATIVE_BUDGET = "negative_budget"
    MISSING_DEADLINE = "missing_deadline"
    INVALID_DEADLINE = "invalid_deadline"
    DEADLINE_TIMEZONE_REQUIRED = "deadline_timezone_required"
    DEADLINE_NOT_FUTURE = "deadline_not_future"
    INVALID_TASK_ID = "invalid_task_id"
    INVALID_MODEL_MODE = "invalid_model_mode"
    INVALID_FAILURE_INJECTION = "invalid_failure_injection"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A field-level validation error safe to return to a caller."""

    code: InputErrorCode
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TaskInput:
    """Validated task input used by later execution stages."""

    topic: str
    budget: Decimal
    deadline: datetime
    task_id: str
    model_mode: str = "mock"
    failure_injection: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "budget": str(self.budget),
            "deadline": self.deadline.isoformat(),
            "task_id": self.task_id,
            "model_mode": self.model_mode,
            "failure_injection": dict(self.failure_injection),
        }


@dataclass(slots=True)
class ToolResult:
    """Normalized result returned by a business tool."""

    tool_name: str
    success: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    incurred_cost: Decimal = ZERO_COST
    duration_seconds: float = 0.0
    error_code: str | None = None
    retryable: bool = False
    partial_success: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "summary": self.summary,
            "data": dict(self.data),
            "incurred_cost": str(self.incurred_cost),
            "duration_seconds": self.duration_seconds,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "partial_success": self.partial_success,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class PlanStep:
    """One planned or executed unit of work."""

    step_id: str
    name: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    tool_name: str | None = None
    result: ToolResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "status": self.status.value,
            "tool_name": self.tool_name,
            "result": self.result.to_dict() if self.result else None,
        }


@dataclass(slots=True)
class TaskPlan:
    """A task plan and its accumulated execution totals."""

    task_id: str
    steps: list[PlanStep] = field(default_factory=list)
    incurred_cost: Decimal = ZERO_COST
    estimated_total_cost: Decimal = ZERO_COST
    estimated_total_duration_seconds: float = 0.0
    estimated_plan_profile: str | None = None
    budget_gap: Decimal = ZERO_COST
    deadline_gap_seconds: float = 0.0
    elapsed_duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "steps": [step.to_dict() for step in self.steps],
            "incurred_cost": str(self.incurred_cost),
            "estimated_total_cost": str(self.estimated_total_cost),
            "estimated_total_duration_seconds": self.estimated_total_duration_seconds,
            "estimated_plan_profile": self.estimated_plan_profile,
            "budget_gap": str(self.budget_gap),
            "deadline_gap_seconds": self.deadline_gap_seconds,
            "elapsed_duration_seconds": self.elapsed_duration_seconds,
        }


@dataclass(slots=True)
class FinalResult:
    """Top-level result returned after validation or execution."""

    status: FinalStatus
    task_id: str | None = None
    plan: TaskPlan | None = None
    errors: list[ValidationIssue] = field(default_factory=list)
    incomplete_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "task_id": self.task_id,
            "plan": self.plan.to_dict() if self.plan else None,
            "errors": [error.to_dict() for error in self.errors],
            "incomplete_reason": self.incomplete_reason,
        }


@dataclass(slots=True)
class ExecutionState:
    """Persistable state for one agent execution."""

    task_id: str
    task_fingerprint: str | None = None
    status: FinalStatus = FinalStatus.INCOMPLETE
    steps: list[PlanStep] = field(default_factory=list)
    incurred_cost: Decimal = ZERO_COST
    estimated_total_cost: Decimal = ZERO_COST
    estimated_total_duration_seconds: float = 0.0
    estimated_plan_profile: str | None = None
    budget_gap: Decimal = ZERO_COST
    deadline_gap_seconds: float = 0.0
    elapsed_duration_seconds: float = 0.0
    tool_call_count: int = 0
    decision_step_count: int = 0
    selected_plan_profile: str = "standard"
    incomplete_reason: str | None = None
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_fingerprint": self.task_fingerprint,
            "status": self.status.value,
            "steps": [step.to_dict() for step in self.steps],
            "incurred_cost": str(self.incurred_cost),
            "estimated_total_cost": str(self.estimated_total_cost),
            "estimated_total_duration_seconds": self.estimated_total_duration_seconds,
            "estimated_plan_profile": self.estimated_plan_profile,
            "budget_gap": str(self.budget_gap),
            "deadline_gap_seconds": self.deadline_gap_seconds,
            "elapsed_duration_seconds": self.elapsed_duration_seconds,
            "tool_call_count": self.tool_call_count,
            "decision_step_count": self.decision_step_count,
            "selected_plan_profile": self.selected_plan_profile,
            "incomplete_reason": self.incomplete_reason,
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result of validating raw input before any paid work is allowed."""

    task: TaskInput | None
    errors: tuple[ValidationIssue, ...] = ()
    steps: tuple[PlanStep, ...] = ()
    incurred_cost: Decimal = ZERO_COST

    @property
    def is_valid(self) -> bool:
        return self.task is not None and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "task": self.task.to_dict() if self.task else None,
            "errors": [error.to_dict() for error in self.errors],
            "steps": [step.to_dict() for step in self.steps],
            "incurred_cost": str(self.incurred_cost),
        }
