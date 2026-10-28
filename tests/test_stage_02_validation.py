"""Stage 02 tests for domain models and input validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from content_agent.models import (
    ExecutionStatus,
    FinalResult,
    FinalStatus,
    InputErrorCode,
    PlanStep,
    TaskPlan,
    ToolResult,
)
from content_agent.validation import validate_task_input


NOW = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)


def valid_payload() -> dict[str, object]:
    return {
        "topic": "秋季新品内容活动",
        "budget": "10.50",
        "deadline": (NOW + timedelta(days=2)).isoformat(),
        "task_id": "campaign-001",
    }


def error_codes(result: object) -> set[InputErrorCode]:
    return {error.code for error in result.errors}  # type: ignore[attr-defined]


def assert_rejected_without_execution(result: object) -> None:
    assert not result.is_valid  # type: ignore[attr-defined]
    assert result.task is None  # type: ignore[attr-defined]
    assert result.steps == ()  # type: ignore[attr-defined]
    assert result.incurred_cost == Decimal("0")  # type: ignore[attr-defined]


def test_complete_valid_input_is_normalized() -> None:
    result = validate_task_input(valid_payload(), now=NOW)

    assert result.is_valid
    assert result.errors == ()
    assert result.task is not None
    assert result.task.topic == "秋季新品内容活动"
    assert result.task.budget == Decimal("10.50")
    assert result.task.task_id == "campaign-001"
    assert result.task.model_mode == "mock"
    assert result.task.deadline.tzinfo is not None


def test_missing_topic_is_rejected() -> None:
    payload = valid_payload()
    payload.pop("topic")

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.MISSING_TOPIC in error_codes(result)
    assert_rejected_without_execution(result)


def test_missing_budget_is_rejected() -> None:
    payload = valid_payload()
    payload.pop("budget")

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.MISSING_BUDGET in error_codes(result)
    assert_rejected_without_execution(result)


def test_missing_deadline_is_rejected() -> None:
    payload = valid_payload()
    payload.pop("deadline")

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.MISSING_DEADLINE in error_codes(result)
    assert_rejected_without_execution(result)


@pytest.mark.parametrize("budget", [-1, "-0.01"])
def test_negative_budget_is_rejected(budget: object) -> None:
    payload = valid_payload()
    payload["budget"] = budget

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.NEGATIVE_BUDGET in error_codes(result)
    assert_rejected_without_execution(result)


@pytest.mark.parametrize("budget", ["not-a-number", "NaN", "Infinity", True, object()])
def test_non_numeric_or_non_finite_budget_is_rejected(budget: object) -> None:
    payload = valid_payload()
    payload["budget"] = budget

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.INVALID_BUDGET in error_codes(result)
    assert_rejected_without_execution(result)


@pytest.mark.parametrize("deadline", ["tomorrow", "2026-99-99T10:00:00+08:00", 123])
def test_invalid_deadline_format_is_rejected(deadline: object) -> None:
    payload = valid_payload()
    payload["deadline"] = deadline

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.INVALID_DEADLINE in error_codes(result)
    assert_rejected_without_execution(result)


def test_deadline_without_timezone_is_rejected() -> None:
    payload = valid_payload()
    payload["deadline"] = "2026-10-30T12:00:00"

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.DEADLINE_TIMEZONE_REQUIRED in error_codes(result)
    assert_rejected_without_execution(result)


def test_past_deadline_is_rejected() -> None:
    payload = valid_payload()
    payload["deadline"] = (NOW - timedelta(seconds=1)).isoformat()

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.DEADLINE_NOT_FUTURE in error_codes(result)
    assert_rejected_without_execution(result)


def test_zero_budget_and_z_suffix_are_valid() -> None:
    payload = valid_payload()
    payload["budget"] = 0
    payload["deadline"] = "2026-10-30T08:00:00Z"

    result = validate_task_input(payload, now=NOW)

    assert result.is_valid
    assert result.task is not None
    assert result.task.budget == Decimal("0")
    assert result.task.deadline.utcoffset() == timedelta(0)


def test_missing_optional_task_id_generates_one() -> None:
    payload = valid_payload()
    payload.pop("task_id")

    result = validate_task_input(payload, now=NOW)

    assert result.is_valid
    assert result.task is not None
    assert result.task.task_id


@pytest.mark.parametrize("task_id", ["", "contains spaces", "/absolute/path", 123])
def test_invalid_task_id_is_rejected(task_id: object) -> None:
    payload = valid_payload()
    payload["task_id"] = task_id

    result = validate_task_input(payload, now=NOW)

    assert InputErrorCode.INVALID_TASK_ID in error_codes(result)
    assert_rejected_without_execution(result)


def test_multiple_missing_fields_are_reported_together() -> None:
    result = validate_task_input({}, now=NOW)

    assert error_codes(result) >= {
        InputErrorCode.MISSING_TOPIC,
        InputErrorCode.MISSING_BUDGET,
        InputErrorCode.MISSING_DEADLINE,
    }
    assert_rejected_without_execution(result)


def test_non_mapping_payload_is_rejected_without_execution() -> None:
    result = validate_task_input(["not", "an", "object"], now=NOW)

    assert error_codes(result) == {InputErrorCode.INVALID_PAYLOAD}
    assert_rejected_without_execution(result)


def test_naive_now_is_rejected_as_programmer_error() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_task_input(valid_payload(), now=datetime(2026, 10, 28, 8, 0))


def test_domain_models_serialize_stable_values() -> None:
    tool_result = ToolResult(
        tool_name="material_search",
        success=True,
        summary="found one synthetic item",
        data={"count": 1},
        incurred_cost=Decimal("1.25"),
        duration_seconds=2.0,
    )
    step = PlanStep(
        step_id="step-1",
        name="检索素材",
        status=ExecutionStatus.SUCCEEDED,
        tool_name="material_search",
        result=tool_result,
    )
    plan = TaskPlan(task_id="campaign-001", steps=[step], incurred_cost=Decimal("1.25"))
    final = FinalResult(status=FinalStatus.COMPLETED, task_id="campaign-001", plan=plan)

    serialized = final.to_dict()

    assert serialized["status"] == "completed"
    assert serialized["plan"]["steps"][0]["status"] == "succeeded"  # type: ignore[index]
    assert serialized["plan"]["incurred_cost"] == "1.25"  # type: ignore[index]
