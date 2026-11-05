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


def test_parse_without_output_fails_explicitly(tmp_path: Path) -> None:
    # Was "parse is a placeholder that fails"; parse now exists and fails explicitly
    # when it cannot do its job (no output directory).
    log_file = tmp_path / "app.log"
    log_file.write_text("2026-01-01 INFO started\n", encoding="utf-8")

    result = runner.invoke(app, ["parse", str(log_file)])

    assert result.exit_code == 1
    assert "output directory is required" in result.output


def test_parse_and_query(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "".join(f"2026-03-14 09:00:{i:02d} ERROR Job {i} failed after {i}s\n" for i in range(5))
        + "2026-03-14 09:01:00 INFO all good\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    parsed = runner.invoke(app, ["parse", str(log_file), "-o", str(out), "-w", "1"], env=WIDE)
    assert parsed.exit_code == 0, parsed.output
    for text in ("Records", "Throughput", "Top templates", "Top error templates", "free_text"):
        assert text in parsed.output
    assert "Job <NUM> failed after <DURATION>" in parsed.output

    queried = runner.invoke(
        app,
        [
            "query",
            str(out),
            "SELECT level, count(*) AS n FROM events GROUP BY level ORDER BY n DESC",
        ],
        env=WIDE,
    )
    assert queried.exit_code == 0, queried.output
    assert "ERROR" in queried.output and "2 rows" in queried.output


def test_parse_rejects_an_unknown_timezone(tmp_path: Path) -> None:
    result = runner.invoke(app, ["parse", str(tmp_path), "-o", str(tmp_path / "o"), "-z", "Mars/X"])
    assert result.exit_code == 1
    assert "unknown timezone" in result.output


def test_parse_with_nothing_readable_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["parse", str(tmp_path / "missing.log"), "-o", str(tmp_path / "o")])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_query_errors(tmp_path: Path) -> None:
    not_a_store = runner.invoke(app, ["query", str(tmp_path), "SELECT 1"])
    assert not_a_store.exit_code == 1 and "not a LogLens output directory" in not_a_store.output
    log_file = tmp_path / "a.log"
    log_file.write_text("x\n", encoding="utf-8")
    runner.invoke(app, ["parse", str(log_file), "-o", str(tmp_path / "out"), "-w", "1"])
    bad_sql = runner.invoke(app, ["query", str(tmp_path / "out"), "SELEC nonsense"])
    assert bad_sql.exit_code == 1 and "SQL error" in bad_sql.output


def test_errors_are_not_wrapped_or_read_as_markup(tmp_path: Path) -> None:
    """A long error with brackets prints verbatim on one line, whatever the width."""
    odd_dir = tmp_path / ("logs[bold]" + "x" * 80)
    odd_dir.mkdir()
    result = runner.invoke(app, ["query", str(odd_dir), "SELECT 1"], env={"COLUMNS": "40"})
    assert result.exit_code == 1
    assert f"{odd_dir} is not a LogLens output directory" in result.output


DETECT_FIXTURES = Path(__file__).parent / "fixtures" / "detect"
WIDE = {"COLUMNS": "250"}  # keep Rich from folding long paths in the table


def test_detect_prints_one_row_per_file() -> None:
    result = runner.invoke(app, ["detect", str(DETECT_FIXTURES)], env=WIDE)
    assert result.exit_code == 0, result.output
    prefix = DETECT_FIXTURES.as_posix()
    rows = {
        line.split()[0]: line.split()[1:3]
        for line in result.output.splitlines()
        if line.strip().startswith(prefix)
    }
    assert len(rows) == len(list(DETECT_FIXTURES.iterdir()))
    assert rows[f"{prefix}/prefix_json.log"] == ["prefix_json", "1.00"]
    assert rows[f"{prefix}/free_text.txt"] == ["free_text", "0.10"]
    assert rows[f"{prefix}/header.csv"] == ["csv", "1.00"]
    for header in ("File", "Format", "Confidence", "Details", "Runner-up"):
        assert header in result.output


def test_detect_shows_csv_details_and_runner_up() -> None:
    result = runner.invoke(
        app,
        ["detect", str(DETECT_FIXTURES / "data.tsv"), str(DETECT_FIXTURES / "ambiguous.log")],
        env=WIDE,
    )
    assert result.exit_code == 0
    assert "delimiter=tab, columns=4, header=no" in result.output
    ambiguous_row = next(line for line in result.output.splitlines() if "ambiguous.log" in line)
    assert ambiguous_row.split()[1:] == ["logfmt", "1.00", "csv", "1.00"]


def test_detect_reports_empty_and_skipped_files(tmp_path: Path) -> None:
    (tmp_path / "empty.log").write_bytes(b"")
    (tmp_path / "image.bin").write_bytes(b"\x89PNG\x00\x00\x00binary")
    (tmp_path / "app.log").write_text('INFO a {"n": 1}\nINFO b {"n": 2}\n', encoding="utf-8")

    result = runner.invoke(app, ["detect", str(tmp_path)], env=WIDE)

    assert result.exit_code == 0
    empty_row = next(line for line in result.output.splitlines() if "empty.log" in line)
    assert empty_row.split()[1:] == ["empty", "-"]
    assert "skipped" in result.output and "binary content" in result.output
    assert "prefix_json" in result.output


def test_detect_shows_partial_match_warnings(tmp_path: Path) -> None:
    path = tmp_path / "mostly.jsonl"
    path.write_text("".join(f'{{"n": {i}}}\n' for i in range(9)) + "not json\n", encoding="utf-8")
    result = runner.invoke(app, ["detect", str(path)], env=WIDE)
    assert result.exit_code == 0
    assert "warning" in result.output
    # "not json" doesn't start with "{", so splitting attaches it to the 9th record: 1 in 9.
    assert "detected json but 11% of sampled records do not match it" in result.output


def test_detect_nothing_readable_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["detect", str(tmp_path / "missing.log")], env=WIDE)
    assert result.exit_code == 1
    assert "not found" in result.output
    assert "No readable file" in result.output


def test_detect_reads_stdin() -> None:
    result = runner.invoke(app, ["detect", "-"], input="a=1 b=2\nc=3 d=4\n", env=WIDE)
    assert result.exit_code == 0
    assert "<stdin>" in result.output and "logfmt" in result.output


def test_detect_sample_option_is_validated() -> None:
    assert runner.invoke(app, ["detect", "--sample", "0", str(DETECT_FIXTURES)]).exit_code != 0


def test_parse_requires_a_path() -> None:
    result = runner.invoke(app, ["parse"])
    assert result.exit_code != 0
