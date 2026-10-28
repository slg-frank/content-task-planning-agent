"""Validation for raw content-planning task input."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from uuid import uuid4

from content_agent.models import (
    InputErrorCode,
    TaskInput,
    ValidationIssue,
    ValidationResult,
)


TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ALLOWED_MODEL_MODES = frozenset({"mock", "real"})


def _issue(code: InputErrorCode, field: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, field=field, message=message)


def _validate_topic(raw_topic: Any, *, is_missing: bool) -> tuple[str | None, ValidationIssue | None]:
    if is_missing or raw_topic is None:
        return None, _issue(InputErrorCode.MISSING_TOPIC, "topic", "topic is required")
    if not isinstance(raw_topic, str):
        return None, _issue(InputErrorCode.INVALID_TOPIC, "topic", "topic must be a string")

    topic = raw_topic.strip()
    if not topic:
        return None, _issue(InputErrorCode.INVALID_TOPIC, "topic", "topic must not be empty")
    if len(topic) > 500:
        return None, _issue(
            InputErrorCode.INVALID_TOPIC,
            "topic",
            "topic must not exceed 500 characters",
        )
    return topic, None


def _validate_budget(raw_budget: Any, *, is_missing: bool) -> tuple[Decimal | None, ValidationIssue | None]:
    if is_missing or raw_budget is None:
        return None, _issue(InputErrorCode.MISSING_BUDGET, "budget", "budget is required")
    if isinstance(raw_budget, bool):
        return None, _issue(InputErrorCode.INVALID_BUDGET, "budget", "budget must be a finite number")

    try:
        budget = Decimal(str(raw_budget).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None, _issue(InputErrorCode.INVALID_BUDGET, "budget", "budget must be a finite number")

    if not budget.is_finite():
        return None, _issue(InputErrorCode.INVALID_BUDGET, "budget", "budget must be a finite number")
    if budget < 0:
        return None, _issue(InputErrorCode.NEGATIVE_BUDGET, "budget", "budget must be zero or greater")
    return budget, None


def _parse_deadline(raw_deadline: Any) -> datetime | None:
    if isinstance(raw_deadline, datetime):
        return raw_deadline
    if not isinstance(raw_deadline, str):
        return None

    value = raw_deadline.strip()
    if not value:
        return None
    if value.endswith(("Z", "z")):
        value = f"{value[:-1]}+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _validate_deadline(
    raw_deadline: Any,
    *,
    is_missing: bool,
    now: datetime,
) -> tuple[datetime | None, ValidationIssue | None]:
    if is_missing or raw_deadline is None:
        return None, _issue(InputErrorCode.MISSING_DEADLINE, "deadline", "deadline is required")

    deadline = _parse_deadline(raw_deadline)
    if deadline is None:
        return None, _issue(
            InputErrorCode.INVALID_DEADLINE,
            "deadline",
            "deadline must be a valid ISO 8601 date-time",
        )
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        return None, _issue(
            InputErrorCode.DEADLINE_TIMEZONE_REQUIRED,
            "deadline",
            "deadline must include a UTC offset or Z suffix",
        )
    if deadline <= now:
        return None, _issue(
            InputErrorCode.DEADLINE_NOT_FUTURE,
            "deadline",
            "deadline must be later than the task start time",
        )
    return deadline, None


def _validate_task_id(raw_task_id: Any) -> tuple[str | None, ValidationIssue | None]:
    if raw_task_id is None:
        return str(uuid4()), None
    if not isinstance(raw_task_id, str):
        return None, _issue(
            InputErrorCode.INVALID_TASK_ID,
            "task_id",
            "task_id must be a string when provided",
        )

    task_id = raw_task_id.strip()
    if not TASK_ID_PATTERN.fullmatch(task_id):
        return None, _issue(
            InputErrorCode.INVALID_TASK_ID,
            "task_id",
            "task_id must be 1-128 characters using letters, digits, dot, underscore, or hyphen",
        )
    return task_id, None


def _validate_model_mode(raw_model_mode: Any) -> tuple[str | None, ValidationIssue | None]:
    if raw_model_mode is None:
        return "mock", None
    if not isinstance(raw_model_mode, str) or raw_model_mode not in ALLOWED_MODEL_MODES:
        return None, _issue(
            InputErrorCode.INVALID_MODEL_MODE,
            "model_mode",
            "model_mode must be either 'mock' or 'real'",
        )
    return raw_model_mode, None


def validate_task_input(
    raw_input: Mapping[str, Any] | Any,
    *,
    now: datetime | None = None,
) -> ValidationResult:
    """Validate raw input without creating steps or incurring any cost.

    The optional ``now`` argument exists to make deadline tests deterministic.
    It must be timezone-aware when provided.
    """
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    if not isinstance(raw_input, Mapping):
        return ValidationResult(
            task=None,
            errors=(
                _issue(
                    InputErrorCode.INVALID_PAYLOAD,
                    "$",
                    "input must be an object with named fields",
                ),
            ),
        )

    errors: list[ValidationIssue] = []

    topic, topic_error = _validate_topic("" if "topic" not in raw_input else raw_input["topic"], is_missing="topic" not in raw_input)
    budget, budget_error = _validate_budget(
        "" if "budget" not in raw_input else raw_input["budget"],
        is_missing="budget" not in raw_input,
    )
    deadline, deadline_error = _validate_deadline(
        "" if "deadline" not in raw_input else raw_input["deadline"],
        is_missing="deadline" not in raw_input,
        now=current_time,
    )
    task_id, task_id_error = _validate_task_id(raw_input.get("task_id"))
    model_mode, model_mode_error = _validate_model_mode(raw_input.get("model_mode"))

    failure_injection_raw = raw_input.get("failure_injection", {})
    if not isinstance(failure_injection_raw, Mapping):
        failure_injection: dict[str, Any] | None = None
        failure_injection_error = _issue(
            InputErrorCode.INVALID_FAILURE_INJECTION,
            "failure_injection",
            "failure_injection must be an object when provided",
        )
    else:
        failure_injection = dict(failure_injection_raw)
        failure_injection_error = None

    for error in (
        topic_error,
        budget_error,
        deadline_error,
        task_id_error,
        model_mode_error,
        failure_injection_error,
    ):
        if error is not None:
            errors.append(error)

    if errors:
        return ValidationResult(task=None, errors=tuple(errors))

    assert topic is not None
    assert budget is not None
    assert deadline is not None
    assert task_id is not None
    assert model_mode is not None
    assert failure_injection is not None

    return ValidationResult(
        task=TaskInput(
            topic=topic,
            budget=budget,
            deadline=deadline,
            task_id=task_id,
            model_mode=model_mode,
            failure_injection=failure_injection,
        )
    )
