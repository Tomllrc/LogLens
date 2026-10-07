"""`loglens formats list | test | new`: inspect, check and create text format plugins."""

import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich import box
from rich.table import Table
from rich.text import Text

from loglens.cli.app import app, console, fail, settings_of
from loglens.formats import BUILT_IN, cached_registry, load_registry
from loglens.parsers.generic import GENERIC_TIMESTAMP_HINTS
from loglens.parsers.models import ParsedEvent
from loglens.parsers.textformats import (
    FormatDefinitionError,
    TextFormat,
    TextFormatParser,
    build_text_format,
    load_text_format,
    schema_errors,
)
from loglens.parsers.timestamps import parse_timestamp
from loglens.sampling import load_records
from loglens.schema import LEVELS, UNKNOWN, normalize_level
from loglens.split import Record

formats_app = typer.Typer(
    help="List, test and create text format plugins (YAML).", no_args_is_help=True
)
app.add_typer(formats_app, name="formats")

FormatsDirs = Annotated[
    list[Path] | None,
    typer.Option("--formats-dir", "-f", help="Extra folder of format YAML files (repeatable)."),
]
EXAMPLES = 5


def format_dirs(ctx: typer.Context, extra: list[Path] | None) -> tuple[Path, ...]:
    """The user formats folder, then any ``--formats-dir``, in override order."""
    return (settings_of(ctx).formats_dir, *(extra or ()))


@formats_app.command("list")
def list_formats(ctx: typer.Context, formats_dir: FormatsDirs = None) -> None:
    """Show every known text format, where it comes from, and invalid plugin files."""
    registry = load_registry(format_dirs(ctx, formats_dir))
    table = Table(box=box.SIMPLE_HEAD, header_style="bold")
    for column in ("Name", "Origin", "Priority", "Samples", "Description"):
        table.add_column(column, overflow="fold")
    for name, text_format in registry.text_formats.items():
        origin = registry.origins[name]
        table.add_row(
            Text(name, style="cyan"),
            Text(origin, style="dim" if origin == BUILT_IN else "green"),
            str(text_format.priority),
            str(len(text_format.samples)),
            Text(text_format.description.split(". ")[0][:90]),
        )
    out = console()
    out.print(table)
    out.print(f"[dim]User formats folder: {settings_of(ctx).formats_dir}[/dim]")
    for error in registry.errors:
        out.print(Text(f"invalid: {error}", style="red"))


@formats_app.command("test")
def test_format(
    format_file: Annotated[Path, typer.Argument(help="The format YAML to test.")],
    log_file: Annotated[Path, typer.Argument(help="A log file (or archive) to test it on.")],
    limit: Annotated[int, typer.Option("--limit", min=1, help="Records to test.")] = 10_000,
) -> None:
    """Measure a format on a log: match rate, matched examples, non-matching lines."""
    try:
        text_format = load_text_format(format_file)
    except FormatDefinitionError as error:
        raise fail(f"Invalid format file: {error}") from error
    records = load_records(log_file, limit)
    if not records:
        raise fail(f"No records found in {log_file}")
    parser = TextFormatParser(text_format)
    matched: list[tuple[str, ParsedEvent]] = []
    failing: list[str] = []
    for index, text in enumerate(records, start=1):
        event = parser.parse(Record(str(log_file), index, index, text))
        if event.error is None:
            matched.append((text, event))
        else:
            failing.append(text)
    out = console()
    rate = len(matched) / len(records)
    style = "green" if rate >= 0.95 else "yellow" if rate >= 0.5 else "red"
    out.print(
        f"[bold]{text_format.name}[/bold] matches [{style}]{rate:.1%}[/{style}] "
        f"of {len(records):,} records ({len(matched):,} matched, {len(failing):,} not)"
    )
    for text, event in matched[:EXAMPLES]:
        out.print(_fields_table(text, event))
    if failing:
        out.print(f"[bold]First {min(EXAMPLES, len(failing))} records that do not match:[/bold]")
        for text in failing[:EXAMPLES]:
            out.print(Text("  " + text.partition("\n")[0][:200], style="red"))


def _fields_table(text: str, event: ParsedEvent) -> Table:
    table = Table(
        title=Text(text.partition("\n")[0][:160], style="dim"),
        title_justify="left",
        box=box.SIMPLE,
        show_header=False,
    )
    table.add_column("Field", style="cyan")
    table.add_column("Value", overflow="fold")
    rows: list[tuple[str, str | None]] = [
        ("timestamp", event.timestamp_raw),
        ("level", f"{event.level_raw} -> {event.level}" if event.level_raw else None),
        ("component", event.component),
        ("message", event.message[:200]),
        *event.extra.items(),
    ]
    for name, value in rows:
        if value is not None:
            table.add_row(name, Text(value))
    return table


