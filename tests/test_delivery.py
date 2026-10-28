"""Stage 12 checks for a safe, reproducible repository delivery."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    "",
    ".example",
    ".gitignore",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".toml",
    ".txt",
}
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "runtime",
}


def _delivery_files() -> list[Path]:
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {".env.example", ".gitignore"}:
            files.append(path)
    return files


def test_repository_contains_no_common_real_secret_patterns() -> None:
    patterns = (
        re.compile("sk-" + r"[A-Za-z0-9_-]{20,}"),
        re.compile("ghp_" + r"[A-Za-z0-9]{20,}"),
        re.compile("github_pat_" + r"[A-Za-z0-9_]{20,}"),
        re.compile("AKIA" + r"[0-9A-Z]{16}"),
        re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    )
    findings: list[str] = []
    for path in _delivery_files():
        content = path.read_text(encoding="utf-8", errors="replace")
        if any(pattern.search(content) for pattern in patterns):
            findings.append(str(path.relative_to(PROJECT_ROOT)))
    assert not findings, f"potential secrets found in: {findings}"


def test_repository_contains_no_machine_specific_absolute_paths() -> None:
    windows_user_path = re.compile(r"[A-Za-z]:[\\/](?:Users|Documents)[\\/]")
    project_path = re.compile(r"[A-Za-z]:[\\/]Gsl[\\/]agentcode", re.IGNORECASE)
    findings: list[str] = []
    for path in _delivery_files():
        content = path.read_text(encoding="utf-8", errors="replace")
        if windows_user_path.search(content) or project_path.search(content):
            findings.append(str(path.relative_to(PROJECT_ROOT)))
    assert not findings, f"machine-specific absolute paths found in: {findings}"


def test_local_secrets_caches_and_runtime_outputs_are_ignored() -> None:
    candidates = (
        ".env",
        ".venv/pyvenv.cfg",
        ".pytest_cache/CACHEDIR.TAG",
        "runtime/state/example.json",
        "src/content_agent/__pycache__/example.pyc",
    )
    process = subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", *candidates],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert process.returncode == 0, process.stderr
    for candidate in candidates:
        assert candidate in process.stdout
    assert not (PROJECT_ROOT / ".env").exists()
    assert not (PROJECT_ROOT / "runtime").exists()


def test_package_metadata_declares_supported_python_and_runtime_dependency() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    assert project["requires-python"] == ">=3.11"
    assert "openai>=2.0,<3.0" in project["dependencies"]
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pytest>=8.0,<10.0" in requirements
    assert "openai>=2.0,<3.0" in requirements


def test_required_delivery_code_documents_data_and_examples_exist() -> None:
    required = (
        "README.md",
        "task.md",
        "readme.txt",
        "pyproject.toml",
        "requirements.txt",
        ".env.example",
        ".gitignore",
        "src/content_agent/cli.py",
        "src/content_agent/agent.py",
        "data/materials.json",
        "data/tool_costs.json",
        "data/tool_durations.json",
        "docs/01-requirements.md",
        "docs/02-solution.md",
        "docs/03-evaluation.md",
        "docs/04-ai-usage.md",
        "docs/05-retrospective.md",
        "tests/test_acceptance.py",
        "tests/test_delivery.py",
        "examples/acceptance-matrix.json",
    )
    assert all((PROJECT_ROOT / item).is_file() for item in required)
    for stage in range(1, 13):
        assert (PROJECT_ROOT / f"examples/test-results/stage-{stage:02d}.txt").is_file()
    for scenario in (
        "normal",
        "missing_input",
        "tool_failure",
        "budget_exceeded",
        "repeated_execution",
    ):
        assert (PROJECT_ROOT / "examples" / scenario / "input.json").is_file()
        assert (PROJECT_ROOT / "examples" / scenario / "trace.jsonl").is_file()


def test_all_data_and_example_json_files_are_parseable() -> None:
    for root in (PROJECT_ROOT / "data", PROJECT_ROOT / "examples"):
        for path in root.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
        for path in root.rglob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                json.loads(line)


def test_readme_contains_install_run_test_review_and_delivery_guidance() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "## 安装",
        "## 运行",
        "## 测试",
        "## 评审入口",
        "python -m pip install -r requirements.txt",
        "python -m pip install -e .",
        "python -m content_agent.cli run",
        "python -m pytest tests/ -v",
        "python -m pytest tests/test_delivery.py -v",
        "https://github.com/slg-frank/content-task-planning-agent",
        "依赖与许可证",
    )
    missing = [item for item in required if item not in readme]
    assert not missing, f"README delivery guidance missing: {missing}"


def test_dependency_license_information_is_explicit_and_accurate() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    for expected in (
        "OpenAI Python SDK",
        "Apache-2.0",
        "pytest",
        "MIT",
        "项目自身许可证",
        "未选择",
    ):
        assert expected in readme
