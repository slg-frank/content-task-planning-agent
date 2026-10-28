"""JSON plan persistence constrained to an approved runtime directory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from json import JSONDecodeError
from pathlib import Path
import re
from typing import Any

from content_agent.models import ToolResult
from content_agent.tools.common import (
    DEFAULT_RUNTIME_ROOT,
    FailureInjector,
    injected_failure,
    load_tool_metrics,
    should_inject_failure,
    tool_failure,
)


TOOL_NAME = "plan_save"
SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class PlanSaveRequest:
    task_id: str
    plan: Mapping[str, Any]


def save_plan(
    request: PlanSaveRequest,
    *,
    output_dir: Path | None = None,
    allowed_output_root: Path | None = None,
    data_dir: Path | None = None,
    failure_injector: FailureInjector | None = None,
) -> ToolResult:
    """Save a plan only below the configured runtime output root."""
    if should_inject_failure(TOOL_NAME, failure_injector):
        return injected_failure(TOOL_NAME)

    task_id = request.task_id.strip() if isinstance(request.task_id, str) else ""
    if not SAFE_TASK_ID.fullmatch(task_id):
        return tool_failure(TOOL_NAME, "invalid_task_id", "task_id is not safe for a file name")
    if not isinstance(request.plan, Mapping):
        return tool_failure(TOOL_NAME, "invalid_plan", "plan must be an object")

    allowed_root = (allowed_output_root or DEFAULT_RUNTIME_ROOT).resolve()
    target_dir = (output_dir or (allowed_root / "plans")).resolve()
    if not target_dir.is_relative_to(allowed_root):
        return tool_failure(
            TOOL_NAME,
            "invalid_output_path",
            "plan output directory must be inside the approved runtime root",
        )

    try:
        cost, duration = load_tool_metrics(TOOL_NAME, data_dir=data_dir)
        serialized_plan = json.dumps(
            dict(request.plan),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{task_id}.json"
        target_path.write_text(f"{serialized_plan}\n", encoding="utf-8")
    except (OSError, JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return tool_failure(TOOL_NAME, "save_failed", f"Unable to save plan: {exc}")

    return ToolResult(
        tool_name=TOOL_NAME,
        success=True,
        summary=f"Saved plan for task {task_id}",
        data={"task_id": task_id, "saved_path": str(target_path)},
        incurred_cost=cost,
        duration_seconds=duration,
    )
