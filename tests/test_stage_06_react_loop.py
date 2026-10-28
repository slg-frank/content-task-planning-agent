"""Stage 06 tests for the core model-action/tool-observation loop."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from content_agent.agent import (
    AgentLimits,
    AgentRunner,
    LocalToolExecutor,
    ToolExecutor,
)
from content_agent.model import MockModelAdapter, ModelContext
from content_agent.models import FinalStatus, TaskInput, ToolResult
from content_agent.state_store import StateStore
from content_agent.trace import TraceRecorder


def _task(task_id: str = "campaign-react-001") -> TaskInput:
    return TaskInput(
        topic="秋季新品内容活动",
        budget=Decimal("20.00"),
        deadline=datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc),
        task_id=task_id,
        model_mode="mock",
    )


def _normal_actions(task_id: str = "campaign-react-001") -> list[dict[str, object]]:
    materials = [
        {"id": "mat-001", "title": "秋季新品发布内容框架"},
        {"id": "mat-003", "title": "新品卖点表达模板"},
    ]
    plan = {
        "topic": "秋季新品内容活动",
        "profile": "standard",
        "estimated_total_cost": "12.00",
        "steps": ["素材检索", "脚本生成", "成本估算", "方案保存"],
    }
    return [
        {
            "action": "call_tool",
            "tool_name": "material_search",
            "parameters": {"topic": "秋季新品内容活动", "limit": 3},
            "decision_reason": "先获取与主题匹配的合成素材",
        },
        {
            "action": "call_tool",
            "tool_name": "script_generation",
            "parameters": {
                "topic": "秋季新品内容活动",
                "materials": materials,
                "variant": "standard",
            },
            "decision_reason": "素材检索成功，可以生成标准脚本",
        },
        {
            "action": "call_tool",
            "tool_name": "cost_estimation",
            "parameters": {"plan_profile": "standard"},
            "decision_reason": "脚本已生成，需要计算方案总成本",
        },
        {
            "action": "call_tool",
            "tool_name": "plan_save",
            "parameters": {"task_id": task_id, "plan": plan},
            "decision_reason": "方案步骤已完成，保存最终计划",
        },
        {
            "action": "complete",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "所有必要步骤成功且方案已经保存",
        },
    ]


class RecordingToolExecutor:
    def __init__(self, delegate: ToolExecutor) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        self.calls.append((tool_name, dict(parameters)))
        return self.delegate.execute(tool_name, parameters)


def _runner(
    tmp_path: Path,
    *,
    task_id: str,
    model: MockModelAdapter,
    executor: ToolExecutor,
    limits: AgentLimits | None = None,
) -> AgentRunner:
    return AgentRunner(
        model=model,
        tool_executor=executor,
        state_store=StateStore(tmp_path / "state"),
        trace_recorder=TraceRecorder(task_id, root=tmp_path / "traces"),
        limits=limits,
    )


def test_normal_react_path_completes_and_saves_plan(tmp_path: Path) -> None:
    task = _task()
    executor = RecordingToolExecutor(LocalToolExecutor(runtime_root=tmp_path / "runtime"))
    runner = _runner(
        tmp_path,
        task_id=task.task_id,
        model=MockModelAdapter(_normal_actions(task.task_id)),
        executor=executor,
    )

    result = runner.run(task)

    assert result.status is FinalStatus.COMPLETED
    assert result.incomplete_reason is None
    assert result.saved_plan_path is not None
    assert result.saved_plan_path.is_file()
    assert [name for name, _ in executor.calls] == [
        "material_search",
        "script_generation",
        "cost_estimation",
        "plan_save",
    ]
    assert all(step.status.value == "succeeded" for step in result.state.steps)
    assert result.state.tool_call_count == 4
    assert result.state.decision_step_count == 5
    assert result.state.incurred_cost == Decimal("4.00")
    assert result.state.estimated_total_cost == Decimal("12.00")
    assert result.state.elapsed_duration_seconds == 9.0


def test_normal_path_state_and_trace_are_persisted_consistently(tmp_path: Path) -> None:
    task = _task("campaign-react-persist")
    runner = _runner(
        tmp_path,
        task_id=task.task_id,
        model=MockModelAdapter(_normal_actions(task.task_id)),
        executor=LocalToolExecutor(runtime_root=tmp_path / "runtime"),
    )

    result = runner.run(task)
    loaded_state = StateStore(tmp_path / "state").load(task.task_id)
    records = TraceRecorder(task.task_id, root=tmp_path / "traces").read_all()

    assert loaded_state.status is FinalStatus.COMPLETED
    assert loaded_state.incurred_cost == result.state.incurred_cost
    assert loaded_state.estimated_total_cost == result.state.estimated_total_cost
    assert loaded_state.tool_call_count == 4
    assert [record.action for record in records] == [
        "material_search",
        "script_generation",
        "cost_estimation",
        "plan_save",
        "complete",
    ]
    assert records[0].result_summary.startswith("Found")
    assert records[0].next_action == "request_model_decision"
    assert records[-1].status == "completed"
    assert records[-1].next_action == "end"
    assert all(record.decision_reason for record in records)


class CostObservationExecutor:
    def __init__(self, estimated_total: str) -> None:
        self.estimated_total = estimated_total
        self.calls: list[str] = []

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        del parameters
        self.calls.append(tool_name)
        return ToolResult(
            tool_name=tool_name,
            success=True,
            summary=f"estimated at {self.estimated_total}",
            data={"estimated_total_cost": self.estimated_total},
            incurred_cost=Decimal("0.50"),
            duration_seconds=1.0,
        )


def _observation_policy(context: ModelContext, call_count: int) -> dict[str, object]:
    if call_count == 0:
        return {
            "action": "call_tool",
            "tool_name": "cost_estimation",
            "parameters": {"plan_profile": "standard"},
            "decision_reason": "先读取标准方案成本",
        }
    observation = context.last_observation or {}
    result = observation.get("result", {})
    if isinstance(result, Mapping):
        data = result.get("data", {})
        if isinstance(data, Mapping) and Decimal(str(data.get("estimated_total_cost", "0"))) > 10:
            return {
                "action": "select_alternative",
                "tool_name": None,
                "parameters": {"plan_profile": "economy"},
                "decision_reason": "观察到标准方案超过预算 10，改选经济方案",
            }
    return {
        "action": "stop",
        "tool_name": None,
        "parameters": {},
        "decision_reason": "观察验证完成，结束隔离测试",
    }


def test_different_tool_observations_change_the_next_model_action(tmp_path: Path) -> None:
    high_task = _task("observation-high")
    high_runner = _runner(
        tmp_path / "high",
        task_id=high_task.task_id,
        model=MockModelAdapter(decision_policy=_observation_policy),
        executor=CostObservationExecutor("12.00"),
    )
    high_runner.run(high_task)
    high_actions = [
        record.action
        for record in TraceRecorder(
            high_task.task_id,
            root=tmp_path / "high" / "traces",
        ).read_all()
    ]

    low_task = _task("observation-low")
    low_runner = _runner(
        tmp_path / "low",
        task_id=low_task.task_id,
        model=MockModelAdapter(decision_policy=_observation_policy),
        executor=CostObservationExecutor("8.00"),
    )
    low_runner.run(low_task)
    low_actions = [
        record.action
        for record in TraceRecorder(
            low_task.task_id,
            root=tmp_path / "low" / "traces",
        ).read_all()
    ]

    assert high_actions[:2] == ["cost_estimation", "select_alternative"]
    assert low_actions[:2] == ["cost_estimation", "stop"]


def test_max_steps_returns_explicit_incomplete_status(tmp_path: Path) -> None:
    task = _task("limit-steps")
    executor = RecordingToolExecutor(LocalToolExecutor(runtime_root=tmp_path / "runtime"))
    runner = _runner(
        tmp_path,
        task_id=task.task_id,
        model=MockModelAdapter(_normal_actions(task.task_id)),
        executor=executor,
        limits=AgentLimits(max_steps=1, max_tool_calls=8),
    )

    result = runner.run(task)

    assert result.status is FinalStatus.INCOMPLETE
    assert result.incomplete_reason == "step_limit_reached"
    assert result.state.decision_step_count == 1
    assert len(executor.calls) == 1


def test_max_tool_calls_prevents_additional_tool_execution(tmp_path: Path) -> None:
    task = _task("limit-tools")
    executor = RecordingToolExecutor(LocalToolExecutor(runtime_root=tmp_path / "runtime"))
    runner = _runner(
        tmp_path,
        task_id=task.task_id,
        model=MockModelAdapter(_normal_actions(task.task_id)),
        executor=executor,
        limits=AgentLimits(max_steps=10, max_tool_calls=1),
    )

    result = runner.run(task)

    assert result.status is FinalStatus.INCOMPLETE
    assert result.incomplete_reason == "tool_call_limit_reached"
    assert result.state.tool_call_count == 1
    assert len(executor.calls) == 1
    assert executor.calls[0][0] == "material_search"


def test_complete_action_is_rejected_until_all_required_steps_succeed(tmp_path: Path) -> None:
    task = _task("early-complete")
    model = MockModelAdapter(
        [
            {
                "action": "complete",
                "tool_name": None,
                "parameters": {},
                "decision_reason": "错误地提前完成",
            }
        ]
    )
    executor = RecordingToolExecutor(LocalToolExecutor(runtime_root=tmp_path / "runtime"))
    runner = _runner(
        tmp_path,
        task_id=task.task_id,
        model=model,
        executor=executor,
    )

    result = runner.run(task)

    assert result.status is FinalStatus.INCOMPLETE
    assert result.incomplete_reason == "completion_requirements_not_met"
    assert executor.calls == []
