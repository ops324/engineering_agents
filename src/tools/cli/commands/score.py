"""Score a run's trajectory, and test what a design proposal did to it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from scenario.jobs.resolve import default_results_root
from scenario.ssos_eclss_loop.physics_gate import TelemetryUnreadable
from scenario.ssos_eclss_loop.proposal_evaluation import (
    ProposalEvaluationError,
    evaluate_proposal,
    write_proposal_evaluation,
)
from scenario.ssos_eclss_loop.reference_limits import (
    SCENARIO_HABITAT,
    Habitat,
    ppco2_mmhg,
)
from scenario.ssos_eclss_loop.trajectory_metrics import (
    NotScorable,
    Yardstick,
    from_frozen_baseline,
    from_reference_limits,
    trajectory_metrics,
    write_trajectory_metrics,
)
from tools.cli import exit_codes
from tools.cli.output import print_error

#: Shared with `ea gate`: a run that cannot be scored is not a low score, and a
#: CI step must tell it from a broken invocation. ENVIRONMENT_ERROR owns 3.
NOT_SCORABLE_EXIT = 4


def register(app: typer.Typer) -> None:
    app.command("score")(score)
    app.command("evaluate")(evaluate)


def _resolve_run(run: str, root: Path) -> Path:
    candidate = Path(run)
    return candidate if candidate.is_dir() else root / run


def _build_yardstick(
    *, baseline: Optional[str], habitat_volume_m3: Optional[float], root: Path
) -> Yardstick:
    """A yardstick has to be named. There is deliberately no default.

    Scoring a run against thresholds read out of that same run is the one thing
    this must not do: `--apply-proposals` can write four `thresholds.*` keys and
    those are the keys health scoring reads, so a run's own bar may be a bar its
    own proposal moved.
    """
    if baseline is not None and habitat_volume_m3 is not None:
        raise typer.BadParameter(
            "give at most one of --baseline RUN (freeze that run's thresholds) "
            "or --habitat-volume M3"
        )
    if baseline is None:
        # NASA-STD-3001 at the scenario habitat is the default because it is
        # the bar nothing in this repository can edit. A frozen baseline is
        # sound too, but only as long as that baseline predates every proposal
        # in the chain -- and this is an iterative loop.
        habitat = (
            Habitat(volume_m3=float(habitat_volume_m3))
            if habitat_volume_m3 is not None
            else SCENARIO_HABITAT
        )
        return from_reference_limits(habitat)
    baseline_dir = _resolve_run(str(baseline), root)
    summary_path = baseline_dir / "summary.json"
    if not summary_path.is_file():
        raise typer.BadParameter(f"no summary.json under {baseline_dir}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return from_frozen_baseline(summary.get("thresholds") or {}, baseline_run_id=baseline_dir.name)


def score(
    run: str = typer.Argument(..., help="Run id under the results root, or a path."),
    baseline: Optional[str] = typer.Option(
        None, "--baseline", help="Freeze the yardstick from this run's thresholds."
    ),
    habitat_volume_m3: Optional[float] = typer.Option(
        None,
        "--habitat-volume",
        help="Cabin gas volume in m3; scores against NASA-STD-3001 instead.",
    ),
    band: Optional[str] = typer.Option(None, "--band", help="Show only this band."),
    results_root: Optional[Path] = typer.Option(None, "--results-root"),
    no_gate: bool = typer.Option(False, "--no-gate", help="Skip the physics gate check."),
    write: bool = typer.Option(False, "--write", help="Write trajectory_metrics.json."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """How deep and for how long, against a bar the run did not set.

    Exit 4 when the run cannot be scored -- failed physics, no telemetry --
    which is different from scoring badly.
    """
    root = Path(results_root) if results_root else default_results_root()
    run_dir = _resolve_run(run, root)
    try:
        # Inside the handler: from_frozen_baseline raises NotScorable when the
        # named baseline has no thresholds, and the contract above promises 4.
        yardstick = _build_yardstick(
            baseline=baseline, habitat_volume_m3=habitat_volume_m3, root=root
        )
        metrics = trajectory_metrics(run_dir, yardstick, require_gate=not no_gate)
    except (NotScorable, TelemetryUnreadable) as exc:
        print_error(str(exc))
        raise typer.Exit(NOT_SCORABLE_EXIT) from exc

    if write:
        write_trajectory_metrics(run_dir / "trajectory_metrics.json", metrics)
    if json_output:
        typer.echo(json.dumps(metrics, ensure_ascii=False, indent=2))
        raise typer.Exit(exit_codes.SUCCESS)

    co2 = metrics["co2"]
    if band and band not in co2["bands"]:
        # Printing nothing and exiting 0 reads as "nothing above threshold".
        print_error(
            f"unknown band {band!r}",
            hint=f"this yardstick has: {', '.join(co2['bands'])}",
        )
        raise typer.Exit(exit_codes.USER_ERROR)
    typer.echo(
        f"{metrics['run_id']}  steps={metrics['steps']}  "
        f"yardstick={metrics['yardstick']['source']}"
    )
    habitat_detail = (metrics["yardstick"].get("detail") or {}).get("habitat") or {}
    volume = habitat_detail.get("volume_m3")
    if volume:
        # The unit the standard is written in. Reporting only kg leaves the
        # reader to do the conversion that the habitat exists to perform.
        habitat = Habitat(volume_m3=float(volume))
        typer.echo(
            f"  peak {co2['peak_kg']:.4g} kg = {ppco2_mmhg(co2['peak_kg'], habitat):.2f} mmHg"
            f"   terminal {co2['terminal_kg']:.4g} kg"
            f" = {ppco2_mmhg(co2['terminal_kg'], habitat):.2f} mmHg"
        )
        typer.echo(f"  habitat {volume:g} m3 — {habitat_detail.get('source', 'unstated')}")
    else:
        typer.echo(f"  peak {co2['peak_kg']:.4g} kg   terminal {co2['terminal_kg']:.4g} kg")
    for name, stats in co2["bands"].items():
        if band and name != band:
            continue
        typer.echo(
            f"  {name:<34} limit {stats['threshold_kg']:.4g} kg\n"
            f"    steps above {stats['steps_above']:<5} "
            f"longest {stats['longest_streak']:<5} "
            f"exposure {stats['exposure_integral_kg_steps']:.4g} kg*steps  "
            f"terminal margin {stats['terminal_margin_kg']:+.4g} kg\n"
            f"    from {stats['origin']}"
        )
    for axis, reason in metrics["not_scored"].items():
        typer.echo(f"  not scored: {axis} — {reason}")
    raise typer.Exit(exit_codes.SUCCESS)


def evaluate(
    run: str = typer.Argument(..., help="Baseline run whose design_proposals.json to test."),
    repeats: int = typer.Option(
        1, "--repeats", help="Control/treated pairs. One is enough on a deterministic arm."
    ),
    habitat_volume_m3: Optional[float] = typer.Option(
        None,
        "--habitat-volume",
        help="Score against NASA-STD-3001 instead of the baseline's own frozen thresholds.",
    ),
    band: Optional[str] = typer.Option(None, "--band", help="Default: the most stringent band."),
    results_root: Optional[Path] = typer.Option(None, "--results-root"),
    write: bool = typer.Option(False, "--write", help="Write proposal_evaluation.json."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Re-run the scenario with and without the proposal, and report the difference.

    The baseline's thresholds are frozen before either arm runs, so a proposal
    cannot improve its own score by moving the bar it is measured against.
    """
    root = Path(results_root) if results_root else default_results_root()
    run_dir = _resolve_run(run, root)
    yardstick = from_reference_limits(
        Habitat(volume_m3=float(habitat_volume_m3))
        if habitat_volume_m3 is not None
        else SCENARIO_HABITAT
    )
    # Checked before either arm runs: an unknown band otherwise raises KeyError
    # after both simulations have already been paid for.
    known = {b.name for b in yardstick.bands}
    if band is not None and band not in known:
        print_error(f"unknown band {band!r}", hint=f"available: {', '.join(sorted(known))}")
        raise typer.Exit(exit_codes.USER_ERROR)
    try:
        result = evaluate_proposal(
            run_dir,
            yardstick=yardstick,
            repeats=repeats,
            results_root=root,
            band=band,
        )
    except (ProposalEvaluationError, NotScorable, TelemetryUnreadable) as exc:
        print_error(str(exc))
        raise typer.Exit(NOT_SCORABLE_EXIT) from exc

    if write:
        write_proposal_evaluation(run_dir / "proposal_evaluation.json", result)
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        raise typer.Exit(exit_codes.SUCCESS)

    typer.echo(
        f"{result['baseline_run_id']}  mode={result['agents_mode']}  "
        f"deterministic={result['deterministic']}  pairs={len(result['pairs'])}  "
        f"band={result['band']}  yardstick={result['yardstick']['source']}"
    )
    for change in result["proposal"]["changes"]:
        typer.echo(f"  proposes {change['change_kind']}: {json.dumps(change.get('payload'), ensure_ascii=False)}")
    if result["yardstick_changes"]:
        typer.echo(
            f"  note: this proposal moves {', '.join(result['yardstick_changes'])} — "
            f"an alarm setting, not a plant. Both arms are still scored on the "
            f"baseline's bar."
        )
    typer.echo(f"  {'metric':<30}{'control':>12}{'treated':>12}{'delta':>12}")
    pair = result["pairs"][0]
    for metric, stats in result["aggregate"].items():
        typer.echo(
            f"  {metric:<30}{pair['control'][metric]:>12.4g}"
            f"{pair['treated'][metric]:>12.4g}{stats['delta']['mean']:>+12.4g}"
        )
    verdict = result["verdict"]
    typer.echo(f"  verdict: {verdict['label']} — {verdict['reason']}")
    raise typer.Exit(exit_codes.SUCCESS)
