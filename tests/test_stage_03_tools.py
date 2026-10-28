"""Stage 03 tests for deterministic local business tools."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pytest

from content_agent.tools import (
    CostEstimationRequest,
    FailureInjector,
    MaterialSearchRequest,
    PlanSaveRequest,
    ScriptGenerationRequest,
    estimate_cost,
    generate_script,
    save_plan,
    search_materials,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


def _search_request() -> MaterialSearchRequest:
    return MaterialSearchRequest(topic="秋季新品内容活动", limit=3, minimum_results=2)


def _materials() -> tuple[dict[str, str], ...]:
    return (
        {"id": "mat-001", "title": "秋季新品发布内容框架"},
        {"id": "mat-003", "title": "新品卖点表达模板"},
    )


def _script_request() -> ScriptGenerationRequest:
    return ScriptGenerationRequest(
        topic="秋季新品内容活动",
        materials=_materials(),
        variant="standard",
    )


def test_all_four_tools_succeed_with_normal_input(tmp_path: Path) -> None:
    search_result = search_materials(_search_request())
    script_result = generate_script(_script_request())
    estimate_result = estimate_cost(CostEstimationRequest(plan_profile="standard"))
    save_result = save_plan(
        PlanSaveRequest(task_id="campaign-001", plan={"topic": "秋季新品", "steps": []}),
        output_dir=tmp_path / "plans",
        allowed_output_root=tmp_path,
    )

    assert search_result.success and search_result.error_code is None
    assert script_result.success and script_result.error_code is None
    assert estimate_result.success and estimate_result.error_code is None
    assert save_result.success and save_result.error_code is None


def test_tool_costs_and_durations_match_data_tables(tmp_path: Path) -> None:
    costs = json.loads((DATA_DIR / "tool_costs.json").read_text(encoding="utf-8"))
    durations = json.loads((DATA_DIR / "tool_durations.json").read_text(encoding="utf-8"))
    results = {
        "material_search": search_materials(_search_request()),
        "script_generation": generate_script(_script_request()),
        "cost_estimation": estimate_cost(CostEstimationRequest(plan_profile="standard")),
        "plan_save": save_plan(
            PlanSaveRequest(task_id="campaign-001", plan={"topic": "秋季新品"}),
            output_dir=tmp_path / "plans",
            allowed_output_root=tmp_path,
        ),
    }

    for tool_name, result in results.items():
        assert result.success
        assert result.incurred_cost == Decimal(costs["tool_invocation_costs"][tool_name])
        assert result.duration_seconds == float(durations["tool_durations_seconds"][tool_name])


def test_same_inputs_produce_reproducible_results(tmp_path: Path) -> None:
    first_search = search_materials(_search_request()).to_dict()
    second_search = search_materials(_search_request()).to_dict()
    first_script = generate_script(_script_request()).to_dict()
    second_script = generate_script(_script_request()).to_dict()
    first_estimate = estimate_cost(CostEstimationRequest("standard")).to_dict()
    second_estimate = estimate_cost(CostEstimationRequest("standard")).to_dict()
    save_request = PlanSaveRequest(task_id="repeatable-001", plan={"b": 2, "a": 1})
    first_save = save_plan(
        save_request,
        output_dir=tmp_path / "plans",
        allowed_output_root=tmp_path,
    ).to_dict()
    second_save = save_plan(
        save_request,
        output_dir=tmp_path / "plans",
        allowed_output_root=tmp_path,
    ).to_dict()

    assert first_search == second_search
    assert first_script == second_script
    assert first_estimate == second_estimate
    assert first_save == second_save


@pytest.mark.parametrize(
    ("profile", "expected_total"),
    [("standard", "12.00"), ("economy", "8.00")],
)
def test_cost_estimation_reference_profiles(profile: str, expected_total: str) -> None:
    result = estimate_cost(CostEstimationRequest(plan_profile=profile))

    assert result.success
    assert result.data["estimated_total_cost"] == expected_total
    line_item_total = sum(
        (Decimal(value) for value in result.data["line_items"].values()),
        Decimal("0"),
    )
    assert line_item_total == Decimal(expected_total)


def test_material_search_partial_success_reports_missing_results() -> None:
    result = search_materials(
        MaterialSearchRequest(topic="秋季", limit=4, minimum_results=3)
    )

    assert result.success
    assert result.partial_success
    assert result.error_code is None
    assert result.data["matched_count"] == 2
    assert result.data["missing_count"] == 1
    assert result.warnings


def test_script_generation_partial_success_reports_ignored_materials() -> None:
    result = generate_script(
        ScriptGenerationRequest(
            topic="秋季新品内容活动",
            materials=(_materials()[0], {"id": "broken-without-title"}),
            variant="economy",
        )
    )

    assert result.success
    assert result.partial_success
    assert result.data["used_material_count"] == 1
    assert result.data["ignored_material_count"] == 1
    assert result.warnings == ["Ignored 1 invalid material(s)"]


@pytest.mark.parametrize(
    "tool_name",
    ["material_search", "script_generation", "cost_estimation", "plan_save"],
)
def test_each_tool_supports_one_deterministic_injected_failure(
    tool_name: str,
    tmp_path: Path,
) -> None:
    injector = FailureInjector({tool_name: 1})

    if tool_name == "material_search":
        first = search_materials(_search_request(), failure_injector=injector)
        second = search_materials(_search_request(), failure_injector=injector)
    elif tool_name == "script_generation":
        first = generate_script(_script_request(), failure_injector=injector)
        second = generate_script(_script_request(), failure_injector=injector)
    elif tool_name == "cost_estimation":
        request = CostEstimationRequest(plan_profile="standard")
        first = estimate_cost(request, failure_injector=injector)
        second = estimate_cost(request, failure_injector=injector)
    else:
        request = PlanSaveRequest(task_id="failure-test", plan={"topic": "测试"})
        first = save_plan(
            request,
            output_dir=tmp_path / "plans",
            allowed_output_root=tmp_path,
            failure_injector=injector,
        )
        second = save_plan(
            request,
            output_dir=tmp_path / "plans",
            allowed_output_root=tmp_path,
            failure_injector=injector,
        )

    assert not first.success
    assert not first.partial_success
    assert first.error_code == "injected_failure"
    assert first.summary
    assert first.incurred_cost == Decimal("0")
    assert second.success


def test_saved_plan_can_be_read_and_matches_input(tmp_path: Path) -> None:
    plan = {
        "topic": "秋季新品内容活动",
        "profile": "economy",
        "estimated_total_cost": "8.00",
        "steps": [{"name": "素材检索", "status": "succeeded"}],
    }
    result = save_plan(
        PlanSaveRequest(task_id="campaign-save-001", plan=plan),
        output_dir=tmp_path / "plans",
        allowed_output_root=tmp_path,
    )

    assert result.success
    saved_path = Path(result.data["saved_path"])
    assert saved_path.is_file()
    assert json.loads(saved_path.read_text(encoding="utf-8")) == plan


def test_plan_save_rejects_output_outside_approved_root(tmp_path: Path) -> None:
    allowed_root = tmp_path / "approved"
    outside = tmp_path / "outside"

    result = save_plan(
        PlanSaveRequest(task_id="campaign-001", plan={"topic": "测试"}),
        output_dir=outside,
        allowed_output_root=allowed_root,
    )

    assert not result.success
    assert result.error_code == "invalid_output_path"
    assert not outside.exists()


@pytest.mark.parametrize(
    ("call", "expected_error"),
    [
        (lambda: search_materials(MaterialSearchRequest(topic="", limit=3)), "invalid_request"),
        (
            lambda: generate_script(
                ScriptGenerationRequest(topic="测试", materials=(), variant="standard")
            ),
            "materials_required",
        ),
        (
            lambda: estimate_cost(CostEstimationRequest(plan_profile="unknown")),
            "unknown_plan_profile",
        ),
    ],
)
def test_business_failures_are_structured(call: object, expected_error: str) -> None:
    result = call()  # type: ignore[operator]

    assert not result.success
    assert not result.partial_success
    assert result.error_code == expected_error
    assert result.summary
    assert result.incurred_cost == Decimal("0")
