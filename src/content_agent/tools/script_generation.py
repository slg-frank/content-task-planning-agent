"""Deterministic local script generation tool."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from content_agent.models import ToolResult
from content_agent.tools.common import (
    FailureInjector,
    injected_failure,
    load_tool_metrics,
    should_inject_failure,
    tool_failure,
)


TOOL_NAME = "script_generation"
ALLOWED_VARIANTS = frozenset({"standard", "economy"})


@dataclass(frozen=True, slots=True)
class ScriptGenerationRequest:
    topic: str
    materials: tuple[Mapping[str, Any], ...]
    variant: str = "standard"


def generate_script(
    request: ScriptGenerationRequest,
    *,
    data_dir: Path | None = None,
    failure_injector: FailureInjector | None = None,
) -> ToolResult:
    """Generate a reproducible script from valid material titles."""
    if should_inject_failure(TOOL_NAME, failure_injector):
        return injected_failure(TOOL_NAME)

    topic = request.topic.strip() if isinstance(request.topic, str) else ""
    if not topic or request.variant not in ALLOWED_VARIANTS:
        return tool_failure(
            TOOL_NAME,
            "invalid_request",
            "topic is required and variant must be 'standard' or 'economy'",
        )

    valid_materials: list[Mapping[str, Any]] = []
    for material in request.materials:
        if isinstance(material, Mapping) and str(material.get("title", "")).strip():
            valid_materials.append(material)
    ignored_count = len(request.materials) - len(valid_materials)
    if not valid_materials:
        return tool_failure(TOOL_NAME, "materials_required", "At least one valid material is required")

    try:
        cost, duration = load_tool_metrics(TOOL_NAME, data_dir=data_dir)
    except (OSError, JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return tool_failure(TOOL_NAME, "configuration_error", f"Unable to load tool metrics: {exc}")

    titles = [str(material["title"]).strip() for material in valid_materials]
    source_text = "、".join(titles)
    if request.variant == "standard":
        sections = [
            {"name": "开场", "text": f"从用户场景切入，介绍{topic}。"},
            {"name": "主体", "text": f"结合{source_text}说明核心价值与行动建议。"},
            {"name": "结尾", "text": "总结要点并邀请用户参与活动。"},
        ]
    else:
        sections = [
            {"name": "精简脚本", "text": f"围绕{topic}，参考{source_text}给出一个重点和一个行动号召。"}
        ]

    partial = ignored_count > 0
    warnings = [f"Ignored {ignored_count} invalid material(s)"] if partial else []
    return ToolResult(
        tool_name=TOOL_NAME,
        success=True,
        summary=f"Generated {request.variant} script with {len(sections)} section(s)",
        data={
            "topic": topic,
            "variant": request.variant,
            "sections": sections,
            "used_material_count": len(valid_materials),
            "ignored_material_count": ignored_count,
        },
        incurred_cost=cost,
        duration_seconds=duration,
        partial_success=partial,
        warnings=warnings,
    )
