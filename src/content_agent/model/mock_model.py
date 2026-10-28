"""Deterministic offline model adapter for tests and local evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from content_agent.model.base import (
    ModelAction,
    ModelContext,
    ModelOutputError,
    parse_model_action,
)


class MockModelAdapter:
    """Return a reproducible sequence of predeclared, validated actions."""

    def __init__(
        self,
        scripted_actions: Sequence[Mapping[str, Any] | ModelAction] | None = None,
        *,
        decision_policy: (
            Callable[[ModelContext, int], Mapping[str, Any] | ModelAction] | None
        ) = None,
    ) -> None:
        self._actions = tuple(scripted_actions or ())
        self._decision_policy = decision_policy
        self.call_count = 0

    def choose_action(self, context: ModelContext) -> ModelAction:
        if self._decision_policy is not None:
            raw_action = self._decision_policy(context, self.call_count)
        else:
            if self.call_count >= len(self._actions):
                raise ModelOutputError("mock action script is exhausted")
            raw_action = self._actions[self.call_count]
        self.call_count += 1
        if isinstance(raw_action, ModelAction):
            return parse_model_action(raw_action.to_dict())
        return parse_model_action(raw_action)
