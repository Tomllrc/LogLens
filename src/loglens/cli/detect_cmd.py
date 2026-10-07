"""`loglens detect`: show each file's detected format."""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from loglens.cli.app import app
from loglens.detect import (
    DEFAULT_SAMPLE_RECORDS,
    FREE_TEXT,
    DetectionResult,
    DetectOptions,
    DetectReport,
    iter_detected,
)
from loglens.ingest import IngestOptions, IngestReport, iter_lines
from loglens.split import iter_records

LINES_PER_SAMPLED_RECORD = 50
"""Line budget per sampled record: `detect` stops reading a file after
``sample * LINES_PER_SAMPLED_RECORD`` lines instead of reading it all."""


@app.command()
def detect(
    paths: Annotated[
        list[Path],
        typer.Argument(help="Log files, folders, or archives; '-' reads stdin."),
    ],
    sample: Annotated[
        int,
        typer.Option("--sample", "-n", min=1, help="Records sampled per file."),
    ] = DEFAULT_SAMPLE_RECORDS,
) -> None:
    """Detect the log format of each file and show how confident the detection is."""
    ingest_report = IngestReport()
    detect_report = DetectReport()
    lines = iter_lines(
        paths,
        options=IngestOptions(max_lines_per_file=sample * LINES_PER_SAMPLED_RECORD),
        report=ingest_report,
    )
    detected = iter_detected(
        iter_records(lines), options=DetectOptions(sample_records=sample), report=detect_report
    )
    for _ in detected:
        pass

    console = Console()
    if ingest_report.files_read:
        console.print(_detection_table(ingest_report.files_read, detect_report.detections))
    for issue in ingest_report.files_skipped:
        console.print(Text.assemble(("skipped ", "yellow"), issue.source_file, f": {issue.reason}"))
    for issue in detect_report.warnings:
        console.print(Text.assemble(("warning ", "yellow"), issue.source_file, f": {issue.reason}"))
    if not ingest_report.files_read:
        console.print("[red]No readable file found.[/red]")
        raise typer.Exit(code=1)


def _detection_table(files: list[str], detections: Mapping[str, DetectionResult]) -> Table:
    """One row per file read: format, confidence, format details, and the runner-up."""
    table = Table(header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("File", overflow="fold")
    table.add_column("Format", style="cyan", no_wrap=True)
    table.add_column("Confidence", justify="right", no_wrap=True)
    table.add_column("Details")
    table.add_column("Runner-up", style="dim", no_wrap=True)
    for source in files:
        result = detections.get(source)
        if result is None:
            table.add_row(Text(source), Text("empty", style="dim"), "-", "", "")
            continue
        details = ", ".join(f"{key}={value}" for key, value in result.details.items())
        runner_up = result.runner_up
        table.add_row(
            Text(source),
            result.format_name,
            Text(f"{result.confidence:.2f}", style=_confidence_style(result)),
            Text(details),
            f"{runner_up[0]} {runner_up[1]:.2f}" if runner_up else "",
        )
    return table


def _confidence_style(result: DetectionResult) -> str:
    if result.format_name == FREE_TEXT:
        return "dim"
    return "green" if result.confidence >= 0.9 else "yellow"
