"""Stage 11 acceptance tests for recorded end-to-end examples and documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = PROJECT_ROOT / "examples"
SCENARIOS = (
    "normal",
    "missing_input",
    "tool_failure",
    "budget_exceeded",
    "repeated_execution",
)
DOCUMENTS = (
    "docs/01-requirements.md",
    "docs/02-solution.md",
    "docs/03-evaluation.md",
    "docs/04-ai-usage.md",
    "docs/05-retrospective.md",
)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _trace(scenario: str) -> list[dict[str, Any]]:
    path = EXAMPLES_ROOT / scenario / "trace.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert all(isinstance(record, dict) for record in records)
    return records


def _step(output: dict[str, Any], tool_name: str) -> dict[str, Any]:
    return next(
        step
        for step in output["state"]["steps"]
        if step["tool_name"] == tool_name
    )


def test_five_actual_cli_scenario_artifact_sets_are_complete() -> None:
    assert (EXAMPLES_ROOT / "generate_stage_11_examples.py").is_file()
    for scenario in SCENARIOS:
        scenario_dir = EXAMPLES_ROOT / scenario
        output_names = (
            ("output-first.json", "output-second.json")
            if scenario == "repeated_execution"
            else ("output.json",)
        )
        required = (
            "input.json",
            "run-record.json",
            "trace.jsonl",
            *output_names,
        )
        assert all((scenario_dir / name).is_file() for name in required), scenario
        record = _json(scenario_dir / "run-record.json")
        assert record["actual_cli_execution"] is True
        assert record["scenario"] == scenario
        assert record["path_normalization"]["applied"] is True


def test_all_recorded_json_and_jsonl_are_parseable_and_portable() -> None:
    for scenario in SCENARIOS:
        scenario_dir = EXAMPLES_ROOT / scenario
        for path in scenario_dir.glob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
        records = _trace(scenario)
        assert [record["step_no"] for record in records] == list(
            range(1, len(records) + 1)
        )
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in scenario_dir.iterdir()
            if path.is_file()
        )
        assert ":\\Users\\" not in combined
        assert ":/Users/" not in combined


def test_normal_example_completes_and_saves_a_consistent_plan() -> None:
    scenario_dir = EXAMPLES_ROOT / "normal"
    output = _json(scenario_dir / "output.json")
    plan = _json(scenario_dir / "plan.json")
    state = _json(scenario_dir / "state.json")
    record = _json(scenario_dir / "run-record.json")

    assert record["exit_code"] == 0
    assert output["status"] == state["status"] == "completed"
    assert output["state"] == state
    assert plan["profile"] == "standard"
    assert plan["estimated_total_cost"] == "12.00"
    assert output["saved_plan_path"].startswith("${RUNTIME_ROOT}")
    assert [item["action"] for item in _trace("normal")][-1] == "complete"


def test_missing_input_example_has_no_runtime_or_billable_tool_trace() -> None:
    scenario_dir = EXAMPLES_ROOT / "missing_input"
    output = _json(scenario_dir / "output.json")
    record = _json(scenario_dir / "run-record.json")

    assert record["exit_code"] == 2
    assert record["artifacts"]["runtime_created"] is False
    assert record["artifacts"]["trace_created"] is False
    assert output["status"] == "invalid_input"
    assert output["state"] is None
    assert output["saved_plan_path"] is None
    assert {error["code"] for error in output["errors"]} == {"missing_budget"}
    assert _trace("missing_input") == []
    assert not (scenario_dir / "state.json").exists()
    assert not (scenario_dir / "plan.json").exists()


def test_tool_failure_example_retries_and_preserves_successful_materials() -> None:
    output = _json(EXAMPLES_ROOT / "tool_failure" / "output.json")
    trace = _trace("tool_failure")
    script_records = [
        record for record in trace if record["action"] == "script_generation"
    ]
    material_step = _step(output, "material_search")

    assert output["status"] == "completed"
    assert [record["status"] for record in script_records] == ["failed", "succeeded"]
    assert "可重试工具错误" in script_records[1]["decision_reason"]
    assert material_step["status"] == "succeeded"
    assert material_step["result"]["data"]["matched_count"] == 3
    assert output["state"]["tool_call_count"] == 5


def test_budget_example_records_observed_replan_and_reestimation() -> None:
    scenario_dir = EXAMPLES_ROOT / "budget_exceeded"
    output = _json(scenario_dir / "output.json")
    plan = _json(scenario_dir / "plan.json")
    trace = _trace("budget_exceeded")
    actions = [record["action"] for record in trace]
    alternative = next(record for record in trace if record["action"] == "select_alternative")

    assert output["status"] == "completed"
    assert output["state"]["selected_plan_profile"] == "economy"
    assert output["state"]["estimated_total_cost"] == "8.00"
    assert output["state"]["budget_gap"] == "0"
    assert plan["profile"] == "economy"
    assert actions.count("cost_estimation") == 2
    assert "预算缺口 2.00" in alternative["decision_reason"]
    assert actions.index("select_alternative") < max(
        index for index, action in enumerate(actions) if action == "cost_estimation"
    )


def test_repeated_execution_has_identical_output_and_no_new_side_effects() -> None:
    scenario_dir = EXAMPLES_ROOT / "repeated_execution"
    first = _json(scenario_dir / "output-first.json")
    second = _json(scenario_dir / "output-second.json")
    record = _json(scenario_dir / "run-record.json")

    assert first == second
    assert first["status"] == "completed"
    assert record["artifacts"]["first_exit_code"] == 0
    assert record["artifacts"]["second_exit_code"] == 0
    assert record["artifacts"]["outputs_identical"] is True
    assert record["artifacts"]["state_plan_trace_unchanged"] is True
    assert record["artifacts"]["saved_plan_count"] == 1
    assert first["state"]["tool_call_count"] == 4


def test_two_traces_prove_different_observations_change_next_action() -> None:
    failure_trace = _trace("tool_failure")
    budget_trace = _trace("budget_exceeded")

    failure_decision = next(
        record
        for record in failure_trace
        if record["action"] == "script_generation" and "可重试" in record["decision_reason"]
    )
    budget_decision = next(
        record for record in budget_trace if record["action"] == "select_alternative"
    )
    assert failure_decision["status"] == "succeeded"
    assert "injected_failure" in failure_decision["decision_reason"]
    assert budget_decision["tool_parameters"] == {"plan_profile": "economy"}
    assert "预算缺口" in budget_decision["decision_reason"]


def test_acceptance_matrix_covers_every_stage_11_mandatory_scenario() -> None:
    matrix = _json(EXAMPLES_ROOT / "acceptance-matrix.json")
    expected_points = {
        "normal_completion_and_save",
        "missing_required_input_without_tool_call",
        "tool_failure_adjustment_and_result_retention",
        "budget_exceeded_dynamic_replan",
        "repeated_execution_idempotency",
        "different_observations_change_next_action",
    }
    assert expected_points <= set(matrix)
    referenced_scenarios = {
        scenario
        for point in expected_points
        for scenario in matrix[point]
    }
    assert referenced_scenarios == set(SCENARIOS)


@pytest.mark.parametrize("document", DOCUMENTS)
def test_all_five_final_documents_are_nonempty_and_reference_stage_11(document: str) -> None:
    content = (PROJECT_ROOT / document).read_text(encoding="utf-8")
    assert len(content.strip()) > 500
    assert "阶段 11" in content


def test_evaluation_document_references_every_existing_example_path() -> None:
    content = (PROJECT_ROOT / "docs/03-evaluation.md").read_text(encoding="utf-8")
    for scenario in SCENARIOS:
        reference = f"examples/{scenario}/"
        assert reference in content
        assert (PROJECT_ROOT / reference).is_dir()
    assert "examples/acceptance-matrix.json" in content
    retrospective = (PROJECT_ROOT / "docs/05-retrospective.md").read_text(
        encoding="utf-8"
    )
    for required_phrase in ("实际投入时间", "未完成内容", "已知限制"):
        assert required_phrase in retrospective
