"""Physics gate — decide whether a run is admissible evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

from scenario.jobs.resolve import default_results_root
from scenario.ssos_eclss_loop.physics_gate import (
    FAIL,
    SKIPPED,
    TelemetryUnreadable,
    evaluate_physics_gate,
    write_physics_gate,
)
from tools.cli import exit_codes
from tools.cli.output import print_error

#: A failed run is void, not low-scoring, so the exit code has to be
#: distinguishable from "the command could not run" -- and from
#: ENVIRONMENT_ERROR (3), which a CI step would otherwise read as the same
#: thing as physics not closing.
GATE_FAILED_EXIT = 4


def register(app: typer.Typer) -> None:
    app.command("gate")(gate)


def gate(
    run: Optional[str] = typer.Argument(
        None, help="Run id under the results root, or a path to a run directory."
    ),
    all_runs: bool = typer.Option(
        False, "--all", help="Check every run under the results root."
    ),
    results_root: Optional[Path] = typer.Option(
        None, "--results-root", help="Override results base directory."
    ),
    write: bool = typer.Option(
        False, "--write", help="Write physics_gate.json into each run directory."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the full result as JSON."),
) -> None:
    """Check that a run's recorded trajectory could have happened.

    Exit 0 when every run passes, 4 when any run fails the gate, 2 on a usage
    error. A gate failure is not a low score -- nothing downstream should score,
    average, or cite the run.
    """
    root = Path(results_root) if results_root else default_results_root()

    if all_runs:
        targets = sorted(
            entry for entry in root.iterdir()
            if entry.is_dir() and (entry / "telemetry.jsonl").is_file()
        ) if root.exists() else []
        if not targets:
            print_error("No runs with telemetry found.", hint=f"Looked under: {root}")
            raise typer.Exit(exit_codes.USER_ERROR)
    elif run is None:
        print_error("Give a run id or --all.", hint="Try: ea gate <RUN_ID>")
        raise typer.Exit(exit_codes.USER_ERROR)
    else:
        candidate = Path(run)
        target = candidate if candidate.is_dir() else root / run
        if not (target / "telemetry.jsonl").is_file():
            print_error(f"No telemetry.jsonl in {target}", hint=f"Looked under: {root}")
            raise typer.Exit(exit_codes.USER_ERROR)
        targets = [target]

    results = []
    for target in targets:
        try:
            result = evaluate_physics_gate(target)
        except TelemetryUnreadable as exc:
            print_error(f"{target.name}: {exc}")
            raise typer.Exit(exit_codes.USER_ERROR) from exc
        if write:
            write_physics_gate(target / "physics_gate.json", result)
        results.append(result)

    if json_output:
        payload = results[0] if len(results) == 1 else results
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    elif len(results) == 1:
        _print_one(results[0])
    else:
        _print_many(results)

    failed = [r for r in results if r["verdict"] == FAIL]
    raise typer.Exit(GATE_FAILED_EXIT if failed else exit_codes.SUCCESS)


def _print_one(result: dict) -> None:
    typer.echo(f"{result['run_id']}  verdict={result['verdict']}  form={result['form']}  steps={result['steps']}")
    for check in result["checks"]:
        typer.echo(f"  {check['status']:<8} {check['name']:<32} {check['detail']}")
    if result["totals_not_recorded"]:
        typer.echo(
            f"  note: {len(result['totals_not_recorded'])} cumulative total(s) "
            "not recorded by this run; the ledgers could not be closed."
        )


def _print_many(results: List[dict]) -> None:
    passed = sum(1 for r in results if r["verdict"] != FAIL)
    failed = [r for r in results if r["verdict"] == FAIL]
    full = sum(1 for r in results if r["form"] == "full")
    typer.echo(f"{len(results)} runs: {passed} pass, {len(failed)} fail")
    typer.echo(f"  full form: {full}   retroactive form: {len(results) - full}")
    for result in failed:
        typer.echo(f"  FAIL {result['run_id']}: {', '.join(result['failed_checks'])}")
