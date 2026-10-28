"""Stage 05 tests for provider-neutral, validated model actions."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from content_agent.model import (
    ActionKind,
    MockModelAdapter,
    ModelConfigurationError,
    ModelContext,
    ModelOutputError,
    OpenAIModelAdapter,
    create_model_adapter,
    parse_model_action,
)


def _context() -> ModelContext:
    return ModelContext(
        task={
            "task_id": "campaign-model-001",
            "topic": "秋季新品内容活动",
            "budget": "10.00",
        },
        execution_state={"steps": [], "incurred_cost": "0"},
        last_observation=None,
    )


def _tool_action() -> dict[str, object]:
    return {
        "action": "call_tool",
        "tool_name": "material_search",
        "parameters": {"topic": "秋季新品内容活动", "limit": 3},
        "decision_reason": "尚无素材，先检索与主题匹配的合成素材",
    }


def test_mock_model_returns_a_valid_tool_action() -> None:
    adapter = MockModelAdapter([_tool_action()])

    action = adapter.choose_action(_context())

    assert action.kind is ActionKind.CALL_TOOL
    assert action.tool_name == "material_search"
    assert action.parameters == {"topic": "秋季新品内容活动", "limit": 3}
    assert adapter.call_count == 1


@pytest.mark.parametrize(
    ("raw", "expected_kind"),
    [
        (
            {
                "action": "call_tool",
                "tool_name": "cost_estimation",
                "parameters": {"plan_profile": "standard"},
                "decision_reason": "脚本已生成，继续估算成本",
            },
            ActionKind.CALL_TOOL,
        ),
        (
            {
                "action": "select_alternative",
                "tool_name": None,
                "parameters": {"plan_profile": "economy"},
                "decision_reason": "标准方案超预算，改选经济方案",
            },
            ActionKind.SELECT_ALTERNATIVE,
        ),
        (
            {
                "action": "stop",
                "tool_name": None,
                "parameters": {},
                "decision_reason": "没有满足预算的可行方案",
            },
            ActionKind.STOP,
        ),
        (
            {
                "action": "complete",
                "tool_name": None,
                "parameters": {},
                "decision_reason": "方案已经成功保存",
            },
            ActionKind.COMPLETE,
        ),
    ],
)
def test_continue_alternative_stop_and_complete_actions_parse(
    raw: dict[str, object],
    expected_kind: ActionKind,
) -> None:
    action = parse_model_action(raw)

    assert action.kind is expected_kind


@pytest.mark.parametrize("tool_name", ["shell", "delete_files", "unknown_tool", ""])
def test_unknown_tool_actions_are_rejected(tool_name: str) -> None:
    raw = _tool_action()
    raw["tool_name"] = tool_name

    with pytest.raises(ModelOutputError, match="unknown or missing tool"):
        parse_model_action(raw)


@pytest.mark.parametrize(
    ("tool_name", "parameters", "missing_name"),
    [
        ("material_search", {}, "topic"),
        ("script_generation", {"topic": "测试"}, "materials"),
        ("cost_estimation", {}, "plan_profile"),
        ("plan_save", {"task_id": "task-1"}, "plan"),
    ],
)
def test_missing_required_tool_parameters_are_rejected(
    tool_name: str,
    parameters: dict[str, object],
    missing_name: str,
) -> None:
    raw = {
        "action": "call_tool",
        "tool_name": tool_name,
        "parameters": parameters,
        "decision_reason": "测试缺少参数",
    }

    with pytest.raises(ModelOutputError, match=missing_name):
        parse_model_action(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "not-an-object",
        {"tool_name": None, "parameters": {}, "decision_reason": "missing action"},
        {
            "action": "invent_action",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "unknown action",
        },
        {
            "action": "complete",
            "tool_name": None,
            "parameters": [],
            "decision_reason": "parameters are not an object",
        },
        {
            "action": "complete",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "",
        },
        {
            "action": "complete",
            "tool_name": None,
            "parameters": {},
            "decision_reason": "done",
            "unexpected": "unsafe extra field",
        },
    ],
)
def test_invalid_model_structures_are_rejected(raw: object) -> None:
    with pytest.raises(ModelOutputError):
        parse_model_action(raw)


def test_invalid_model_output_cannot_reach_tool_execution() -> None:
    executed: list[str] = []

    def execute_tool(tool_name: str) -> None:
        executed.append(tool_name)

    raw = {
        "action": "call_tool",
        "tool_name": "material_search",
        "parameters": {"limit": 3},
        "decision_reason": "topic is missing",
    }

    with pytest.raises(ModelOutputError):
        action = parse_model_action(raw)
        execute_tool(action.tool_name or "")

    assert executed == []


class _FakeResponses:
    def __init__(self, output: dict[str, object] | str) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        output_text = self.output if isinstance(self.output, str) else json.dumps(self.output)
        return SimpleNamespace(output_text=output_text)


class _FakeOpenAIClient:
    def __init__(self, output: dict[str, object] | str) -> None:
        self.responses = _FakeResponses(output)


def test_real_adapter_uses_strict_schema_then_applies_local_validation() -> None:
    client = _FakeOpenAIClient(
        {
            "action": "call_tool",
            "tool_name": "cost_estimation",
            "parameters_json": '{"plan_profile": "standard"}',
            "decision_reason": "已有脚本，需要计算方案总成本",
        }
    )
    adapter = OpenAIModelAdapter(
        model="configured-test-model",
        api_key="test-value-not-a-real-key",
        client=client,
    )

    action = adapter.choose_action(_context())

    assert action.kind is ActionKind.CALL_TOOL
    assert action.tool_name == "cost_estimation"
    assert action.parameters == {"plan_profile": "standard"}
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["model"] == "configured-test-model"
    assert call["text"]["format"]["type"] == "json_schema"  # type: ignore[index]
    assert call["text"]["format"]["strict"] is True  # type: ignore[index]


def test_real_adapter_rejects_invalid_parameters_json_without_execution() -> None:
    client = _FakeOpenAIClient(
        {
            "action": "call_tool",
            "tool_name": "material_search",
            "parameters_json": "[]",
            "decision_reason": "invalid parameters",
        }
    )
    adapter = OpenAIModelAdapter(
        model="configured-test-model",
        api_key="test-value-not-a-real-key",
        client=client,
    )

    with pytest.raises(ModelOutputError, match="parameters_json"):
        adapter.choose_action(_context())


def test_real_mode_without_api_key_has_clear_mock_guidance() -> None:
    with pytest.raises(ModelConfigurationError, match="OPENAI_API_KEY.*model_mode='mock'"):
        create_model_adapter(
            "real",
            environ={"OPENAI_MODEL": "configured-test-model"},
        )

    mock = create_model_adapter("mock", scripted_actions=[_tool_action()], environ={})
    action = mock.choose_action(_context())
    assert action.tool_name == "material_search"


def test_real_mode_requires_explicit_model_name() -> None:
    with pytest.raises(ModelConfigurationError, match="OPENAI_MODEL"):
        create_model_adapter("real", environ={"OPENAI_API_KEY": "placeholder-value"})


def test_mock_script_exhaustion_is_explicit() -> None:
    adapter = MockModelAdapter([_tool_action()])
    adapter.choose_action(_context())

    with pytest.raises(ModelOutputError, match="exhausted"):
        adapter.choose_action(_context())
