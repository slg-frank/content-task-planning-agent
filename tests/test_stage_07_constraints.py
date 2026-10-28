"""Stage 07 tests for budget, deadline, and dynamic replanning."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from content_agent.agent import AgentRunner, LocalToolExecutor, ToolExecutor
from content_agent.model import MockModelAdapter, ModelContext
from content_agent.models import ExecutionStatus, FinalStatus, TaskInput, ToolResult
from content_agent.state_store import StateStore
from content_agent.trace import TraceRecorder


START = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)
MATERIALS = [
    {"id": "mat-001", "title": "秋季新品发布内容框架"},
    {"id": "mat-003", "title": "新品卖点表达模板"},
]


def _task(
    task_id: str,
    *,
    budget: str,
    available_seconds: float,
) -> TaskInput:
    return TaskInput(
        topic="秋季新品内容活动",
        budget=Decimal(budget),
        deadline=START + timedelta(seconds=available_seconds),
        task_id=task_id,
        model_mode="mock",
    )


class RecordingExecutor:
    def __init__(self, delegate: ToolExecutor) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        self.calls.append((tool_name, dict(parameters)))
        return self.delegate.execute(tool_name, parameters)


def _step_succeeded(context: ModelContext, tool_name: str) -> bool:
    steps = context.execution_state.get("steps", [])
    return any(
        isinstance(step, Mapping)
        and step.get("tool_name") == tool_name
        and step.get("status") == "succeeded"
        for step in steps
    )


def _constraint_aware_policy(task_id: str):
    def policy(context: ModelContext, call_count: int) -> dict[str, object]:
        del call_count
        if not _step_succeeded(context, "material_search"):
            return {
                "action": "call_tool",
                "tool_name": "material_search",
                "parameters": {"topic": "秋季新品内容活动", "limit": 3},
                "decision_reason": "先检索方案所需素材",
            }
        if not _step_succeeded(context, "script_generation"):
            return {
                "action": "call_tool",
                "tool_name": "script_generation",
                "parameters": {
                    "topic": "秋季新品内容活动",
                    "materials": MATERIALS,
                    "variant": "standard",
                },
                "decision_reason": "素材已经成功获取，继续生成脚本",
            }

        constraints = context.execution_state.get("constraints", {})
        assert isinstance(constraints, Mapping)
        selected_profile = str(constraints["selected_plan_profile"])
        if constraints.get("requires_reestimation"):
            return {
                "action": "call_tool",
                "tool_name": "cost_estimation",
                "parameters": {"plan_profile": selected_profile},
                "decision_reason": f"需要重新估算 {selected_profile} 方案",
            }

        budget_gap = Decimal(str(constraints.get("budget_gap", "0")))
        deadline_gap = float(constraints.get("deadline_gap_seconds", 0.0))
        if budget_gap > 0 or deadline_gap > 0:
            if selected_profile == "standard":
                return {
                    "action": "select_alternative",
                    "tool_name": None,
                    "parameters": {"plan_profile": "economy"},
                    "decision_reason": (
                        f"标准方案不可行：预算缺口 {budget_gap}，"
                        f"时间缺口 {deadline_gap} 秒；改选经济方案"
                    ),
                }
            return {
                "action": "stop",
                "tool_name": None,
                "parameters": {},
                "decision_reason": (
                    f"经济方案仍不可行：预算缺口 {budget_gap}，"
                    f"时间缺口 {deadline_gap} 秒"
                ),
            }

        if not _step_succeeded(context, "plan_save"):
            plan = {
                "topic": "秋季新品内容活动",
                "profile": selected_profile,
                "estimated_total_cost": constraints["estimated_total_cost"],
            }
            return {
                "action": "call_tool",
                "tool_name": "plan_save",
                "parameters": {"task_id": task_id, "plan": plan},
                "decision_reason": "当前方案满足预算和截止时间，保存方案",
            }
        return {
            "action": "complete",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "所有步骤成功且可行方案已经保存",
        }

    return policy


def _runner(
    tmp_path: Path,
    task: TaskInput,
    model: MockModelAdapter,
) -> tuple[AgentRunner, RecordingExecutor]:
    executor = RecordingExecutor(LocalToolExecutor(runtime_root=tmp_path / "runtime"))
    runner = AgentRunner(
        model=model,
        tool_executor=executor,
        state_store=StateStore(tmp_path / "state"),
        trace_recorder=TraceRecorder(task.task_id, root=tmp_path / "traces"),
        clock=lambda: START,
    )
    return runner, executor


def test_budget_10_replans_from_12_to_8_then_saves(tmp_path: Path) -> None:
    task = _task("budget-replan", budget="10.00", available_seconds=300)
    runner, executor = _runner(
        tmp_path,
        task,
        MockModelAdapter(decision_policy=_constraint_aware_policy(task.task_id)),
    )

    result = runner.run(task)
    records = TraceRecorder(task.task_id, root=tmp_path / "traces").read_all()

    assert result.status is FinalStatus.COMPLETED
    assert result.state.selected_plan_profile == "economy"
    assert result.state.estimated_plan_profile == "economy"
    assert result.state.estimated_total_cost == Decimal("8.00")
    assert result.state.budget_gap == Decimal("0")
    assert result.saved_plan_path is not None and result.saved_plan_path.is_file()
    assert [name for name, _ in executor.calls].count("cost_estimation") == 2
    assert [params["plan_profile"] for name, params in executor.calls if name == "cost_estimation"] == [
        "standard",
        "economy",
    ]
    assert [name for name, _ in executor.calls].index("plan_save") > max(
        index
        for index, (name, _) in enumerate(executor.calls)
        if name == "cost_estimation"
    )
    assert [record.action for record in records] == [
        "material_search",
        "script_generation",
        "cost_estimation",
        "select_alternative",
        "cost_estimation",
        "plan_save",
        "complete",
    ]
    standard_steps = [
        step
        for step in result.state.steps
        if step.tool_name == "cost_estimation"
    ]
    assert len(standard_steps) == 2
    assert all(step.status is ExecutionStatus.SUCCEEDED for step in standard_steps)
    assert standard_steps[0].result is not None
    assert standard_steps[0].result.data["estimated_total_cost"] == "12.00"


def test_no_budget_feasible_profile_returns_gap_and_does_not_save(tmp_path: Path) -> None:
    task = _task("budget-no-solution", budget="5.00", available_seconds=300)
    runner, executor = _runner(
        tmp_path,
        task,
        MockModelAdapter(decision_policy=_constraint_aware_policy(task.task_id)),
    )

    result = runner.run(task)

    assert result.status is FinalStatus.INCOMPLETE
    assert result.state.selected_plan_profile == "economy"
    assert result.state.estimated_total_cost == Decimal("8.00")
    assert result.state.budget_gap == Decimal("3.00")
    assert result.incomplete_reason is not None
    assert "预算缺口 3.00" in result.incomplete_reason
    assert "plan_save" not in [name for name, _ in executor.calls]
    assert result.saved_plan_path is None


def test_deadline_overrun_selects_faster_profile_and_completes(tmp_path: Path) -> None:
    task = _task("deadline-replan", budget="20.00", available_seconds=100)
    runner, _ = _runner(
        tmp_path,
        task,
        MockModelAdapter(decision_policy=_constraint_aware_policy(task.task_id)),
    )

    result = runner.run(task)

    assert result.status is FinalStatus.COMPLETED
    assert result.state.selected_plan_profile == "economy"
    assert result.state.estimated_total_duration_seconds == 60.0
    assert result.state.deadline_gap_seconds == 0.0
    assert result.saved_plan_path is not None


def test_no_deadline_feasible_profile_stops_with_time_gap(tmp_path: Path) -> None:
    task = _task("deadline-no-solution", budget="20.00", available_seconds=50)
    runner, executor = _runner(
        tmp_path,
        task,
        MockModelAdapter(decision_policy=_constraint_aware_policy(task.task_id)),
    )

    result = runner.run(task)

    assert result.status is FinalStatus.INCOMPLETE
    assert result.state.selected_plan_profile == "economy"
    assert result.state.deadline_gap_seconds > 0
    assert result.incomplete_reason is not None
    assert "时间缺口" in result.incomplete_reason
    assert "plan_save" not in [name for name, _ in executor.calls]


def _direct_save_actions(task_id: str) -> list[dict[str, object]]:
    return [
        {
            "action": "call_tool",
            "tool_name": "material_search",
            "parameters": {"topic": "秋季新品内容活动", "limit": 3},
            "decision_reason": "检索素材",
        },
        {
            "action": "call_tool",
            "tool_name": "script_generation",
            "parameters": {
                "topic": "秋季新品内容活动",
                "materials": MATERIALS,
                "variant": "standard",
            },
            "decision_reason": "生成脚本",
        },
        {
            "action": "call_tool",
            "tool_name": "cost_estimation",
            "parameters": {"plan_profile": "standard"},
            "decision_reason": "估算标准方案",
        },
        {
            "action": "call_tool",
            "tool_name": "plan_save",
            "parameters": {
                "task_id": task_id,
                "plan": {"topic": "秋季新品", "profile": "standard"},
            },
            "decision_reason": "尝试绕过约束直接保存",
        },
        {
            "action": "stop",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "保存被保护层拒绝，停止任务",
        },
    ]


@pytest.mark.parametrize(
    ("budget", "available_seconds", "expected_blocker"),
    [
        ("10.00", 300, "budget_exceeded"),
        ("20.00", 100, "deadline_exceeded"),
    ],
)
def test_over_budget_or_over_deadline_plan_cannot_bypass_save_guard(
    tmp_path: Path,
    budget: str,
    available_seconds: float,
    expected_blocker: str,
) -> None:
    task = _task(
        f"guard-{expected_blocker}",
        budget=budget,
        available_seconds=available_seconds,
    )
    runner, executor = _runner(
        tmp_path,
        task,
        MockModelAdapter(_direct_save_actions(task.task_id)),
    )

    result = runner.run(task)
    records = TraceRecorder(task.task_id, root=tmp_path / "traces").read_all()

    assert result.status is FinalStatus.INCOMPLETE
    assert "plan_save" not in [name for name, _ in executor.calls]
    assert result.saved_plan_path is None
    blocked_record = next(record for record in records if record.action == "plan_save")
    assert blocked_record.status == "blocked"
    assert expected_blocker in blocked_record.result_summary


def test_alternative_must_be_reestimated_before_save(tmp_path: Path) -> None:
    task = _task("guard-reestimate", budget="10.00", available_seconds=300)
    actions = _direct_save_actions(task.task_id)[:3] + [
        {
            "action": "select_alternative",
            "tool_name": None,
            "parameters": {"plan_profile": "economy"},
            "decision_reason": "标准方案超预算，改选经济方案",
        },
        {
            "action": "call_tool",
            "tool_name": "plan_save",
            "parameters": {
                "task_id": task.task_id,
                "plan": {"topic": "秋季新品", "profile": "economy"},
            },
            "decision_reason": "未重新估算就尝试保存",
        },
        {
            "action": "stop",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "重新估算保护验证完成",
        },
    ]
    runner, executor = _runner(tmp_path, task, MockModelAdapter(actions))

    result = runner.run(task)
    records = TraceRecorder(task.task_id, root=tmp_path / "traces").read_all()

    assert result.status is FinalStatus.INCOMPLETE
    assert result.state.selected_plan_profile == "economy"
    assert result.state.estimated_plan_profile is None
    assert [name for name, _ in executor.calls].count("cost_estimation") == 1
    assert "plan_save" not in [name for name, _ in executor.calls]
    blocked_record = next(record for record in records if record.action == "plan_save")
    assert "reestimation_required" in blocked_record.result_summary
