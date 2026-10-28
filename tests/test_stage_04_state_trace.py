"""Stage 04 tests for execution state, persistence, and trace redaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from content_agent.models import (
    ExecutionState,
    ExecutionStatus,
    PlanStep,
    ToolResult,
)
from content_agent.state_store import (
    InvalidStateTransition,
    StateStore,
    transition_step,
)
from content_agent.trace import REDACTED, TraceRecorder


def _state(*step_ids: str) -> ExecutionState:
    return ExecutionState(
        task_id="campaign-state-001",
        steps=[
            PlanStep(step_id=step_id, name=f"步骤 {step_id}", tool_name="test_tool")
            for step_id in step_ids
        ],
    )


def _success_result(
    *,
    cost: str = "1.25",
    duration: float = 2.0,
) -> ToolResult:
    return ToolResult(
        tool_name="test_tool",
        success=True,
        summary="tool completed",
        data={"output": "synthetic result"},
        incurred_cost=Decimal(cost),
        duration_seconds=duration,
    )


def _failure_result(*, duration: float = 0.5) -> ToolResult:
    return ToolResult(
        tool_name="test_tool",
        success=False,
        summary="tool failed",
        error_code="synthetic_failure",
        duration_seconds=duration,
    )


def test_valid_state_transitions_succeed() -> None:
    state = _state("search")

    running = transition_step(state, "search", ExecutionStatus.RUNNING)
    completed = transition_step(
        state,
        "search",
        ExecutionStatus.SUCCEEDED,
        result=_success_result(),
    )

    assert running is completed
    assert completed.status is ExecutionStatus.SUCCEEDED
    assert completed.result is not None
    assert completed.result.success


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ExecutionStatus.PENDING, ExecutionStatus.SUCCEEDED),
        (ExecutionStatus.PENDING, ExecutionStatus.FAILED),
        (ExecutionStatus.SUCCEEDED, ExecutionStatus.RUNNING),
        (ExecutionStatus.SKIPPED, ExecutionStatus.RUNNING),
    ],
)
def test_invalid_state_transitions_are_rejected(
    current: ExecutionStatus,
    target: ExecutionStatus,
) -> None:
    state = _state("search")
    state.steps[0].status = current
    result = _success_result() if target is ExecutionStatus.SUCCEEDED else _failure_result()

    with pytest.raises(InvalidStateTransition):
        transition_step(state, "search", target, result=result)


def test_failed_step_cannot_be_changed_directly_to_false_success() -> None:
    state = _state("script")
    transition_step(state, "script", ExecutionStatus.RUNNING)
    transition_step(
        state,
        "script",
        ExecutionStatus.FAILED,
        result=_failure_result(),
    )

    with pytest.raises(InvalidStateTransition, match="failed -> succeeded"):
        transition_step(
            state,
            "script",
            ExecutionStatus.SUCCEEDED,
            result=_success_result(),
        )

    assert state.steps[0].status is ExecutionStatus.FAILED
    assert state.steps[0].result is not None
    assert not state.steps[0].result.success


def test_result_success_flag_must_match_terminal_status() -> None:
    successful_state = _state("save")
    transition_step(successful_state, "save", ExecutionStatus.RUNNING)
    with pytest.raises(InvalidStateTransition, match="cannot mark a step succeeded"):
        transition_step(
            successful_state,
            "save",
            ExecutionStatus.SUCCEEDED,
            result=_failure_result(),
        )

    failed_state = _state("save")
    transition_step(failed_state, "save", ExecutionStatus.RUNNING)
    with pytest.raises(InvalidStateTransition, match="cannot mark a step failed"):
        transition_step(
            failed_state,
            "save",
            ExecutionStatus.FAILED,
            result=_success_result(),
        )


def test_successful_step_result_survives_save_and_reload(tmp_path: Path) -> None:
    state = _state("search")
    transition_step(state, "search", ExecutionStatus.RUNNING)
    transition_step(
        state,
        "search",
        ExecutionStatus.SUCCEEDED,
        result=_success_result(),
    )
    store = StateStore(tmp_path / "state")

    saved_path = store.save(state)
    loaded = StateStore(tmp_path / "state").load(state.task_id)

    assert saved_path.is_file()
    assert loaded.task_id == state.task_id
    assert loaded.steps[0].status is ExecutionStatus.SUCCEEDED
    assert loaded.steps[0].result is not None
    assert loaded.steps[0].result.data == {"output": "synthetic result"}
    assert loaded.steps[0].result.incurred_cost == Decimal("1.25")


def test_cost_duration_and_tool_call_totals_accumulate_once() -> None:
    state = _state("search", "script")
    transition_step(state, "search", ExecutionStatus.RUNNING)
    transition_step(
        state,
        "search",
        ExecutionStatus.SUCCEEDED,
        result=_success_result(cost="1.25", duration=2.0),
    )
    transition_step(state, "script", ExecutionStatus.RUNNING)
    transition_step(
        state,
        "script",
        ExecutionStatus.FAILED,
        result=_failure_result(duration=0.5),
    )

    assert state.incurred_cost == Decimal("1.25")
    assert state.elapsed_duration_seconds == 2.5
    assert state.tool_call_count == 2

    with pytest.raises(InvalidStateTransition):
        transition_step(
            state,
            "search",
            ExecutionStatus.SUCCEEDED,
            result=_success_result(),
        )
    assert state.incurred_cost == Decimal("1.25")
    assert state.tool_call_count == 2


def test_failed_step_can_only_succeed_after_explicit_retry() -> None:
    state = _state("script")
    transition_step(state, "script", ExecutionStatus.RUNNING)
    transition_step(
        state,
        "script",
        ExecutionStatus.FAILED,
        result=_failure_result(),
    )
    transition_step(state, "script", ExecutionStatus.RUNNING)
    transition_step(
        state,
        "script",
        ExecutionStatus.SUCCEEDED,
        result=_success_result(),
    )

    assert state.steps[0].status is ExecutionStatus.SUCCEEDED
    assert state.tool_call_count == 2
    assert state.incurred_cost == Decimal("1.25")


def test_trace_fields_are_complete_ordered_and_reloadable(tmp_path: Path) -> None:
    recorder = TraceRecorder("trace-order-001", root=tmp_path / "traces")
    start = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)
    first = recorder.append(
        action="material_search",
        tool_parameters={"topic": "秋季新品"},
        result_summary="found three materials",
        status=ExecutionStatus.SUCCEEDED,
        incurred_cost=Decimal("1.00"),
        estimated_total_cost=Decimal("0"),
        elapsed_duration_seconds=2.0,
        next_action="script_generation",
        decision_reason="素材已满足脚本生成要求",
        timestamp=start,
    )
    second = recorder.append(
        action="script_generation",
        tool_parameters={"variant": "standard"},
        result_summary="generated three sections",
        status=ExecutionStatus.SUCCEEDED,
        incurred_cost=Decimal("3.00"),
        estimated_total_cost=Decimal("12.00"),
        elapsed_duration_seconds=7.0,
        next_action="cost_estimation",
        decision_reason="脚本生成成功，继续核算方案成本",
        timestamp=start + timedelta(seconds=1),
    )

    loaded = TraceRecorder("trace-order-001", root=tmp_path / "traces").read_all()

    assert first.step_no == 1
    assert second.step_no == 2
    assert [record.step_no for record in loaded] == [1, 2]
    assert [record.action for record in loaded] == ["material_search", "script_generation"]
    assert loaded[0].timestamp < loaded[1].timestamp
    required_fields = {
        "step_no",
        "timestamp",
        "action",
        "tool_parameters",
        "result_summary",
        "status",
        "incurred_cost",
        "estimated_total_cost",
        "elapsed_duration_seconds",
        "next_action",
        "decision_reason",
    }
    assert set(loaded[0].to_dict()) == required_fields


def test_trace_redacts_sensitive_keys_and_environment_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "stage04-super-secret-value"
    monkeypatch.setenv("CONTENT_AGENT_API_KEY", secret)
    recorder = TraceRecorder("trace-secret-001", root=tmp_path / "traces")

    record = recorder.append(
        action="model_call",
        tool_parameters={
            "api_key": secret,
            "nested": {"authorization": f"Bearer {secret}"},
            "note": f"value accidentally included: {secret}",
        },
        result_summary=f"provider response for {secret}",
        status="succeeded",
        incurred_cost=Decimal("0"),
        estimated_total_cost=Decimal("0"),
        elapsed_duration_seconds=0.0,
        next_action="finish",
        decision_reason=f"response received using {secret}",
    )

    raw_trace = recorder.path.read_text(encoding="utf-8")

    assert secret not in raw_trace
    assert record.tool_parameters["api_key"] == REDACTED
    assert record.tool_parameters["nested"]["authorization"] == REDACTED
    assert REDACTED in record.tool_parameters["note"]
    assert REDACTED in record.result_summary
    assert REDACTED in record.decision_reason