@formats_app.command("new")
def new_format(
    ctx: typer.Context,
    sample: Annotated[
        Path | None, typer.Option("--sample", "-s", help="A log file to test the regex on.")
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Where to save (user formats folder).")
    ] = None,
) -> None:
    """Interactive wizard: write a regex, test it live on your log, save a format YAML."""
    out = console()
    out.print("[bold]New text format[/bold] (Ctrl+C to quit)")
    name = _ask_name()
    description = typer.prompt("One-sentence description", default=f"Logs of {name}.")
    sample_path = sample or Path(typer.prompt("Sample log file"))
    records = load_records(sample_path, 2000)
    if not records:
        raise fail(f"No records found in {sample_path}")
    out.print(f"[dim]{len(records):,} records loaded. First lines:[/dim]")
    for text in records[:EXAMPLES]:
        out.print(Text("  " + text.partition("\n")[0][:160]))

    regex, matches = _ask_regex(records)
    definition: dict[str, Any] = {"name": name, "description": description, "regex": [regex]}
    timestamps = [m.group("timestamp") for m in matches if "timestamp" in m.re.groupindex]
    hints = _suggest_hints([t for t in timestamps if t])
    if hints:
        definition["timestamp_format"] = hints
    levels = [m.group("level") for m in matches if "level" in m.re.groupindex and m.group("level")]
    level_map = _suggest_level_map(levels)
    if level_map:
        definition["level"] = {"map": level_map}
    definition["sample_lines"] = [m.string for m in matches[:EXAMPLES]]
    definition["metadata"] = {"generated_by": "wizard"}

    problems = schema_errors(definition)
    if problems:
        raise fail("Invalid definition: " + "; ".join(problems))
    try:
        text_format = build_text_format(definition, source=f"{name}.yaml")
    except FormatDefinitionError as error:
        raise fail(f"Invalid definition: {error}") from error
    target = (output_dir or settings_of(ctx).formats_dir) / f"{name}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(definition, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )
    cached_registry.cache_clear()
    _print_saved(target, text_format)


def _ask_name() -> str:
    while True:
        name = str(typer.prompt("Format name (snake_case)")).strip()
        if re.fullmatch(r"[a-z][a-z0-9_]*", name):
            return name
        console().print(
            "[red]Use lowercase letters, digits and underscores, starting with a letter.[/red]"
        )


def _ask_regex(records: list[str]) -> tuple[str, list[re.Match[str]]]:
    """Prompt for a regex until it compiles, has named groups, and the user keeps it."""
    out = console()
    out.print(
        "[dim]Write a Python regex matched at the start of each record, with named groups "
        "such as (?P<timestamp>...), (?P<level>...), (?P<component>...), (?P<message>.*)[/dim]"
    )
    while True:
        regex = str(typer.prompt("Regex"))
        try:
            pattern = re.compile(regex)
        except re.error as error:
            out.print(f"[red]Does not compile: {error}[/red]")
            continue
        if not pattern.groupindex:
            out.print("[red]Add named groups, e.g. (?P<message>.*)[/red]")
            continue
        matches = [m for text in records if (m := pattern.match(text)) is not None]
        rate = len(matches) / len(records)
        out.print(f"Matches [bold]{rate:.1%}[/bold] of {len(records):,} records")
        if matches:
            fields = {k: v for k, v in matches[0].groupdict().items() if v is not None}
            out.print(
                Text("  first match: " + ", ".join(f"{k}={v[:40]!r}" for k, v in fields.items()))
            )
        missing = next((text for text in records if not pattern.match(text)), None)
        if missing is not None:
            out.print(
                Text("  first non-matching: " + missing.partition("\n")[0][:160], style="red")
            )
        if matches and typer.confirm("Keep this regex?", default=rate >= 0.95):
            return regex, matches


def _suggest_hints(timestamps: list[str]) -> list[str]:
    """Generic timestamp hints that parse most of the extracted timestamps."""
    sample = timestamps[:200]
    if not sample:
        return []
    for hint in GENERIC_TIMESTAMP_HINTS:
        parsed = sum(1 for text in sample if parse_timestamp(text, [hint]) is not None)
        if parsed / len(sample) >= 0.9:
            console().print(f"Timestamp format: [cyan]{hint}[/cyan]")
            return [hint]
    custom = str(typer.prompt("Timestamp strptime format (empty to skip)", default=""))
    return [custom] if custom else []


def _suggest_level_map(levels: list[str]) -> dict[str, str]:
    """Each raw level value mapped: standard words automatically, others by asking."""
    mapping: dict[str, str] = {}
    unknown: list[str] = []
    for raw, _ in Counter(levels).most_common(30):
        level = normalize_level(raw)
        if level == UNKNOWN:
            unknown.append(raw)
        else:
            mapping[raw] = level
    if mapping:
        console().print("Levels: " + ", ".join(f"{raw} -> {lv}" for raw, lv in mapping.items()))
    choices = [level for level in LEVELS if level != UNKNOWN]
    for raw in sorted(unknown):
        while True:
            answer = str(
                typer.prompt(f"Level of {raw!r} ({'/'.join(choices)}, empty to skip)", default="")
            )
            level = answer.strip().upper()
            if not level or level in choices:
                break
            console().print(f"[red]Use one of {', '.join(choices)}.[/red]")
        if level:
            mapping[raw] = level
    return mapping


def _print_saved(path: Path, text_format: TextFormat) -> None:
    out = console()
    out.print(f"[green]Saved[/green] {path}")
    out.print(f"Its {len(text_format.samples)} sample lines become tests. Try it:")
    out.print(f"  loglens formats test {path} <logfile>")
