"""Generate stage-11 acceptance artifacts by executing the real CLI.

The generated JSON preserves business output exactly. Temporary absolute paths
are replaced with ``${RUNTIME_ROOT}`` so committed examples stay portable.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = Path(__file__).resolve().parent
FUTURE_DEADLINE = "2099-12-31T23:59:59+08:00"

SCENARIOS: dict[str, dict[str, Any]] = {
    "normal": {
        "topic": "秋季新品内容活动",
        "budget": "20",
        "deadline": FUTURE_DEADLINE,
        "task_id": "acceptance-normal",
        "model_mode": "mock",
    },
    "missing_input": {
        "topic": "秋季新品内容活动",
        "deadline": FUTURE_DEADLINE,
        "task_id": "acceptance-missing-budget",
        "model_mode": "mock",
    },
    "tool_failure": {
        "topic": "秋季新品内容活动",
        "budget": "20",
        "deadline": FUTURE_DEADLINE,
        "task_id": "acceptance-tool-failure",
        "model_mode": "mock",
        "failure_injection": {"script_generation": 1},
    },
    "budget_exceeded": {
        "topic": "秋季新品内容活动",
        "budget": "10",
        "deadline": FUTURE_DEADLINE,
        "task_id": "acceptance-budget-replan",
        "model_mode": "mock",
    },
    "repeated_execution": {
        "topic": "秋季新品内容活动",
        "budget": "20",
        "deadline": FUTURE_DEADLINE,
        "task_id": "acceptance-repeated",
        "model_mode": "mock",
    },
}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _normalize(value: Any, runtime_root: Path) -> Any:
    runtime_texts = {
        str(runtime_root),
        runtime_root.as_posix(),
        str(runtime_root.resolve()),
        runtime_root.resolve().as_posix(),
    }
    if isinstance(value, dict):
        return {key: _normalize(item, runtime_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, runtime_root) for item in value]
    if isinstance(value, str):
        normalized = value
        for runtime_text in sorted(runtime_texts, key=len, reverse=True):
            normalized = normalized.replace(runtime_text, "${RUNTIME_ROOT}")
        return normalized
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_cli(input_path: Path, runtime_root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    source_path = str(PROJECT_ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else os.pathsep.join((source_path, existing_pythonpath))
    )
    command = [
        sys.executable,
        "-m",
        "content_agent.cli",
        "run",
        "--input",
        str(input_path),
        "--runtime-dir",
        str(runtime_root),
    ]
    process = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    try:
        output = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"CLI emitted invalid JSON for {input_path.name}: {process.stdout!r}"
        ) from exc
    return {
        "command": command,
        "exit_code": process.returncode,
        "stderr": process.stderr.strip(),
        "output": output,
    }


def _copy_runtime_artifacts(
    scenario_dir: Path,
    input_payload: dict[str, Any],
    runtime_root: Path,
    output: dict[str, Any],
) -> dict[str, Any]:
    task_id = input_payload["task_id"]
    trace_source = runtime_root / "traces" / f"{task_id}.trace.jsonl"
    state_source = runtime_root / "state" / f"{task_id}.json"
    plan_source = runtime_root / "plans" / f"{task_id}.json"

    trace_target = scenario_dir / "trace.jsonl"
    trace_created = trace_source.is_file()
    trace_target.write_text(
        trace_source.read_text(encoding="utf-8") if trace_created else "",
        encoding="utf-8",
    )

    copied: dict[str, Any] = {
        "trace_created": trace_created,
        "runtime_created": runtime_root.exists(),
    }
    if state_source.is_file():
        state_payload = json.loads(state_source.read_text(encoding="utf-8"))
        _write_json(scenario_dir / "state.json", _normalize(state_payload, runtime_root))
        copied["state_sha256"] = _sha256(state_source)
    if plan_source.is_file():
        plan_payload = json.loads(plan_source.read_text(encoding="utf-8"))
        _write_json(scenario_dir / "plan.json", _normalize(plan_payload, runtime_root))
        copied["plan_sha256"] = _sha256(plan_source)
    if trace_created:
        copied["trace_sha256"] = _sha256(trace_source)
    copied["output_status"] = output.get("status")
    return copied


def _record_for_run(
    scenario: str,
    run: dict[str, Any],
    runtime_root: Path,
    artifact_facts: dict[str, Any],
) -> dict[str, Any]:
    command = [
        "python",
        "-m",
        "content_agent.cli",
        "run",
        "--input",
        "input.json",
        "--runtime-dir",
        "${RUNTIME_ROOT}",
    ]
    return {
        "schema_version": 1,
        "scenario": scenario,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "actual_cli_execution": True,
        "command": command,
        "exit_code": run["exit_code"],
        "stderr": _normalize(run["stderr"], runtime_root),
        "path_normalization": {
            "applied": True,
            "replacement": "${RUNTIME_ROOT}",
            "scope": "temporary input and runtime paths only",
        },
        "artifacts": artifact_facts,
    }


def _generate_single(
    scenario: str,
    input_payload: dict[str, Any],
    scratch_root: Path,
) -> None:
    scenario_dir = EXAMPLES_ROOT / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    _write_json(scenario_dir / "input.json", input_payload)

    scratch_scenario = scratch_root / scenario
    scratch_scenario.mkdir(parents=True, exist_ok=True)
    input_path = scratch_scenario / "input.json"
    runtime_root = scratch_scenario / "runtime"
    _write_json(input_path, input_payload)

    run = _run_cli(input_path, runtime_root)
    normalized_output = _normalize(run["output"], runtime_root)
    _write_json(scenario_dir / "output.json", normalized_output)
    facts = _copy_runtime_artifacts(
        scenario_dir,
        input_payload,
        runtime_root,
        run["output"],
    )
    _write_json(
        scenario_dir / "run-record.json",
        _record_for_run(scenario, run, runtime_root, facts),
    )


def _generate_repeated(input_payload: dict[str, Any], scratch_root: Path) -> None:
    scenario = "repeated_execution"
    scenario_dir = EXAMPLES_ROOT / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    _write_json(scenario_dir / "input.json", input_payload)

    scratch_scenario = scratch_root / scenario
    scratch_scenario.mkdir(parents=True, exist_ok=True)
    input_path = scratch_scenario / "input.json"
    runtime_root = scratch_scenario / "runtime"
    _write_json(input_path, input_payload)

    first = _run_cli(input_path, runtime_root)
    task_id = input_payload["task_id"]
    tracked_paths = (
        runtime_root / "state" / f"{task_id}.json",
        runtime_root / "plans" / f"{task_id}.json",
        runtime_root / "traces" / f"{task_id}.trace.jsonl",
    )
    before = {
        path.name: (path.stat().st_mtime_ns, _sha256(path)) for path in tracked_paths
    }
    second = _run_cli(input_path, runtime_root)
    after = {
        path.name: (path.stat().st_mtime_ns, _sha256(path)) for path in tracked_paths
    }

    _write_json(
        scenario_dir / "output-first.json",
        _normalize(first["output"], runtime_root),
    )
    _write_json(
        scenario_dir / "output-second.json",
        _normalize(second["output"], runtime_root),
    )
    facts = _copy_runtime_artifacts(
        scenario_dir,
        input_payload,
        runtime_root,
        second["output"],
    )
    facts.update(
        {
            "outputs_identical": first["output"] == second["output"],
            "state_plan_trace_unchanged": before == after,
            "saved_plan_count": len(list((runtime_root / "plans").glob("*.json"))),
            "first_exit_code": first["exit_code"],
            "second_exit_code": second["exit_code"],
        }
    )
    record = _record_for_run(scenario, second, runtime_root, facts)
    record["runs"] = [
        {
            "ordinal": 1,
            "exit_code": first["exit_code"],
            "stderr": _normalize(first["stderr"], runtime_root),
        },
        {
            "ordinal": 2,
            "exit_code": second["exit_code"],
            "stderr": _normalize(second["stderr"], runtime_root),
        },
    ]
    _write_json(scenario_dir / "run-record.json", record)


def _write_acceptance_matrix() -> None:
    matrix = {
        "schema_version": 1,
        "normal_completion_and_save": ["normal"],
        "missing_required_input_without_tool_call": ["missing_input"],
        "tool_failure_adjustment_and_result_retention": ["tool_failure"],
        "budget_exceeded_dynamic_replan": ["budget_exceeded"],
        "repeated_execution_idempotency": ["repeated_execution"],
        "different_observations_change_next_action": [
            "tool_failure",
            "budget_exceeded",
        ],
    }
    _write_json(EXAMPLES_ROOT / "acceptance-matrix.json", matrix)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="content-agent-stage11-") as temporary:
        scratch_root = Path(temporary)
        for scenario, input_payload in SCENARIOS.items():
            if scenario == "repeated_execution":
                _generate_repeated(input_payload, scratch_root)
            else:
                _generate_single(scenario, input_payload, scratch_root)
    _write_acceptance_matrix()
    print("Generated five stage-11 acceptance scenarios from actual CLI executions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
