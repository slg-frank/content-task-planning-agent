"""Stage 01 acceptance tests for the runnable project baseline."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def test_required_directories_exist() -> None:
    required_directories = ["src", "tests", "examples", "data", "docs"]
    missing = [name for name in required_directories if not (PROJECT_ROOT / name).is_dir()]
    assert not missing, f"Missing required directories: {missing}"


def test_required_stage_01_files_exist() -> None:
    required_files = [
        "README.md",
        "task.md",
        "requirements.txt",
        "pyproject.toml",
        ".env.example",
        ".gitignore",
        "src/content_agent/__init__.py",
        "src/content_agent/cli.py",
        "docs/01-requirements.md",
        "docs/02-solution.md",
        "docs/03-evaluation.md",
        "docs/04-ai-usage.md",
        "docs/05-retrospective.md",
    ]
    missing = [name for name in required_files if not (PROJECT_ROOT / name).is_file()]
    assert not missing, f"Missing required files: {missing}"


def test_package_can_be_imported() -> None:
    import content_agent

    assert content_agent.__version__ == "0.1.0"


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    existing_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(SRC_ROOT)
        if not existing_path
        else os.pathsep.join((str(SRC_ROOT), existing_path))
    )
    return subprocess.run(
        [sys.executable, "-m", "content_agent.cli", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_help_returns_success() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "content-agent" in result.stdout


def test_cli_without_arguments_displays_help() -> None:
    result = _run_cli()
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "content-agent" in result.stdout


def test_cli_version_returns_success() -> None:
    result = _run_cli("--version")
    assert result.returncode == 0, result.stderr
    assert "content-agent 0.1.0" in result.stdout


def test_env_example_does_not_contain_common_real_secret_patterns() -> None:
    content = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    suspicious_patterns = [
        r"sk-[A-Za-z0-9_-]{20,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    ]
    assert not any(re.search(pattern, content) for pattern in suspicious_patterns)


def test_readme_contains_required_review_guidance() -> None:
    content = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    required_sections = ["## 环境要求", "## 安装", "## 运行", "## 测试", "## 评审入口"]
    missing = [section for section in required_sections if section not in content]
    assert not missing, f"README is missing sections: {missing}"
    assert "python -m pytest tests/ -v" in content
    assert "python -m content_agent.cli --help" in content
