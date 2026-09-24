"""Tests for the `loglens` command-line interface."""

import shutil
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

import loglens
from loglens.cli import app

runner = CliRunner()


def test_installed_entry_point_prints_version() -> None:
    """Smoke test: the installed `loglens` script runs and reports its version."""
    scripts_dir = Path(sys.executable).parent
    executable = shutil.which("loglens", path=str(scripts_dir))
    assert executable is not None, f"`loglens` script not found in {scripts_dir}"

    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"loglens {loglens.__version__}"


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"loglens {loglens.__version__}"


def test_short_version_option() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert loglens.__version__ in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "parse" in result.output


def test_parse_placeholder_fails_explicitly(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    log_file.write_text("2026-01-01 INFO started\n", encoding="utf-8")

    result = runner.invoke(app, ["parse", str(log_file)])

    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_parse_requires_a_path() -> None:
    result = runner.invoke(app, ["parse"])
    assert result.exit_code != 0
