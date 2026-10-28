"""Stage 08 tests for tool failure recovery and partial success handling."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from content_agent.agent import AgentLimits, AgentRunner, LocalToolExecutor, ToolExecutor
from content_agent.model import MockModelAdapter, ModelContext
from content_agent.models import ExecutionStatus, FinalStatus, TaskInput, ToolResult
from content_agent.state_store import StateStore
from content_agent.tools import FailureInjector
from content_agent.trace import TraceRecorder


START = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)


def _task(task_id: str) -> TaskInput:
    return TaskInput(
        topic="秋季新品内容活动",
        budget=Decimal("20.00"),
        deadline=START + timedelta(seconds=300),
        task_id=task_id,
        model_mode="mock",
    )


def _steps(context: ModelContext) -> list[Mapping[str, Any]]:
    raw_steps = context.execution_state.get("steps", [])
    return [step for step in raw_steps if isinstance(step, Mapping)]


def _step(context: ModelContext, tool_name: str) -> Mapping[str, Any] | None:
    matches = [step for step in _steps(context) if step.get("tool_name") == tool_name]
    return matches[-1] if matches else None


def _material_results(context: ModelContext) -> list[dict[str, Any]]:
    material_step = _step(context, "material_search")
    if not material_step:
        return []
    result = material_step.get("result")
    if not isinstance(result, Mapping):
        return []
    data = result.get("data")
    if not isinstance(data, Mapping):
        return []
    materials = data.get("materials")
    if not isinstance(materials, list):
        return []
    return [dict(item) for item in materials if isinstance(item, Mapping)]


def _recovery_policy(task_id: str, *, partial_search: bool = False):
    def policy(context: ModelContext, call_count: int) -> dict[str, object]:
        del call_count
        material_step = _step(context, "material_search")
        if not material_step or material_step.get("status") != "succeeded":
            return {
                "action": "call_tool",
                "tool_name": "material_search",
                "parameters": (
                    {"topic": "秋季", "limit": 4, "minimum_results": 3}
                    if partial_search
                    else {"topic": "秋季新品内容活动", "limit": 3}
                ),
                "decision_reason": "先获取真实可用的素材结果",
            }

        script_step = _step(context, "script_generation")
        script_status = script_step.get("status") if script_step else "pending"
        if script_status != "succeeded":
            if script_status == "failed":
                result = script_step.get("result") if script_step else None
                retryable = bool(result.get("retryable")) if isinstance(result, Mapping) else False
                error_code = (
                    str(result.get("error_code"))
                    if isinstance(result, Mapping)
                    else "unknown_failure"
                )
                if not retryable:
                    return {
                        "action": "stop",
                        "tool_name": None,
                        "parameters": {},
                        "decision_reason": f"脚本工具失败且不可重试：{error_code}",
                    }
                reason = f"观察到可重试错误 {error_code}，保留素材并重试脚本生成"
            else:
                reason = "素材已经可用，开始脚本生成"
            return {
                "action": "call_tool",
                "tool_name": "script_generation",
                "parameters": {
                    "topic": "秋季新品内容活动",
                    "materials": _material_results(context),
                    "variant": "standard",
                },
                "decision_reason": reason,
            }

        constraints = context.execution_state.get("constraints", {})
        assert isinstance(constraints, Mapping)
        if constraints.get("requires_reestimation"):
            return {
                "action": "call_tool",
                "tool_name": "cost_estimation",
                "parameters": {"plan_profile": "standard"},
                "decision_reason": "脚本成功，继续估算标准方案",
            }
        plan_step = _step(context, "plan_save")
        if not plan_step or plan_step.get("status") != "succeeded":
            return {
                "action": "call_tool",
                "tool_name": "plan_save",
                "parameters": {
                    "task_id": task_id,
                    "plan": {
                        "topic": "秋季新品内容活动",
                        "profile": "standard",
                        "material_count": len(_material_results(context)),
                    },
                },
                "decision_reason": "成功步骤和约束均满足，保存方案",
            }
        return {
            "action": "complete",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "所有必要步骤成功，结束任务",
        }

    return policy


class RecordingExecutor:
    def __init__(self, delegate: ToolExecutor) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        self.calls.append((tool_name, dict(parameters)))
        return self.delegate.execute(tool_name, parameters)


class NonRetryableScriptFailureExecutor:
    def __init__(self, delegate: ToolExecutor) -> None:
        self.delegate = delegate
        self.calls: list[str] = []

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        self.calls.append(tool_name)
        if tool_name == "script_generation":
            return ToolResult(
                tool_name=tool_name,
                success=False,
                summary="Script format is unsupported and has no fallback",
                error_code="unsupported_script_format",
                retryable=False,
                duration_seconds=0.2,
            )
        return self.delegate.execute(tool_name, parameters)


class UnexpectedScriptExceptionExecutor:
    def __init__(self, delegate: ToolExecutor) -> None:
        self.delegate = delegate

    def execute(self, tool_name: str, parameters: Mapping[str, Any]) -> ToolResult:
        if tool_name == "script_generation":
            raise RuntimeError("synthetic unexpected exception")
        return self.delegate.execute(tool_name, parameters)


def _runner(
    tmp_path: Path,
    task: TaskInput,
    executor: ToolExecutor,
    *,
    limits: AgentLimits | None = None,
    partial_search: bool = False,
) -> AgentRunner:
    return AgentRunner(
        model=MockModelAdapter(
            decision_policy=_recovery_policy(
                task.task_id,
                partial_search=partial_search,
            )
        ),
        tool_executor=executor,
        state_store=StateStore(tmp_path / "state"),
        trace_recorder=TraceRecorder(task.task_id, root=tmp_path / "traces"),
        limits=limits,
        clock=lambda: START,
    )


def test_one_injected_script_failure_is_observed_retried_and_recovers(
    tmp_path: Path,
) -> None:
    task = _task("recover-once")
    local = LocalToolExecutor(
        runtime_root=tmp_path / "runtime",
        failure_injector=FailureInjector({"script_generation": 1}),
    )
    executor = RecordingExecutor(local)
    runner = _runner(tmp_path, task, executor)

    result = runner.run(task)
    records = TraceRecorder(task.task_id, root=tmp_path / "traces").read_all()

    assert result.status is FinalStatus.COMPLETED
    assert [name for name, _ in executor.calls].count("script_generation") == 2
    material_step = next(
        step for step in result.state.steps if step.tool_name == "material_search"
    )
    script_step = next(
        step for step in result.state.steps if step.tool_name == "script_generation"
    )
    assert material_step.status is ExecutionStatus.SUCCEEDED
    assert material_step.result is not None
    assert material_step.result.data["matched_count"] == 3
    assert script_step.status is ExecutionStatus.SUCCEEDED
    assert result.saved_plan_path is not None and result.saved_plan_path.is_file()

    script_records = [record for record in records if record.action == "script_generation"]
    assert [record.status for record in script_records] == ["failed", "succeeded"]
    assert "Injected failure" in script_records[0].result_summary
    assert script_records[0].next_action == "request_model_decision"
    assert "可重试错误" in script_records[1].decision_reason


def test_non_retryable_failure_stops_and_preserves_successful_materials(
    tmp_path: Path,
) -> None:
    task = _task("no-alternative")
    executor = NonRetryableScriptFailureExecutor(
        LocalToolExecutor(runtime_root=tmp_path / "runtime")
    )
    runner = _runner(tmp_path, task, executor)

    result = runner.run(task)

    assert result.status is FinalStatus.INCOMPLETE
    assert result.incomplete_reason is not None
    assert "unsupported_script_format" in result.incomplete_reason
    material_step = next(
        step for step in result.state.steps if step.tool_name == "material_search"
    )
    script_step = next(
        step for step in result.state.steps if step.tool_name == "script_generation"
    )
    assert material_step.status is ExecutionStatus.SUCCEEDED
    assert material_step.result is not None and material_step.result.success
    assert script_step.status is ExecutionStatus.FAILED
    assert script_step.result is not None
    assert script_step.result.error_code == "unsupported_script_format"
    assert not script_step.result.retryable
    assert result.saved_plan_path is None


def test_partial_material_success_passes_only_real_results_forward(
    tmp_path: Path,
) -> None:
    task = _task("partial-materials")
    executor = RecordingExecutor(LocalToolExecutor(runtime_root=tmp_path / "runtime"))
    runner = _runner(
        tmp_path,
        task,
        executor,
        partial_search=True,
    )

    result = runner.run(task)

    assert result.status is FinalStatus.COMPLETED
    material_step = next(
        step for step in result.state.steps if step.tool_name == "material_search"
    )
    assert material_step.result is not None
    assert material_step.result.partial_success
    assert material_step.result.data["matched_count"] == 2
    assert material_step.result.data["missing_count"] == 1
    script_parameters = next(
        parameters
        for tool_name, parameters in executor.calls
        if tool_name == "script_generation"
    )
    assert len(script_parameters["materials"]) == 2
    script_step = next(
        step for step in result.state.steps if step.tool_name == "script_generation"
    )
    assert script_step.result is not None
    assert script_step.result.data["used_material_count"] == 2
    assert script_step.result.data["ignored_material_count"] == 0


def test_repeated_retryable_failure_cannot_exceed_tool_call_limit(
    tmp_path: Path,
) -> None:
    task = _task("retry-limit")
    executor = RecordingExecutor(
        LocalToolExecutor(
            runtime_root=tmp_path / "runtime",
            failure_injector=FailureInjector({"script_generation": 10}),
        )
    )
    runner = _runner(
        tmp_path,
        task,
        executor,
        limits=AgentLimits(max_steps=10, max_tool_calls=3),
    )

    result = runner.run(task)

    assert result.status is FinalStatus.INCOMPLETE
    assert result.incomplete_reason == "tool_call_limit_reached"
    assert result.state.tool_call_count == 3
    assert len(executor.calls) == 3
    assert [name for name, _ in executor.calls] == [
        "material_search",
        "script_generation",
        "script_generation",
    ]
    script_step = next(
        step for step in result.state.steps if step.tool_name == "script_generation"
    )
    assert script_step.status is ExecutionStatus.FAILED
    assert script_step.result is not None and script_step.result.retryable


def test_unexpected_tool_exception_becomes_non_retryable_failed_observation(
    tmp_path: Path,
) -> None:
    task = _task("unexpected-exception")
    executor = UnexpectedScriptExceptionExecutor(
        LocalToolExecutor(runtime_root=tmp_path / "runtime")
    )
    runner = _runner(tmp_path, task, executor)

    result = runner.run(task)
    records = TraceRecorder(task.task_id, root=tmp_path / "traces").read_all()

    assert result.status is FinalStatus.INCOMPLETE
    assert result.incomplete_reason is not None
    assert "unexpected_tool_exception" in result.incomplete_reason
    script_step = next(
        step for step in result.state.steps if step.tool_name == "script_generation"
    )
    assert script_step.status is ExecutionStatus.FAILED
    assert script_step.result is not None
    assert script_step.result.error_code == "unexpected_tool_exception"
    assert not script_step.result.retryable
    script_record = next(record for record in records if record.action == "script_generation")
    assert script_record.status == "failed"
    assert "RuntimeError" in script_record.result_summary
    assert script_record.next_action == "request_model_decision"
