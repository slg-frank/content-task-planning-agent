"""Observation-driven offline policy used by the CLI Mock mode."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from content_agent.model.base import ModelAction, ModelContext, parse_model_action


class PlanningMockModelAdapter:
    """Choose deterministic actions from persisted state and observations.

    This adapter proves the local workflow and is intentionally not evidence of
    real-model planning quality.
    """

    def __init__(self) -> None:
        self.call_count = 0

    @staticmethod
    def _steps(context: ModelContext) -> list[Mapping[str, Any]]:
        raw_steps = context.execution_state.get("steps", [])
        return [step for step in raw_steps if isinstance(step, Mapping)]

    @classmethod
    def _step(
        cls,
        context: ModelContext,
        tool_name: str,
    ) -> Mapping[str, Any] | None:
        matches = [
            step
            for step in cls._steps(context)
            if step.get("tool_name") == tool_name
        ]
        return matches[-1] if matches else None

    @classmethod
    def _materials(cls, context: ModelContext) -> list[dict[str, Any]]:
        step = cls._step(context, "material_search")
        result = step.get("result") if step else None
        data = result.get("data") if isinstance(result, Mapping) else None
        materials = data.get("materials") if isinstance(data, Mapping) else None
        if not isinstance(materials, list):
            return []
        return [dict(item) for item in materials if isinstance(item, Mapping)]

    @staticmethod
    def _validated(raw: Mapping[str, Any]) -> ModelAction:
        return parse_model_action(raw)

    def _handle_failed_observation(
        self,
        context: ModelContext,
    ) -> ModelAction | None:
        observation = context.last_observation
        if not isinstance(observation, Mapping):
            return None
        result = observation.get("result")
        action = observation.get("action")
        if not isinstance(result, Mapping) or result.get("success") is not False:
            return None
        if result.get("error_code") == "constraint_violation":
            return None

        error_code = str(result.get("error_code", "unknown_tool_failure"))
        if bool(result.get("retryable")) and isinstance(action, Mapping):
            tool_name = action.get("tool_name")
            parameters = action.get("parameters")
            if isinstance(tool_name, str) and isinstance(parameters, Mapping):
                return self._validated(
                    {
                        "action": "call_tool",
                        "tool_name": tool_name,
                        "parameters": dict(parameters),
                        "decision_reason": (
                            f"观察到可重试工具错误 {error_code}，使用相同参数重试"
                        ),
                    }
                )
        return self._validated(
            {
                "action": "stop",
                "tool_name": None,
                "parameters": {},
                "decision_reason": f"工具错误不可重试且无安全替代方案：{error_code}",
            }
        )

    def choose_action(self, context: ModelContext) -> ModelAction:
        self.call_count += 1
        failed_action = self._handle_failed_observation(context)
        if failed_action is not None:
            return failed_action

        topic = str(context.task.get("topic", "")).strip()
        task_id = str(context.task.get("task_id", "")).strip()
        selected_profile = str(
            context.execution_state.get("selected_plan_profile", "standard")
        )

        material_step = self._step(context, "material_search")
        if not material_step or material_step.get("status") != "succeeded":
            return self._validated(
                {
                    "action": "call_tool",
                    "tool_name": "material_search",
                    "parameters": {"topic": topic, "limit": 4, "minimum_results": 1},
                    "decision_reason": "当前没有成功素材结果，先执行素材检索",
                }
            )

        script_step = self._step(context, "script_generation")
        if not script_step or script_step.get("status") != "succeeded":
            materials = self._materials(context)
            if not materials:
                return self._validated(
                    {
                        "action": "stop",
                        "tool_name": None,
                        "parameters": {},
                        "decision_reason": "素材工具没有返回可用于脚本生成的真实素材",
                    }
                )
            return self._validated(
                {
                    "action": "call_tool",
                    "tool_name": "script_generation",
                    "parameters": {
                        "topic": topic,
                        "materials": materials,
                        "variant": (
                            "economy" if selected_profile == "economy" else "standard"
                        ),
                    },
                    "decision_reason": "已有可用素材，继续生成与当前方案匹配的脚本",
                }
            )

        constraints = context.execution_state.get("constraints", {})
        if not isinstance(constraints, Mapping):
            constraints = {}
        if bool(constraints.get("requires_reestimation", True)):
            return self._validated(
                {
                    "action": "call_tool",
                    "tool_name": "cost_estimation",
                    "parameters": {"plan_profile": selected_profile},
                    "decision_reason": f"需要估算当前 {selected_profile} 方案的费用和时长",
                }
            )

        budget_gap = Decimal(str(constraints.get("budget_gap", "0")))
        deadline_gap = float(constraints.get("deadline_gap_seconds", 0.0))
        if budget_gap > 0 or deadline_gap > 0:
            if selected_profile == "standard":
                return self._validated(
                    {
                        "action": "select_alternative",
                        "tool_name": None,
                        "parameters": {"plan_profile": "economy"},
                        "decision_reason": (
                            f"标准方案不可行：预算缺口 {budget_gap}，"
                            f"时间缺口 {deadline_gap} 秒；改选经济方案"
                        ),
                    }
                )
            return self._validated(
                {
                    "action": "stop",
                    "tool_name": None,
                    "parameters": {},
                    "decision_reason": (
                        f"经济方案仍不可行：预算缺口 {budget_gap}，"
                        f"时间缺口 {deadline_gap} 秒"
                    ),
                }
            )

        save_step = self._step(context, "plan_save")
        if not save_step or save_step.get("status") != "succeeded":
            plan_steps = [
                {
                    "name": str(step.get("name", "")),
                    "tool_name": step.get("tool_name"),
                    "status": step.get("status"),
                }
                for step in self._steps(context)
                if step.get("tool_name") != "plan_save"
            ]
            plan = {
                "topic": topic,
                "profile": selected_profile,
                "estimated_total_cost": str(
                    constraints.get("estimated_total_cost", "0")
                ),
                "estimated_total_duration_seconds": constraints.get(
                    "estimated_total_duration_seconds",
                    0.0,
                ),
                "steps": plan_steps,
            }
            return self._validated(
                {
                    "action": "call_tool",
                    "tool_name": "plan_save",
                    "parameters": {"task_id": task_id, "plan": plan},
                    "decision_reason": "必要步骤成功且预算与截止时间均满足，保存方案",
                }
            )

        return self._validated(
            {
                "action": "complete",
                "tool_name": None,
                "parameters": {},
                "decision_reason": "所有必要步骤成功且方案已经保存",
            }
        )
