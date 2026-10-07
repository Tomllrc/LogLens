"""`loglens parse` and `loglens query`."""

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

import duckdb
import typer
from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from loglens.cli.app import app, console, fail, spinner_for
from loglens.pipeline import ParseOptions, ParseSummary, run_parse
from loglens.pipeline.options import DEFAULT_WORKERS
from loglens.schema import LEVELS
from loglens.store import StoreError, query

LEVEL_STYLES = {
    "TRACE": "dim",
    "DEBUG": "dim",
    "INFO": "green",
    "WARN": "yellow",
    "ERROR": "red",
    "FATAL": "bold red",
    "UNKNOWN": "magenta",
}


@app.command()
def parse(
    paths: Annotated[
        list[Path], typer.Argument(help="Log files, folders, or archives; '-' reads stdin.")
    ],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Output directory (created if needed).")
    ] = None,
    timezone: Annotated[
        str, typer.Option("--timezone", "-z", help="Time zone of timestamps without one.")
    ] = "UTC",
    include: Annotated[
        list[str] | None,
        typer.Option("--include", "-i", help="Glob of files to read in folders (repeatable)."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", "-x", help="Glob of files or folders to skip (repeatable)."),
    ] = None,
    workers: Annotated[
        int, typer.Option("--workers", "-w", min=1, help="Parallel worker processes.")
    ] = DEFAULT_WORKERS,
    max_record_lines: Annotated[
        int, typer.Option("--max-record-lines", min=1, help="Longest multi-line record.")
    ] = 500,
    masking: Annotated[
        Path | None, typer.Option("--masking", help="Variable masking rules (YAML).")
    ] = None,
) -> None:
    """Parse logs into the unified event table (Parquet + DuckDB) and summarize them."""
    if output is None:
        raise fail("An output directory is required: loglens parse <paths> -o <dir>")
    try:
        options = ParseOptions(
            timezone=timezone,
            include=tuple(include or ()),
            exclude=tuple(exclude or ()),
            workers=workers,
            max_record_lines=max_record_lines,
            masking_file=masking,
        )
    except ValueError as error:
        raise fail(str(error)) from error

    out = console()
    with out.status("Parsing...", spinner=spinner_for(out)) as status:

        def progress(stage: str, done: int, total: int) -> None:
            amount = f"{done}/{total}" if total else f"{done:,}"
            status.update(f"{stage.capitalize()}: {amount}")

        summary = run_parse([str(p) for p in paths], output, options, progress=progress)
    out.print(render_summary(summary))
    if not summary.files:
        raise typer.Exit(code=1)


def render_summary(summary: ParseSummary) -> Group:
    """The Rich view of a parse run."""
    numbers = Table.grid(padding=(0, 2))
    numbers.add_column(style="bold")
    numbers.add_column(justify="right")
    numbers.add_row("Source files", f"{len(summary.files):,}")
    numbers.add_row("Skipped", f"{len(summary.skipped):,}")
    numbers.add_row("Records", f"{summary.records:,}")
    numbers.add_row("Events", f"{summary.events:,}")
    numbers.add_row("Templates", f"{summary.templates:,}")
    numbers.add_row("Throughput", f"{summary.records_per_second:,.0f} records/s")
    numbers.add_row("Low confidence", f"{summary.low_confidence_share:.1%}")
    numbers.add_row("Warnings", f"{summary.warnings:,}")
    numbers.add_row("Time", f"{summary.seconds:.2f} s")

    formats = _counts_table("Format", summary.formats.most_common(), "files")
    levels = _counts_table(
        "Level",
        [(level, summary.levels[level]) for level in LEVELS if summary.levels.get(level)],
        "events",
        styles=LEVEL_STYLES,
    )
    parts: list[Any] = [
        Panel(
            Columns([numbers, formats, levels], padding=(0, 4)),
            title=f"Parsed into {summary.output_dir}",
            title_align="left",
        ),
        _templates_table("Top templates", summary.top_templates),
    ]
    if summary.top_error_templates:
        parts.append(_templates_table("Top error templates", summary.top_error_templates, "red"))
    if summary.skipped:
        lines = [f"  {issue.source_file}: {issue.reason}" for issue in summary.skipped[:10]]
        more = len(summary.skipped) - 10
        lines += [f"  ... and {more} more"] if more > 0 else []
        parts.append(Text("Skipped:\n" + "\n".join(lines), style="yellow"))
    for source, error in summary.failed_inputs:
        parts.append(Text(f"Failed: {source}: {error}", style="red"))
    return Group(*parts)


def _counts_table(
    title: str,
    rows: Sequence[tuple[str, int]],
    unit: str,
    styles: dict[str, str] | None = None,
) -> Table:
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    table.add_column(title)
    table.add_column(unit.capitalize(), justify="right")
    for name, count in rows:
        table.add_row(Text(name, style=(styles or {}).get(name, "")), f"{count:,}")
    return table


def _templates_table(title: str, rows: Sequence[tuple[str, int]], style: str = "") -> Table:
    table = Table(title=title, title_justify="left", box=box.SIMPLE_HEAD, header_style="bold")
    table.add_column("Events", justify="right", style=style)
    table.add_column("Template", overflow="fold")
    for template, count in rows:
        table.add_row(f"{count:,}", Text(template or "(empty)"))
    return table


@app.command("query")
def query_command(
    directory: Annotated[Path, typer.Argument(help="Output directory of `loglens parse`.")],
    sql: Annotated[str, typer.Argument(help="SQL over events, templates, files, warnings.")],
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Rows to show.")] = 100,
) -> None:
    """Run read-only SQL against a parsed directory (DuckDB dialect)."""
    try:
        result = query(directory, sql, limit=limit)
    except StoreError as error:
        raise fail(str(error)) from error
    except duckdb.Error as error:
        raise fail(f"SQL error: {error}") from error
    out = console()
    if not result.columns:
        out.print("[dim]No result set.[/dim]")
        return
    table = Table(box=box.SIMPLE_HEAD, header_style="bold")
    for column in result.columns:
        table.add_column(column, overflow="fold")
    for row in result.rows:
        table.add_row(*(Text("" if value is None else str(value)) for value in row))
    out.print(table)
    shown = f"{len(result.rows):,} row{'s' if len(result.rows) != 1 else ''}"
    out.print(f"[dim]{shown}{f' (first {limit}, use --limit)' if result.truncated else ''}[/dim]")
