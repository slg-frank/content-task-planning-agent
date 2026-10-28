"""Command-line entry point for the content task planning agent."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

from content_agent import __version__
from content_agent.agent import AgentLimits, AgentRunner, LocalToolExecutor
from content_agent.model import ModelConfigurationError, create_model_adapter
from content_agent.models import FinalStatus
from content_agent.state_store import StateStore
from content_agent.tools import FailureInjector
from content_agent.trace import TraceRecorder
from content_agent.validation import validate_task_input


EXIT_COMPLETED = 0
EXIT_INPUT_ERROR = 2
EXIT_INCOMPLETE = 3
EXIT_SYSTEM_ERROR = 4


class CliInputError(ValueError):
    """A user-correctable command-line or JSON input error."""


class CliArgumentParser(argparse.ArgumentParser):
    """Turn parser failures into the same structured input-error response."""

    def error(self, message: str) -> None:
        raise CliInputError(message)


def build_parser() -> argparse.ArgumentParser:
    """Build the complete stage-10 command-line parser."""
    parser = CliArgumentParser(
        prog="content-agent",
        description="在预算和截止时间约束内规划内容活动。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser(
        "run",
        help="执行一个内容规划任务",
        description="从命令行参数或 UTF-8 JSON 文件执行内容规划任务。",
    )
    run_parser.add_argument("--input", type=Path, help="JSON 输入文件")
    run_parser.add_argument("--topic", help="活动主题")
    run_parser.add_argument("--budget", help="非负预算")
    run_parser.add_argument("--deadline", help="带时区的 ISO 8601 截止时间")
    run_parser.add_argument("--task-id", help="可重复使用的幂等任务 ID")
    run_parser.add_argument(
        "--model-mode",
        choices=("mock", "real"),
        help="模型模式；默认 mock",
    )
    run_parser.add_argument(
        "--failure-injection",
        help='测试用 JSON 对象，例如 {"script_generation":1}',
    )
    run_parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="运行产物根目录；默认 ./runtime",
    )
    run_parser.add_argument("--state-dir", type=Path, help="状态目录")
    run_parser.add_argument("--trace-dir", type=Path, help="轨迹目录")
    run_parser.add_argument("--json-output", type=Path, help="额外写入完整 JSON 结果")
    run_parser.add_argument("--max-steps", help="最大模型决策步数；默认 10")
    run_parser.add_argument("--max-tool-calls", help="最大工具调用数；默认 8")
    return parser


def _read_input_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliInputError(f"无法读取输入文件：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise CliInputError(
            f"输入文件不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）"
        ) from exc
    if not isinstance(raw, Mapping):
        raise CliInputError("JSON 输入必须是对象")
    return dict(raw)


def _parse_json_object(value: str, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CliInputError(f"{field_name} 必须是有效 JSON 对象") from exc
    if not isinstance(parsed, Mapping):
        raise CliInputError(f"{field_name} 必须是 JSON 对象")
    return dict(parsed)


def _merged_input(args: argparse.Namespace) -> dict[str, Any]:
    raw = _read_input_file(args.input) if args.input is not None else {}
    direct_values = {
        "topic": args.topic,
        "budget": args.budget,
        "deadline": args.deadline,
        "task_id": args.task_id,
        "model_mode": args.model_mode,
    }
    for field_name, value in direct_values.items():
        if value is not None:
            raw[field_name] = value
    if args.failure_injection is not None:
        raw["failure_injection"] = _parse_json_object(
            args.failure_injection,
            "--failure-injection",
        )
    raw.setdefault("model_mode", "mock")
    return raw


def _positive_integer(value: Any, field_name: str, default: int) -> int:
    raw = default if value is None else value
    if isinstance(raw, bool):
        raise CliInputError(f"{field_name} 必须是正整数")
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise CliInputError(f"{field_name} 必须是正整数") from exc
    if parsed < 1 or str(raw).strip() != str(parsed):
        raise CliInputError(f"{field_name} 必须是正整数")
    return parsed


def _limits(args: argparse.Namespace, raw: Mapping[str, Any]) -> AgentLimits:
    max_steps = _positive_integer(
        args.max_steps if args.max_steps is not None else raw.get("max_steps"),
        "max_steps",
        10,
    )
    max_tool_calls = _positive_integer(
        (
            args.max_tool_calls
            if args.max_tool_calls is not None
            else raw.get("max_tool_calls")
        ),
        "max_tool_calls",
        8,
    )
    return AgentLimits(max_steps=max_steps, max_tool_calls=max_tool_calls)


def _configured_path(
    cli_value: Path | None,
    raw: Mapping[str, Any],
    field_name: str,
    default: Path,
) -> Path:
    value: Any = cli_value if cli_value is not None else raw.get(field_name, default)
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise CliInputError(f"{field_name} 必须是非空路径")
    return Path(value).expanduser().resolve()


def _invalid_payload(message: str, *, errors: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "task_id": None,
        "status": FinalStatus.INVALID_INPUT.value,
        "state": None,
        "trace_path": None,
        "saved_plan_path": None,
        "incomplete_reason": None,
        "errors": errors
        or [{"code": "invalid_cli_input", "field": "$", "message": message}],
    }


def _system_error_payload(message: str, error_type: str) -> dict[str, Any]:
    return {
        "task_id": None,
        "status": "system_error",
        "state": None,
        "trace_path": None,
        "saved_plan_path": None,
        "incomplete_reason": None,
        "errors": [
            {
                "code": "system_error",
                "field": "$",
                "message": message,
                "type": error_type,
            }
        ],
    }


def _emit(payload: Mapping[str, Any], summary: str) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    print(summary, file=sys.stderr)


def _write_json_output(path: Path, payload: Mapping[str, Any]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_command(args: argparse.Namespace) -> int:
    try:
        raw = _merged_input(args)
        limits = _limits(args, raw)
        runtime_root = _configured_path(
            args.runtime_dir,
            raw,
            "runtime_dir",
            Path("runtime"),
        )
        state_root = _configured_path(
            args.state_dir,
            raw,
            "state_dir",
            runtime_root / "state",
        )
        trace_root = _configured_path(
            args.trace_dir,
            raw,
            "trace_dir",
            runtime_root / "traces",
        )
        json_output_value: Any = (
            args.json_output
            if args.json_output is not None
            else raw.get("json_output")
        )
        if json_output_value is not None and (
            not isinstance(json_output_value, (str, Path))
            or not str(json_output_value).strip()
        ):
            raise CliInputError("json_output 必须是非空路径")
    except CliInputError as exc:
        payload = _invalid_payload(str(exc))
        _emit(payload, f"输入错误：{exc}")
        return EXIT_INPUT_ERROR

    now = datetime.now(timezone.utc)
    validation = validate_task_input(raw, now=now)
    if not validation.is_valid:
        errors = [error.to_dict() for error in validation.errors]
        payload = _invalid_payload("任务输入校验失败", errors=errors)
        if json_output_value is not None:
            try:
                _write_json_output(Path(json_output_value), payload)
            except OSError as exc:
                payload = _system_error_payload(
                    "无法写入 JSON 结果文件",
                    type(exc).__name__,
                )
                _emit(payload, f"系统错误：无法写入结果文件（{type(exc).__name__}）")
                return EXIT_SYSTEM_ERROR
        _emit(payload, f"输入错误：任务输入校验失败（{len(errors)} 项）")
        return EXIT_INPUT_ERROR

    assert validation.task is not None
    task = validation.task
    try:
        failure_injector = FailureInjector(task.failure_injection)
    except (TypeError, ValueError) as exc:
        payload = _invalid_payload(
            str(exc),
            errors=[
                {
                    "code": "invalid_failure_injection",
                    "field": "failure_injection",
                    "message": str(exc),
                }
            ],
        )
        if json_output_value is not None:
            try:
                _write_json_output(Path(json_output_value), payload)
            except OSError as write_error:
                payload = _system_error_payload(
                    "无法写入 JSON 结果文件",
                    type(write_error).__name__,
                )
                _emit(
                    payload,
                    f"系统错误：无法写入结果文件（{type(write_error).__name__}）",
                )
                return EXIT_SYSTEM_ERROR
        _emit(payload, f"输入错误：{exc}")
        return EXIT_INPUT_ERROR

    try:
        model = create_model_adapter(task.model_mode)
        runner = AgentRunner(
            model=model,
            tool_executor=LocalToolExecutor(
                runtime_root=runtime_root,
                failure_injector=failure_injector,
            ),
            state_store=StateStore(state_root),
            trace_recorder=TraceRecorder(task.task_id, trace_root),
            limits=limits,
            clock=lambda: now,
        )
        result = runner.run(task)
        payload = result.to_dict()
        payload["errors"] = []
        if result.status is FinalStatus.COMPLETED:
            summary = (
                f"任务 {task.task_id} 已完成；方案：{payload['saved_plan_path']}；"
                f"轨迹：{payload['trace_path']}"
            )
            exit_code = EXIT_COMPLETED
        else:
            summary = (
                f"任务 {task.task_id} 未完成（{result.status.value}）："
                f"{result.incomplete_reason or '无补充原因'}"
            )
            exit_code = EXIT_INCOMPLETE
    except ModelConfigurationError as exc:
        payload = _system_error_payload(str(exc), type(exc).__name__)
        summary = f"系统配置错误：{exc}"
        exit_code = EXIT_SYSTEM_ERROR
    except Exception as exc:  # Defensive CLI boundary; details stay out of stdout.
        payload = _system_error_payload(
            "执行期间发生未预期的系统错误，请检查本地日志和配置",
            type(exc).__name__,
        )
        summary = f"系统错误：{type(exc).__name__}"
        exit_code = EXIT_SYSTEM_ERROR

    if json_output_value is not None:
        try:
            _write_json_output(Path(json_output_value), payload)
        except OSError as exc:
            payload = _system_error_payload(
                "无法写入 --json-output 指定的结果文件",
                type(exc).__name__,
            )
            summary = f"系统错误：无法写入结果文件（{type(exc).__name__}）"
            exit_code = EXIT_SYSTEM_ERROR
    _emit(payload, summary)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a stable process exit code."""
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if not arguments:
        parser.print_help()
        return EXIT_COMPLETED
    try:
        args = parser.parse_args(arguments)
    except CliInputError as exc:
        payload = _invalid_payload(str(exc))
        _emit(payload, f"输入错误：{exc}")
        return EXIT_INPUT_ERROR
    if args.command == "run":
        return _run_command(args)
    parser.print_help()
    return EXIT_COMPLETED


if __name__ == "__main__":
    raise SystemExit(main())
