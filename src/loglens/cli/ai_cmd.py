"""`loglens doctor` and `loglens learn`: the AI set-up check and the format learner."""

import time
from collections.abc import Callable
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from loglens.ai.format_learner import LearnCancelled, LearnResult, learn_format
from loglens.ai.llm import (
    PRIVACY_WARNING,
    AIError,
    LLMClient,
    LLMRequest,
    ResilientClient,
    UsageTracker,
    create_client,
)
from loglens.cli.app import app, console, fail, settings_of
from loglens.config import API_KEY_VARIABLE, Settings, api_key_is_set
from loglens.formats import cached_registry


def make_client(settings: Settings, tracker: UsageTracker) -> LLMClient:
    """The configured client (tests replace this function to inject a FakeClient)."""
    return create_client(settings, tracker=tracker)


@app.command()
def doctor(
    ctx: typer.Context,
    no_call: Annotated[
        bool, typer.Option("--no-call", help="Skip the test call to the model.")
    ] = False,
) -> None:
    """Check the AI set-up: key present (never shown), model, cache, and a tiny test call."""
    settings = settings_of(ctx)
    out = console()
    cache_entries = (
        len(list(settings.cache_dir.glob("*.json"))) if settings.cache_dir.is_dir() else 0
    )
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("AI enabled", _yes_no(settings.ai_enabled))
    table.add_row(
        f"{API_KEY_VARIABLE}", "set" if api_key_is_set() else Text("not set", style="red")
    )
    table.add_row("Model", settings.model)
    table.add_row("Min delay between calls", f"{settings.min_delay_seconds:g} s")
    table.add_row("Home", str(settings.home))
    table.add_row("Response cache", f"{settings.cache_dir} ({cache_entries} entries)")
    table.add_row("User formats", str(settings.formats_dir))
    out.print(Panel(table, title="LogLens doctor", title_align="left"))
    out.print(Text(PRIVACY_WARNING, style="yellow"))
    if no_call or not settings.ai_enabled:
        return
    tracker = UsageTracker()
    try:
        client = make_client(settings, tracker)
        inner = client.inner if isinstance(client, ResilientClient) else client  # no cache
        started = time.perf_counter()
        response = inner.complete(LLMRequest.from_prompt("Reply with the single word OK."))
        tracker.record(response)
    except AIError as error:
        raise fail(f"Test call failed: {error}") from error
    elapsed = time.perf_counter() - started
    out.print(f"[green]Test call OK[/green] in {elapsed:.1f} s: {response.text.strip()[:40]!r}")
    out.print(tracker.summary(settings.model))


def _yes_no(value: bool) -> Text:
    return Text("yes", style="green") if value else Text("no", style="yellow")


@app.command()
def learn(
    ctx: typer.Context,
    log_file: Annotated[str, typer.Argument(help="A log file, or archive member a.zip!/x.log.")],
    name: Annotated[str | None, typer.Option("--name", help="Name of the new format.")] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Send the samples without asking.")
    ] = False,
) -> None:
    """Teach LogLens a new log format with Gemini (about 50 sample lines, one call)."""
    settings = settings_of(ctx)
    tracker = UsageTracker()
    try:
        client = make_client(settings, tracker)
    except AIError as error:
        raise fail(str(error)) from error
    try:
        result = learn_format(
            log_file,
            client,
            formats_dir=settings.formats_dir,
            name=name,
            confirm=None if yes else confirm_samples(settings),
        )
    except LearnCancelled as error:
        raise fail(str(error), code=0) from error
    except AIError as error:
        console().print(tracker.summary(settings.model))
        raise fail(str(error)) from error
    cached_registry.cache_clear()
    show_learn_result(result)
    console().print(tracker.summary(settings.model))
    if result.path is None:
        raise typer.Exit(code=1)


def confirm_samples(settings: Settings) -> Callable[[list[str]], bool]:
    """A confirm callback that shows the privacy warning and the samples, then asks."""

    def confirm(samples: list[str]) -> bool:
        out = console()
        out.print(Text(PRIVACY_WARNING, style="yellow"))
        out.print(f"[bold]{len(samples)} sample records will be sent to {settings.model}:[/bold]")
        for sample in samples:
            out.print(Text("  " + sample.partition("\n")[0][:160], style="dim"))
        return bool(typer.confirm("Send them?", default=False))

    return confirm


def show_learn_result(result: LearnResult) -> None:
    """Print the validation of a learned format."""
    out = console()
    validation = result.validation
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Attempts", str(result.attempts))
    table.add_row("Records tested", f"{validation.records:,}")
    table.add_row("Match rate", f"{validation.match_rate:.1%}")
    table.add_row("Timestamps parsed", f"{validation.timestamp_rate:.1%}")
    if validation.level_rate is not None:
        table.add_row("Levels mapped", f"{validation.level_rate:.1%}")
    for problem in validation.errors:
        table.add_row("Problem", Text(problem, style="red"))
    out.print(Panel(table, title="Learned format", title_align="left"))
    if result.path is not None:
        out.print(f"[green]Saved[/green] {result.path} (used from now on by parse and detect)")
    else:
        out.print("[red]No usable format was learned; nothing was saved.[/red]")
