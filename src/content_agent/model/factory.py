"""Factory for selecting offline or real model adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from content_agent.model.base import ModelAction, ModelAdapter, ModelConfigurationError
from content_agent.model.mock_model import MockModelAdapter
from content_agent.model.planning_mock import PlanningMockModelAdapter
from content_agent.model.real_model import OpenAIModelAdapter


def create_model_adapter(
    mode: str,
    *,
    scripted_actions: Sequence[Mapping[str, Any] | ModelAction] | None = None,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> ModelAdapter:
    """Create a requested adapter without silently changing model modes."""
    if mode == "mock":
        if scripted_actions is None:
            return PlanningMockModelAdapter()
        return MockModelAdapter(scripted_actions)
    if mode == "real":
        return OpenAIModelAdapter.from_environment(environ=environ, client=client)
    raise ModelConfigurationError("model mode must be either 'mock' or 'real'")
