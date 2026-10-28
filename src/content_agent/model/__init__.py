"""Model adapter interfaces, offline Mock, and OpenAI implementation."""

from content_agent.model.base import (
    ActionKind,
    ModelAction,
    ModelAdapter,
    ModelConfigurationError,
    ModelContext,
    ModelError,
    ModelOutputError,
    ModelProviderError,
    parse_model_action,
)
from content_agent.model.factory import create_model_adapter
from content_agent.model.mock_model import MockModelAdapter
from content_agent.model.planning_mock import PlanningMockModelAdapter
from content_agent.model.real_model import OpenAIModelAdapter

__all__ = [
    "ActionKind",
    "MockModelAdapter",
    "ModelAction",
    "ModelAdapter",
    "ModelConfigurationError",
    "ModelContext",
    "ModelError",
    "ModelOutputError",
    "ModelProviderError",
    "OpenAIModelAdapter",
    "PlanningMockModelAdapter",
    "create_model_adapter",
    "parse_model_action",
]
