"""Typer application entry for Engineering Agents CLI."""

from __future__ import annotations

import typer

from tools import __version__
from tools.cli.commands import doctor, gate, job, results, run, scenarios

app = typer.Typer(
    name="ea",
    help="Engineering Agents simulation CLI.",
    no_args_is_help=True,
    add_completion=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Engineering Agents — design and verification simulation CLI."""


run.register(app)
scenarios.register(app)
results.register(app)
doctor.register(app)
job.register(app)
gate.register(app)
