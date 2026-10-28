"""Structured JSONL execution traces with recursive secret redaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
from json import JSONDecodeError
import os
from pathlib import Path
import re
from typing import Any

from content_agent.tools.common import DEFAULT_RUNTIME_ROOT


SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|token|secret|password|authorization|cookie|credential)",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"


def _environment_secret_values() -> tuple[str, ...]:
    values = {
        value
        for key, value in os.environ.items()
        if SENSITIVE_KEY.search(key) and isinstance(value, str) and len(value) >= 4
    }
    return tuple(sorted(values, key=len, reverse=True))


def _redact_string(value: str, secret_values: tuple[str, ...]) -> str:
    redacted = value
    for secret in secret_values:
        redacted = redacted.replace(secret, REDACTED)
    return redacted


def redact_sensitive(value: Any, *, secret_values: tuple[str, ...] | None = None) -> Any:
    """Return a JSON-safe copy with sensitive keys and env secret values removed."""
    secrets = secret_values if secret_values is not None else _environment_secret_values()
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            redacted[key_text] = (
                REDACTED
                if SENSITIVE_KEY.search(key_text)
                else redact_sensitive(item, secret_values=secrets)
            )
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item, secret_values=secrets) for item in value]
    if isinstance(value, str):
        return _redact_string(value, secrets)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value), secrets)


@dataclass(frozen=True, slots=True)
class TraceRecord:
    step_no: int
    timestamp: datetime
    action: str
    tool_parameters: dict[str, Any]
    result_summary: str
    status: str
    incurred_cost: Decimal
    estimated_total_cost: Decimal
    elapsed_duration_seconds: float
    next_action: str
    decision_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_no": self.step_no,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "tool_parameters": dict(self.tool_parameters),
            "result_summary": self.result_summary,
            "status": self.status,
            "incurred_cost": str(self.incurred_cost),
            "estimated_total_cost": str(self.estimated_total_cost),
            "elapsed_duration_seconds": self.elapsed_duration_seconds,
            "next_action": self.next_action,
            "decision_reason": self.decision_reason,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TraceRecord":
        timestamp = datetime.fromisoformat(str(raw["timestamp"]))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("trace timestamp must be timezone-aware")
        return cls(
            step_no=int(raw["step_no"]),
            timestamp=timestamp,
            action=str(raw["action"]),
            tool_parameters=dict(raw.get("tool_parameters", {})),
            result_summary=str(raw["result_summary"]),
            status=str(raw["status"]),
            incurred_cost=Decimal(str(raw["incurred_cost"])),
            estimated_total_cost=Decimal(str(raw["estimated_total_cost"])),
            elapsed_duration_seconds=float(raw["elapsed_duration_seconds"]),
            next_action=str(raw["next_action"]),
            decision_reason=str(raw["decision_reason"]),
        )


class TraceRecorder:
    """Append and reload one ordered JSONL trace per task."""

    def __init__(self, task_id: str, root: Path | None = None) -> None:
        if not SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError("task_id is not safe for trace persistence")
        self.task_id = task_id
        self.root = (root or (DEFAULT_RUNTIME_ROOT / "traces")).resolve()
        self.path = self.root / f"{task_id}.trace.jsonl"

    def append(
        self,
        *,
        action: str,
        tool_parameters: Mapping[str, Any],
        result_summary: str,
        status: str | Enum,
        incurred_cost: Decimal,
        estimated_total_cost: Decimal,
        elapsed_duration_seconds: float,
        next_action: str,
        decision_reason: str,
        timestamp: datetime | None = None,
    ) -> TraceRecord:
        existing = self.read_all()
        recorded_at = timestamp or datetime.now(timezone.utc)
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("trace timestamp must be timezone-aware")
        if incurred_cost < 0 or estimated_total_cost < 0 or elapsed_duration_seconds < 0:
            raise ValueError("trace totals must be non-negative")

        secrets = _environment_secret_values()
        record = TraceRecord(
            step_no=len(existing) + 1,
            timestamp=recorded_at,
            action=_redact_string(str(action), secrets),
            tool_parameters=redact_sensitive(dict(tool_parameters), secret_values=secrets),
            result_summary=_redact_string(str(result_summary), secrets),
            status=str(status.value if isinstance(status, Enum) else status),
            incurred_cost=incurred_cost,
            estimated_total_cost=estimated_total_cost,
            elapsed_duration_seconds=elapsed_duration_seconds,
            next_action=_redact_string(str(next_action), secrets),
            decision_reason=_redact_string(str(decision_reason), secrets)[:500],
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))
            stream.write("\n")
        return record

    def read_all(self) -> list[TraceRecord]:
        if not self.path.exists():
            return []
        records: list[TraceRecord] = []
        try:
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if not line.strip():
                    continue
                raw = json.loads(line)
                if not isinstance(raw, Mapping):
                    raise ValueError(f"trace line {line_number} must contain an object")
                records.append(TraceRecord.from_dict(raw))
        except (OSError, JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"unable to read trace for {self.task_id}: {exc}") from exc

        expected_steps = list(range(1, len(records) + 1))
        actual_steps = [record.step_no for record in records]
        if actual_steps != expected_steps:
            raise ValueError("trace step numbers are not contiguous and ordered")
        return records
