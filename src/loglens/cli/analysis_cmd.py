"""`loglens issues`, `loglens anomalies` and `loglens compare`."""

import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import duckdb
import typer
from rich import box
from rich.table import Table
from rich.text import Text

from loglens.analysis.anomalies import KINDS, AnomalyReport, find_anomalies
from loglens.analysis.compare import CompareReport, IssueChange, compare
from loglens.analysis.issues import IssuesReport, find_issues
from loglens.analysis.model import TimeWindow, jsonable
from loglens.analysis.signals import Signals, SignalsError, load_signals
from loglens.analysis.timeseries import format_bucket, parse_bucket
from loglens.cli.app import app, console, fail
from loglens.store import StoreError, connect

StoreArg = Annotated[Path, typer.Argument(help="Output directory of `loglens parse`.")]
SinceOpt = Annotated[
    str | None, typer.Option("--since", help="Only events at or after this time (ISO 8601).")
]
UntilOpt = Annotated[
    str | None, typer.Option("--until", help="Only events before this time (ISO 8601).")
]
SignalsOpt = Annotated[
    Path | None, typer.Option("--signals", help="Issue signals file (YAML) replacing the defaults.")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")]

KIND_TITLES = {
    "spike": "Spikes",
    "level_shift": "Level shifts",
    "new_pattern": "New patterns",
    "rare_transition": "Rare transitions",
}


def parse_time(text: str | None) -> datetime | None:
    """An ISO 8601 time from the command line; naive times are UTC."""
    if text is None:
        return None
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError as error:
        raise fail(f"invalid time {text!r}: use ISO 8601, e.g. 2026-03-01T12:00:00Z") from error
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _window(since: str | None, until: str | None) -> TimeWindow:
    return TimeWindow(parse_time(since), parse_time(until))


def _signals(path: Path | None) -> Signals:
    try:
        return load_signals(path)
    except SignalsError as error:
        raise fail(str(error)) from error


@contextmanager
def open_store(directory: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only connection to a store; a clean error when it is not one."""
    try:
        connection = connect(directory)
    except StoreError as error:
        raise fail(str(error)) from error
    try:
        yield connection
    finally:
        connection.close()


def _echo_json(report: object) -> None:
    typer.echo(json.dumps(jsonable(report), indent=2, ensure_ascii=False))


def _time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "-"


def _notes(notes: tuple[str, ...]) -> None:
    for note in notes:
        console().print(f"[dim]Note: {note}[/dim]")


@app.command()
def issues(
    store: StoreArg,
    top: Annotated[int, typer.Option("--top", "-n", min=1, help="Issues to show.")] = 20,
    since: SinceOpt = None,
    until: UntilOpt = None,
    min_events: Annotated[
        int, typer.Option("--min-events", min=1, help="Hide smaller issues.")
    ] = 1,
    signals: SignalsOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Recurring issues ranked by impact, with the Pareto "vital few"."""
    window = _window(since, until)
    rules = _signals(signals)
    with open_store(store) as conn:
        report = find_issues(conn, window=window, signals=rules, min_events=min_events)
    shown = dataclasses.replace(report, issues=report.issues[:top])
    if as_json:
        _echo_json(shown)
        return
    render_issues(shown, total_issues=len(report.issues))


def render_issues(report: IssuesReport, total_issues: int) -> None:
    """Print the issues table and the Pareto summary."""
    out = console()
    if not report.issues:
        out.print("No issues found.")
        _notes(report.notes)
        return
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", title="Recurring issues")
    for column, justify in (
        ("#", "right"),
        ("Impact", "right"),
        ("Share", "right"),
        ("Cum.", "right"),
        ("Events", "right"),
        ("First seen", "left"),
        ("Last seen", "left"),
        ("Files", "right"),
        ("Issue", "left"),
    ):
        table.add_column(column, justify=justify)  # type: ignore[arg-type]
    for rank, issue in enumerate(report.issues, 1):
        title = Text(issue.title, style="bold" if issue.vital_few else "")
        if len(issue.template_ids) > 1:
            title.append(f"  (+{len(issue.template_ids) - 1} similar)", style="dim")
        table.add_row(
            str(rank),
            f"{issue.impact:,.0f}",
            f"{issue.share:.1%}",
            f"{issue.cumulative_share:.0%}",
            f"{issue.events:,}",
            _time(issue.first_seen),
            _time(issue.last_seen),
            str(issue.sources),
            title,
        )
    out.print(table)
    vital = [i for i in report.issues if i.vital_few]
    if vital:
        out.print(
            f"[bold]{len(vital)} of {total_issues} issues account for "
            f"{vital[-1].cumulative_share:.0%} of impact[/bold] (bold rows)."
        )
    _notes(report.notes)


@app.command()
def anomalies(
    store: StoreArg,
    bucket: Annotated[
        str | None,
        typer.Option("--bucket", help="Time bucket: 30s, 5m, 1h, 1d, 1w (default: auto)."),
    ] = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    baseline_until: Annotated[
        str | None,
        typer.Option("--baseline-until", help="End of the normal period (new-pattern cutoff)."),
    ] = None,
    kind: Annotated[
        list[str] | None,
        typer.Option("--kind", "-k", help=f"Detector to run (repeatable): {', '.join(KINDS)}."),
    ] = None,
    signals: SignalsOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Spikes, level shifts, new patterns and rare transitions."""
    window = _window(since, until)
    cutoff = parse_time(baseline_until)
    rules = _signals(signals)
    try:
        size = parse_bucket(bucket) if bucket else None
    except ValueError as error:
        raise fail(str(error)) from error
    with open_store(store) as conn:
        try:
            report = find_anomalies(
                conn,
                window=window,
                signals=rules,
                bucket=size,
                baseline_until=cutoff,
                kinds=kind or KINDS,
            )
        except ValueError as error:
            raise fail(str(error)) from error
    if as_json:
        _echo_json(report)
        return
    render_anomalies(report)


def render_anomalies(report: AnomalyReport) -> None:
    """Print one table per anomaly kind."""
    out = console()
    if report.grid is not None:
        out.print(
            f"[dim]{_time(report.span.start)} -> {_time(report.span.end)}, "
            f"{report.grid.size} buckets of {format_bucket(report.grid.bucket)}[/dim]"
        )
    if not report.anomalies:
        out.print("No anomalies found.")
    for kind in KINDS:
        found = report.by_kind(kind)
        if not found:
            continue
        table = Table(box=box.SIMPLE_HEAD, header_style="bold", title=KIND_TITLES[kind])
        table.add_column("Score", justify="right")
        table.add_column("When")
        table.add_column("What")
        table.add_column("Issue")
        table.add_column("Evidence")
        for anomaly in found:
            table.add_row(
                f"{anomaly.score:.2f}",
                _time(anomaly.start),
                anomaly.summary,
                anomaly.issue_id or "-",
                anomaly.evidence[0].location if anomaly.evidence else "-",
            )
        out.print(table)
    _notes(report.notes)


@app.command("compare")
def compare_command(
    baseline: Annotated[
        Path, typer.Argument(help="Baseline store (or the only store, with --split).")
    ],
    target: Annotated[
        Path | None, typer.Argument(help="Store to compare with the baseline.")
    ] = None,
    split: Annotated[
        str | None,
        typer.Option("--split", help="Compare one store before and after this time (ISO 8601)."),
    ] = None,
    signals: SignalsOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """New, gone and changed issues between two stores or two periods."""
    if (target is None) == (split is None):
        raise fail("Give exactly one of TARGET or --split.")
    rules = _signals(signals)
    if target is None:
        moment = parse_time(split)
        with open_store(baseline) as conn:
            report = compare(
                conn,
                conn,
                baseline_window=TimeWindow(until=moment),
                target_window=TimeWindow(since=moment),
                signals=rules,
            )
    else:
        with open_store(baseline) as base_conn, open_store(target) as target_conn:
            report = compare(base_conn, target_conn, signals=rules)
    if as_json:
        _echo_json(report)
        return
    render_compare(report)


def render_compare(report: CompareReport) -> None:
    """Print new, gone and changed issues, then the overall numbers."""
    out = console()
    sections: tuple[tuple[str, tuple[IssueChange, ...], str], ...] = (
        ("New issues", report.new, "red"),
        ("Gone issues", report.gone, "green"),
        ("Changed issues", report.changed, "yellow"),
    )
    for title, changes, style in sections:
        if not changes:
            out.print(f"[dim]{title}: none[/dim]")
            continue
        table = Table(box=box.SIMPLE_HEAD, header_style="bold", title=title, title_style=style)
        table.add_column("Before/h", justify="right")
        table.add_column("After/h", justify="right")
        table.add_column("Ratio", justify="right")
        table.add_column("p", justify="right")
        table.add_column("Issue")
        for change in changes:
            table.add_row(
                f"{change.baseline_rate:,.1f}",
                f"{change.target_rate:,.1f}",
                "-" if change.p_value is None else f"x{change.ratio:.1f}",
                "-" if change.p_value is None else f"{change.p_value:.1g}",
                change.title,
            )
        out.print(table)
    out.print(
        f"Baseline: {report.baseline.events:,} events, {report.baseline.error_rate:.1%} errors. "
        f"Target: {report.target.events:,} events, {report.target.error_rate:.1%} errors."
    )
    _notes(report.notes)
