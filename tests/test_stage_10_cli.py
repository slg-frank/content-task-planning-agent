"""Stage 10 tests for the complete CLI and end-to-end output contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUTURE_DEADLINE = "2099-12-31T23:59:59+08:00"


def _run_cli(
    *arguments: str,
    unset_environment: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in unset_environment:
        environment.pop(name, None)
    environment["PYTHONUTF8"] = "1"
    existing_pythonpath = environment.get("PYTHONPATH")
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else os.pathsep.join((source_path, existing_pythonpath))
    )
    return subprocess.run(
        [sys.executable, "-m", "content_agent.cli", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _payload(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    parsed = json.loads(process.stdout)
    assert isinstance(parsed, dict)
    return parsed


def _normal_arguments(runtime_root: Path, task_id: str, budget: str = "20") -> tuple[str, ...]:
    return (
        "run",
        "--topic",
        "秋季新品内容活动",
        "--budget",
        budget,
        "--deadline",
        FUTURE_DEADLINE,
        "--task-id",
        task_id,
        "--runtime-dir",
        str(runtime_root),
    )


def _read_trace(payload: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(payload["trace_path"])
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_cli_normal_input_completes_with_json_and_human_summary(tmp_path: Path) -> None:
    process = _run_cli(*_normal_arguments(tmp_path / "runtime", "cli-normal"))

    assert process.returncode == 0
    payload = _payload(process)
    assert payload["status"] == "completed"
    assert payload["task_id"] == "cli-normal"
    assert payload["errors"] == []
    assert payload["state"]["status"] == "completed"
    assert payload["state"]["selected_plan_profile"] == "standard"
    assert payload["state"]["tool_call_count"] == 4
    assert Path(payload["saved_plan_path"]).is_file()
    assert Path(payload["trace_path"]).is_file()
    assert "任务 cli-normal 已完成" in process.stderr


@pytest.mark.parametrize(
    ("omitted_flag", "expected_code"),
    (("--budget", "missing_budget"), ("--deadline", "missing_deadline")),
)
def test_cli_missing_required_constraint_is_explicit_input_error(
    tmp_path: Path,
    omitted_flag: str,
    expected_code: str,
) -> None:
    arguments = list(_normal_arguments(tmp_path / "runtime", f"missing-{expected_code}"))
    flag_index = arguments.index(omitted_flag)
    del arguments[flag_index : flag_index + 2]

    process = _run_cli(*arguments)

    assert process.returncode == 2
    payload = _payload(process)
    assert payload["status"] == "invalid_input"
    assert expected_code in {error["code"] for error in payload["errors"]}
    assert not (tmp_path / "runtime").exists()


def test_cli_budget_excess_triggers_replan_and_second_estimate(tmp_path: Path) -> None:
    process = _run_cli(
        *_normal_arguments(tmp_path / "runtime", "cli-replan", budget="10")
    )

    assert process.returncode == 0
    payload = _payload(process)
    state = payload["state"]
    assert state["selected_plan_profile"] == "economy"
    assert state["estimated_plan_profile"] == "economy"
    assert state["estimated_total_cost"] == "8.00"
    assert state["budget_gap"] == "0"
    trace = _read_trace(payload)
    assert [record["action"] for record in trace].count("cost_estimation") == 2
    assert "select_alternative" in [record["action"] for record in trace]


def test_cli_failure_injection_retries_from_observation(tmp_path: Path) -> None:
    process = _run_cli(
        *_normal_arguments(tmp_path / "runtime", "cli-retry"),
        "--failure-injection",
        '{"script_generation": 1}',
    )

    assert process.returncode == 0
    payload = _payload(process)
    assert payload["status"] == "completed"
    assert payload["state"]["tool_call_count"] == 5
    script_records = [
        record
        for record in _read_trace(payload)
        if record["action"] == "script_generation"
    ]
    assert [record["status"] for record in script_records] == ["failed", "succeeded"]
    assert "可重试" in script_records[1]["decision_reason"]


def test_cli_repeated_task_id_is_side_effect_free(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    arguments = _normal_arguments(runtime_root, "cli-idempotent")

    first = _run_cli(*arguments)
    first_payload = _payload(first)
    state_path = runtime_root / "state" / "cli-idempotent.json"
    plan_path = Path(first_payload["saved_plan_path"])
    trace_path = Path(first_payload["trace_path"])
    before = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in (state_path, plan_path, trace_path)
    }

    second = _run_cli(*arguments)

    assert first.returncode == second.returncode == 0
    assert _payload(second) == first_payload
    assert {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in (state_path, plan_path, trace_path)
    } == before
    assert list((runtime_root / "plans").glob("*.json")) == [plan_path]


def test_cli_json_input_and_output_are_complete_and_parseable(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    result_path = tmp_path / "result.json"
    input_path = tmp_path / "task.json"
    input_path.write_text(
        json.dumps(
            {
                "topic": "秋季新品内容活动",
                "budget": "20",
                "deadline": FUTURE_DEADLINE,
                "task_id": "cli-json",
                "model_mode": "mock",
                "runtime_dir": str(runtime_root),
                "state_dir": str(tmp_path / "custom-state"),
                "trace_dir": str(tmp_path / "custom-trace"),
                "json_output": str(result_path),
                "max_steps": 10,
                "max_tool_calls": 8,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    process = _run_cli("run", "--input", str(input_path))

    assert process.returncode == 0
    stdout_payload = _payload(process)
    file_payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert file_payload == stdout_payload
    assert Path(stdout_payload["trace_path"]).parent == (tmp_path / "custom-trace")
    assert (tmp_path / "custom-state" / "cli-json.json").is_file()
    assert set(stdout_payload) == {
        "errors",
        "incomplete_reason",
        "saved_plan_path",
        "state",
        "status",
        "task_id",
        "trace_path",
    }


def test_cli_infeasible_economy_plan_has_stable_incomplete_exit(tmp_path: Path) -> None:
    process = _run_cli(
        *_normal_arguments(tmp_path / "runtime", "cli-incomplete", budget="5")
    )

    assert process.returncode == 3
    payload = _payload(process)
    assert payload["status"] == "incomplete"
    assert payload["saved_plan_path"] is None
    assert payload["state"]["selected_plan_profile"] == "economy"
    assert payload["state"]["budget_gap"] == "3.00"
    assert "经济方案仍不可行" in payload["incomplete_reason"]


def test_cli_real_mode_without_configuration_has_stable_system_exit(tmp_path: Path) -> None:
    process = _run_cli(
        *_normal_arguments(tmp_path / "runtime", "cli-real-config"),
        "--model-mode",
        "real",
        unset_environment=("OPENAI_API_KEY", "OPENAI_MODEL"),
    )

    assert process.returncode == 4
    payload = _payload(process)
    assert payload["status"] == "system_error"
    assert payload["errors"][0]["type"] == "ModelConfigurationError"
    assert "OPENAI_API_KEY" in payload["errors"][0]["message"]
    assert not (tmp_path / "runtime").exists()


def test_readme_documents_copyable_stage_10_commands(tmp_path: Path) -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "python -m content_agent.cli run" in readme
    assert "--failure-injection" in readme
    assert "--input" in readme
    assert "退出码" in readme

    process = _run_cli(*_normal_arguments(tmp_path / "runtime", "readme-command"))
    assert process.returncode == 0
    assert _payload(process)["status"] == "completed"
