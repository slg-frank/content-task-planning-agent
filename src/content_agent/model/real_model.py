"""OpenAI Responses API adapter producing validated agent actions."""

from __future__ import annotations

from collections.abc import Mapping
import json
from json import JSONDecodeError
import os
from typing import Any

from content_agent.model.base import (
    ALLOWED_TOOLS,
    ActionKind,
    ModelAction,
    ModelConfigurationError,
    ModelContext,
    ModelOutputError,
    ModelProviderError,
    parse_model_action,
)


MODEL_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [kind.value for kind in ActionKind],
        },
        "tool_name": {
            "type": "string",
            "enum": [*sorted(ALLOWED_TOOLS), "none"],
        },
        "parameters_json": {
            "type": "string",
            "description": "A JSON object containing parameters for the selected action.",
        },
        "decision_reason": {
            "type": "string",
            "description": "A concise, auditable reason without hidden chain-of-thought.",
        },
    },
    "required": ["action", "tool_name", "parameters_json", "decision_reason"],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTIONS = """You select exactly one next action for a content planning agent.
Use only the supplied action and tool names. Return concise decision_reason text that
describes the observable basis for the decision; do not provide private chain-of-thought.
Encode action parameters as a JSON object string in parameters_json. Use tool_name 'none'
for select_alternative, stop, and complete. Never claim that a tool ran; only select it.
"""


class OpenAIModelAdapter:
    """Real model adapter using strict Structured Outputs plus local validation."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        client: Any | None = None,
    ) -> None:
        if not model.strip():
            raise ModelConfigurationError("OPENAI_MODEL is required for real model mode")
        if not api_key.strip():
            raise ModelConfigurationError(
                "OPENAI_API_KEY is required for real model mode; use model_mode='mock' for offline runs"
            )
        self.model = model.strip()
        self._api_key = api_key
        self._client = client

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        client: Any | None = None,
    ) -> "OpenAIModelAdapter":
        environment = os.environ if environ is None else environ
        api_key = environment.get("OPENAI_API_KEY", "")
        model = environment.get("OPENAI_MODEL", "")
        if not api_key.strip():
            raise ModelConfigurationError(
                "OPENAI_API_KEY is not configured; set it outside the repository or use model_mode='mock'"
            )
        if not model.strip():
            raise ModelConfigurationError(
                "OPENAI_MODEL is not configured; choose an available model or use model_mode='mock'"
            )
        return cls(model=model, api_key=api_key, client=client)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ModelConfigurationError(
                    "The 'openai' package is required for real model mode"
                ) from exc
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def choose_action(self, context: ModelContext) -> ModelAction:
        client = self._get_client()
        try:
            response = client.responses.create(
                model=self.model,
                instructions=SYSTEM_INSTRUCTIONS,
                input=json.dumps(context.to_dict(), ensure_ascii=False, sort_keys=True),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "content_agent_action",
                        "strict": True,
                        "schema": MODEL_ACTION_SCHEMA,
                    }
                },
            )
            output_text = getattr(response, "output_text", None)
            if not isinstance(output_text, str) or not output_text.strip():
                raise ModelOutputError("real model returned no structured output text")
            provider_output = json.loads(output_text)
            if not isinstance(provider_output, Mapping):
                raise ModelOutputError("real model output must decode to an object")

            parameters_json = provider_output.get("parameters_json")
            if not isinstance(parameters_json, str):
                raise ModelOutputError("real model output is missing parameters_json")
            parameters = json.loads(parameters_json)
            if not isinstance(parameters, Mapping):
                raise ModelOutputError("parameters_json must decode to an object")

            tool_name = provider_output.get("tool_name")
            normalized = {
                "action": provider_output.get("action"),
                "tool_name": None if tool_name == "none" else tool_name,
                "parameters": dict(parameters),
                "decision_reason": provider_output.get("decision_reason"),
            }
            return parse_model_action(normalized)
        except ModelOutputError:
            raise
        except (JSONDecodeError, TypeError, ValueError) as exc:
            raise ModelOutputError(f"real model returned invalid structured JSON: {exc}") from exc
        except Exception as exc:
            raise ModelProviderError(f"real model request failed: {type(exc).__name__}") from exc
