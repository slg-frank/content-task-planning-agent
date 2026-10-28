"""Stage 09 tests for task idempotency and input conflicts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from content_agent.agent import AgentLimits, AgentRunner, LocalToolExecutor, ToolExecutor
from content_agent.model import MockModelAdapter
from content_agent.models import FinalStatus, TaskInput, ToolResult
from content_agent.state_store import StateStore
from content_agent.trace import TraceRecorder


START = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)
MATERIALS = [
    {"id": "mat-001", "title": "秋季新品发布内容框架"},
    {"id": "mat-003", "title": "新品卖点表达模板"},
]


def _task(
    task_id: str = "idempotent-task",
    *,
    topic: str = "秋季新品内容活动",
    budget: str = "20.00",
    deadline: datetime | None = None,
) -> TaskInput:
    return TaskInput(
        topic=topic,
        budget=Decimal(budget),
        deadline=deadline or (START + timedelta(seconds=300)),
        task_id=task_id,
        model_mode="mock",
    )


def _normal_actions(task_id: str) -> list[dict[str, object]]:
    return [
        {
            "action": "call_tool",
            "tool_name": "material_search",
            "parameters": {"topic": "秋季新品内容活动", "limit": 3},
            "decision_reason": "检索活动素材",
        },
        {
            "action": "call_tool",
            "tool_name": "script_generation",
            "parameters": {
                "topic": "秋季新品内容活动",
                "materials": MATERIALS,
                "variant": "standard",
            },
            "decision_reason": "素材成功，生成脚本",
        },
        {
            "action": "call_tool",
            "tool_name": "cost_estimation",
            "parameters": {"plan_profile": "standard"},
            "decision_reason": "脚本成功，估算成本",
        },
        {
            "action": "call_tool",
            "tool_name": "plan_save",
            "parameters": {
                "task_id": task_id,
                "plan": {
                    "topic": "秋季新品内容活动",
                    "profile": "standard",
                    "estimated_total_cost": "12.00",
                },
            },
            "decision_reason": "约束满足，保存方案",
        },
        {
            "action": "complete",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "方案已保存，完成任务",
        },
    ]


class RecordingExecutor:
    def __init__(self, delegate: ToolExecutor) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        self.calls.append((tool_name, dict(parameters)))
        return self.delegate.execute(tool_name, parameters)


def _runner(
    tmp_path: Path,
    task: TaskInput,
    *,
    limits: AgentLimits | None = None,
) -> tuple[AgentRunner, MockModelAdapter, RecordingExecutor]:
    model = MockModelAdapter(_normal_actions(task.task_id))
    executor = RecordingExecutor(LocalToolExecutor(runtime_root=tmp_path / "runtime"))
    runner = AgentRunner(
        model=model,
        tool_executor=executor,
        state_store=StateStore(tmp_path / "state"),
        trace_recorder=TraceRecorder(task.task_id, root=tmp_path / "traces"),
        limits=limits,
        clock=lambda: START,
    )
    return runner, model, executor


def test_completed_task_run_twice_saves_and_charges_only_once(tmp_path: Path) -> None:
    task = _task()
    runner, model, executor = _runner(tmp_path, task)

    first = runner.run(task)
    first_call_count = len(executor.calls)
    first_model_count = model.call_count
    first_cost = first.state.incurred_cost
    first_tool_calls = first.state.tool_call_count
    first_plan_files = list((tmp_path / "runtime" / "plans").glob("*.json"))

    second = runner.run(task)
    second_plan_files = list((tmp_path / "runtime" / "plans").glob("*.json"))

    assert first.status is FinalStatus.COMPLETED
    assert second.status is FinalStatus.COMPLETED
    assert len(first_plan_files) == 1
    assert len(second_plan_files) == 1
    assert first_plan_files[0] == second_plan_files[0]
    assert len(executor.calls) == first_call_count == 4
    assert model.call_count == first_model_count == 5
    assert second.state.incurred_cost == first_cost == Decimal("4.00")
    assert second.state.tool_call_count == first_tool_calls == 4
    assert second.to_dict() == first.to_dict()


def test_second_execution_does_not_modify_state_trace_or_saved_plan(tmp_path: Path) -> None:
    task = _task("idempotent-files")
    runner, _, _ = _runner(tmp_path, task)
    first = runner.run(task)
    assert first.saved_plan_path is not None
    state_path = runner.state_store.path_for(task.task_id)
    trace_path = runner.trace.path
    plan_path = first.saved_plan_path
    before = {
        "state": state_path.read_bytes(),
        "trace": trace_path.read_bytes(),
        "plan": plan_path.read_bytes(),
        "state_mtime": state_path.stat().st_mtime_ns,
        "trace_mtime": trace_path.stat().st_mtime_ns,
        "plan_mtime": plan_path.stat().st_mtime_ns,
    }

    runner.run(task)

    assert state_path.read_bytes() == before["state"]
    assert trace_path.read_bytes() == before["trace"]
    assert plan_path.read_bytes() == before["plan"]
    assert state_path.stat().st_mtime_ns == before["state_mtime"]
    assert trace_path.stat().st_mtime_ns == before["trace_mtime"]
    assert plan_path.stat().st_mtime_ns == before["plan_mtime"]


@pytest.mark.parametrize(
    "changed_task",
    [
        _task(topic="不同的活动主题"),
        _task(budget="19.00"),
        _task(deadline=START + timedelta(seconds=301)),
    ],
    ids=["topic", "budget", "deadline"],
)
def test_same_task_id_with_changed_critical_input_returns_conflict(
    tmp_path: Path,
    changed_task: TaskInput,
) -> None:
    original = _task()
    runner, model, executor = _runner(tmp_path, original)
    completed = runner.run(original)
    state_path = runner.state_store.path_for(original.task_id)
    trace_path = runner.trace.path
    state_before = state_path.read_bytes()
    trace_before = trace_path.read_bytes()
    calls_before = len(executor.calls)
    model_calls_before = model.call_count

    conflict = runner.run(changed_task)

    assert completed.status is FinalStatus.COMPLETED
    assert conflict.status is FinalStatus.CONFLICT
    assert conflict.incomplete_reason == "task_input_conflict"
    assert conflict.state.status is FinalStatus.COMPLETED
    assert len(executor.calls) == calls_before
    assert model.call_count == model_calls_before
    assert state_path.read_bytes() == state_before
    assert trace_path.read_bytes() == trace_before


def test_equivalent_decimal_and_timezone_inputs_share_fingerprint(tmp_path: Path) -> None:
    original = _task(
        "normalized-input",
        budget="20.00",
        deadline=datetime(2026, 10, 28, 8, 5, tzinfo=timezone.utc),
    )
    runner, model, executor = _runner(tmp_path, original)
    first = runner.run(original)
    equivalent = _task(
        "normalized-input",
        budget="20",
        deadline=datetime(
            2026,
            10,
            28,
            16,
            5,
            tzinfo=timezone(timedelta(hours=8)),
        ),
    )

    second = runner.run(equivalent)

    assert first.status is FinalStatus.COMPLETED
    assert second.status is FinalStatus.COMPLETED
    assert model.call_count == 5
    assert len(executor.calls) == 4


def test_repeated_incomplete_task_returns_original_state_without_resuming(
    tmp_path: Path,
) -> None:
    task = _task("incomplete-repeat")
    runner, model, executor = _runner(
        tmp_path,
        task,
        limits=AgentLimits(max_steps=1, max_tool_calls=8),
    )

    first = runner.run(task)
    state_path = runner.state_store.path_for(task.task_id)
    trace_path = runner.trace.path
    state_before = state_path.read_bytes()
    trace_before = trace_path.read_bytes()
    second = runner.run(task)

    assert first.status is FinalStatus.INCOMPLETE
    assert first.incomplete_reason == "step_limit_reached"
    assert second.to_dict() == first.to_dict()
    assert len(executor.calls) == 1
    assert model.call_count == 1
    assert state_path.read_bytes() == state_before
    assert trace_path.read_bytes() == trace_before
