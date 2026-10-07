"""The Typer application and helpers shared by the command modules."""

from typing import Annotated

import typer
from rich.console import Console

from loglens import __version__

app = typer.Typer(
    name="loglens",
    help="Turn any log into structured events, recurring issues, and cited answers.",
    no_args_is_help=True,
    add_completion=False,
)


def console() -> Console:
    """A console writing to the current stdout (created per call, so tests can capture it)."""
    return Console()


def spinner_for(out: Console) -> str:
    """A spinner the console can print: legacy Windows code pages lack braille glyphs."""
    return "dots" if out.encoding.lower().startswith("utf") else "line"


def fail(message: str, code: int = 1) -> typer.Exit:
    """Print an error and return the Exit to raise."""
    console().print(f"[red]{message}[/red]")
    return typer.Exit(code=code)


def _version_callback(value: bool) -> None:
    """Print the version and exit when `--version` is passed."""
    if value:
        typer.echo(f"loglens {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """LogLens: AI-powered log investigation."""
