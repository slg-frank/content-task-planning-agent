"""Synthetic material search tool."""

from __future__ import annotations

from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from content_agent.models import ToolResult
from content_agent.tools.common import (
    FailureInjector,
    injected_failure,
    load_json_table,
    load_tool_metrics,
    should_inject_failure,
    tool_failure,
)


TOOL_NAME = "material_search"


@dataclass(frozen=True, slots=True)
class MaterialSearchRequest:
    topic: str
    limit: int = 3
    minimum_results: int = 1


def search_materials(
    request: MaterialSearchRequest,
    *,
    data_dir: Path | None = None,
    failure_injector: FailureInjector | None = None,
) -> ToolResult:
    """Return deterministic synthetic materials relevant to a topic."""
    if should_inject_failure(TOOL_NAME, failure_injector):
        return injected_failure(TOOL_NAME)

    topic = request.topic.strip() if isinstance(request.topic, str) else ""
    if not topic or not isinstance(request.limit, int) or not 1 <= request.limit <= 20:
        return tool_failure(TOOL_NAME, "invalid_request", "topic is required and limit must be 1-20")
    if not isinstance(request.minimum_results, int) or not 1 <= request.minimum_results <= request.limit:
        return tool_failure(
            TOOL_NAME,
            "invalid_request",
            "minimum_results must be between 1 and limit",
        )

    try:
        materials = load_json_table("materials.json", data_dir=data_dir)
        cost, duration = load_tool_metrics(TOOL_NAME, data_dir=data_dir)
        if not isinstance(materials, list):
            raise ValueError("materials table must contain a list")
    except (OSError, JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return tool_failure(TOOL_NAME, "configuration_error", f"Unable to load material data: {exc}")

    normalized_topic = topic.casefold()
    matched: list[dict[str, Any]] = []
    for material in materials:
        if not isinstance(material, dict):
            continue
        keywords = material.get("keywords", [])
        if not isinstance(keywords, list):
            continue
        if any(str(keyword).casefold() in normalized_topic for keyword in keywords):
            matched.append(dict(material))

    matched.sort(key=lambda item: str(item.get("id", "")))
    selected = matched[: request.limit]
    if not selected:
        return tool_failure(TOOL_NAME, "no_materials_found", f"No synthetic materials matched topic: {topic}")

    partial = len(selected) < request.minimum_results
    warnings = []
    if partial:
        warnings.append(
            f"Requested at least {request.minimum_results} materials but found {len(selected)}"
        )

    return ToolResult(
        tool_name=TOOL_NAME,
        success=True,
        summary=f"Found {len(selected)} synthetic material(s)",
        data={
            "topic": topic,
            "materials": selected,
            "matched_count": len(selected),
            "missing_count": max(0, request.minimum_results - len(selected)),
        },
        incurred_cost=cost,
        duration_seconds=duration,
        partial_success=partial,
        warnings=warnings,
    )
