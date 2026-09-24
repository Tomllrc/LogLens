"""Command-line interface for LogLens."""

from pathlib import Path
from typing import Annotated

import typer

from loglens import __version__

app = typer.Typer(
    name="loglens",
    help="Turn any log into structured events, recurring issues, and cited answers.",
    no_args_is_help=True,
    add_completion=False,
)


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


@app.command()
def parse(
    paths: Annotated[
        list[Path],
        typer.Argument(help="Log files, folders, or archives to parse."),
    ],
) -> None:
    """Parse logs into the unified event table (not implemented yet)."""
    typer.echo("`loglens parse` is not implemented yet.", err=True)
    raise typer.Exit(code=1)
