"""Deterministic plan cost estimation tool."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from json import JSONDecodeError
from pathlib import Path

from content_agent.models import ToolResult
from content_agent.tools.common import (
    FailureInjector,
    injected_failure,
    load_json_table,
    load_tool_metrics,
    should_inject_failure,
    tool_failure,
)


TOOL_NAME = "cost_estimation"


@dataclass(frozen=True, slots=True)
class CostEstimationRequest:
    plan_profile: str


def estimate_cost(
    request: CostEstimationRequest,
    *,
    data_dir: Path | None = None,
    failure_injector: FailureInjector | None = None,
) -> ToolResult:
    """Calculate a profile total from the synthetic cost table."""
    if should_inject_failure(TOOL_NAME, failure_injector):
        return injected_failure(TOOL_NAME)

    if not isinstance(request.plan_profile, str) or not request.plan_profile.strip():
        return tool_failure(TOOL_NAME, "invalid_request", "plan_profile is required")
    profile_name = request.plan_profile.strip()

    try:
        cost_table = load_json_table("tool_costs.json", data_dir=data_dir)
        profile = cost_table["plan_profiles"][profile_name]
        if not isinstance(profile, dict) or not profile:
            raise ValueError("plan profile must contain line items")
        line_items = {name: Decimal(str(value)) for name, value in profile.items()}
        if any(not value.is_finite() or value < 0 for value in line_items.values()):
            raise ValueError("plan costs must be finite and non-negative")
        estimated_total = sum(line_items.values(), Decimal("0"))
        duration_table = load_json_table("tool_durations.json", data_dir=data_dir)
        estimated_duration = float(
            duration_table["plan_profiles_duration_seconds"][profile_name]
        )
        if estimated_duration < 0:
            raise ValueError("plan duration must be non-negative")
        invocation_cost, duration = load_tool_metrics(TOOL_NAME, data_dir=data_dir)
    except KeyError:
        return tool_failure(
            TOOL_NAME,
            "unknown_plan_profile",
            f"Unknown plan profile: {profile_name}",
        )
    except (OSError, JSONDecodeError, InvalidOperation, TypeError, ValueError) as exc:
        return tool_failure(TOOL_NAME, "configuration_error", f"Unable to estimate cost: {exc}")

    return ToolResult(
        tool_name=TOOL_NAME,
        success=True,
        summary=f"Estimated {profile_name} plan at {estimated_total} units",
        data={
            "plan_profile": profile_name,
            "line_items": {name: str(value) for name, value in line_items.items()},
            "estimated_total_cost": str(estimated_total),
            "estimated_total_duration_seconds": estimated_duration,
        },
        incurred_cost=invocation_cost,
        duration_seconds=duration,
    )
