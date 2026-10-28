"""Shared configuration and deterministic failure support for local tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from content_agent.models import ToolResult


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime"


@dataclass(slots=True)
class FailureInjector:
    """Fail configured tools a deterministic number of times.

    A single injector instance is intended to be shared across calls in one
    execution so a rule such as ``{"script_generation": 1}`` fails only the
    first matching call.
    """

    rules: Mapping[str, int | bool] = field(default_factory=dict)
    _consumed: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        normalized: dict[str, int] = {}
        for tool_name, raw_count in self.rules.items():
            count = int(raw_count)
            if count < 0:
                raise ValueError("failure counts must be zero or greater")
            normalized[str(tool_name)] = count
        self.rules = normalized

    def should_fail(self, tool_name: str) -> bool:
        allowed_failures = int(self.rules.get(tool_name, 0))
        consumed = self._consumed.get(tool_name, 0)
        if consumed >= allowed_failures:
            return False
        self._consumed[tool_name] = consumed + 1
        return True


def injected_failure(tool_name: str) -> ToolResult:
    """Build the standard result for an injected, non-billable failure."""
    return ToolResult(
        tool_name=tool_name,
        success=False,
        summary=f"Injected failure for {tool_name}",
        error_code="injected_failure",
        retryable=True,
    )


def tool_failure(
    tool_name: str,
    error_code: str,
    summary: str,
    *,
    retryable: bool = False,
) -> ToolResult:
    """Build a non-billable structured failure result."""
    return ToolResult(
        tool_name=tool_name,
        success=False,
        summary=summary,
        error_code=error_code,
        retryable=retryable,
    )


def load_json_table(filename: str, *, data_dir: Path | None = None) -> Any:
    """Load one UTF-8 JSON table from the configured synthetic data folder."""
    path = (data_dir or DEFAULT_DATA_DIR) / filename
    return json.loads(path.read_text(encoding="utf-8"))


def load_tool_metrics(
    tool_name: str,
    *,
    data_dir: Path | None = None,
) -> tuple[Decimal, float]:
    """Return invocation cost and simulated duration for one tool."""
    costs = load_json_table("tool_costs.json", data_dir=data_dir)
    durations = load_json_table("tool_durations.json", data_dir=data_dir)
    cost = Decimal(str(costs["tool_invocation_costs"][tool_name]))
    duration = float(durations["tool_durations_seconds"][tool_name])
    if not cost.is_finite() or cost < 0 or duration < 0:
        raise ValueError(f"invalid metrics for {tool_name}")
    return cost, duration


def should_inject_failure(
    tool_name: str,
    failure_injector: FailureInjector | None,
) -> bool:
    return failure_injector is not None and failure_injector.should_fail(tool_name)
