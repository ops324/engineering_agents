"""Score a run's trajectory, and test what a design proposal did to it."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import List, Optional

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
from scenario.ssos_eclss_loop.scorecard import score_run
from scenario.ssos_eclss_loop.trajectory_metrics import (
    inventory_metrics,
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
    app.command("scorecard")(scorecard)


def _resolve_run(run: str, root: Path) -> Path:
    candidate = Path(run)
    return candidate if candidate.is_dir() else root / run


def _bar_moved_by(run_dir: Path) -> Optional[List[str]]:
    """What moved the bar this run would be scored against. [] = nothing.

    None means the run predates the record and cannot say.

    Two earlier versions of this check were too narrow, each in the same way --
    picking one route to the bar and assuming it was the only one.
    ``apply_proposals_path`` missed ``--set thresholds.*``; watching
    ``thresholds`` alone missed ``plant_sim.survival.bands.*``, which an audit
    used to take a run from 0/4 crew to 4/4 and 29.4 to 78.1 of 90 with the flag
    still reading clean. The run now records every part it moved, decided
    against scenario.yaml before its first step.
    """
    try:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["summary.json is unreadable"]
    moved = summary.get("scoring_bar_modified")
    if moved is not None:
        return [str(m) for m in moved]
    return None if summary.get("apply_proposals_path") is None else ["applied proposals"]


def _operating_point_moved_by(run_dir: Path) -> Optional[List[str]]:
    """Which parts of the operating point the run changed. [] = none.

    None means the run predates the record. Distinct from the bar: a clean bar
    says the score was not gamed, not that the scenario was the published one.
    """
    try:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    changed = summary.get("operating_point_modified")
    return None if changed is None else [str(c) for c in changed]


def _habitat_for(habitat_volume_m3: Optional[float]) -> Habitat:
    """The habitat to score in: the caller's volume, or the scenario's.

    Every partial pressure downstream is ``nRT/V``, so a volume that is not a
    positive real number does not produce a wrong reading -- it produces a
    reading that means nothing while still being printed under the name of
    NASA-STD-3001. An audit (2026-08-29, EXP-022) scored a run with
    ``--habitat-volume-m3=-5`` and got ``B 資源余裕 20.0 / 20`` on a card
    headed ``NASA-STD-3001 at -5 m3``: at negative volume the CO2 band comes
    out negative, every peak is "below" it, and the margin axis pays full
    marks. The same audit found ``--habitat-volume-m3 0`` silently scored at
    388 -- the old test was ``if habitat_volume_m3``, and 0.0 is falsy, so the
    one value that cannot be a habitat was the one that looked like no request
    at all.
    """
    if habitat_volume_m3 is None:
        return SCENARIO_HABITAT
    volume = float(habitat_volume_m3)
    if not math.isfinite(volume) or volume <= 0.0:
        raise typer.BadParameter(
            f"--habitat-volume-m3 must be a positive volume, got {volume:g}. "
            "Partial pressure is nRT/V; there is no reading to take in a "
            "cabin of that size."
        )
    return Habitat(volume_m3=volume)


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
        return from_reference_limits(_habitat_for(habitat_volume_m3))
    baseline_dir = _resolve_run(str(baseline), root)
    summary_path = baseline_dir / "summary.json"
    if not summary_path.is_file():
        raise typer.BadParameter(f"no summary.json under {baseline_dir}")
    # A frozen baseline is only sound while the baseline itself did not draw the
    # line. Pointing --baseline at a run that moved its own bar launders that
    # bar into every run scored against it -- including, when the baseline is
    # the run under test, the case this function's docstring forbids.
    moved = _bar_moved_by(baseline_dir)
    if moved:
        raise typer.BadParameter(
            f"baseline {baseline_dir.name} moved its own bar ({', '.join(moved)}); "
            "freezing it would score every run against a line that run drew"
        )
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
    # O2 and water carry no standard here, and saying only that leaves the two
    # axes invisible -- EXP-011 produced a run with the best cabin CO2 of ten
    # and the fewest survivors, three taken by O2 dwell while this section read
    # "not scored". The figures below are against the run's own survival bands,
    # which is a house measure and not a compliance claim.
    for axis, reason in metrics["not_scored"].items():
        typer.echo(f"  not scored against a standard: {axis} — {reason}")
    bands = _survival_bands(run_dir)
    if bands:
        try:
            # The habitat the yardstick was built with, so PIO2 and ppCO2 are
            # taken in the same volume. Absent when scoring off a frozen
            # baseline, and then O2 stays a house measure rather than
            # borrowing a volume nobody chose.
            scored_habitat = (
                _habitat_for(habitat_volume_m3)
                if habitat_volume_m3 is not None
                else None
            )
            inventory = inventory_metrics(run_dir, bands, scored_habitat)
        except (NotScorable, TelemetryUnreadable, KeyError):
            inventory = None
        if inventory is not None:
            o2, water = inventory["o2"], inventory["water"]
            pio2 = o2.get("pio2")
            if pio2 is None and o2.get("pio2_reason"):
                # Saying nothing here is how an axis disappears. The frozen
                # baseline path has no habitat, so PIO2 cannot be taken.
                typer.echo(
                    f"  not scored against a standard: o2 — {o2['pio2_reason']}"
                )
            if pio2:
                typer.echo(
                    f"  cabin O2 against [V2 6003], as PIO2 "
                    f"(total pressure assumed {pio2['assumed_total_pressure_mmhg']:.0f} mmHg)\n"
                    f"    min {pio2['min_mmhg']:.4g} mmHg   terminal {pio2['terminal_mmhg']:.4g} mmHg"
                )
                for label, band in pio2["bands"].items():
                    typer.echo(
                        f"    {label:<34} floor {band['floor_pio2_mmhg']:g} mmHg = "
                        f"{band['floor_kg']:.4g} kg\n"
                        f"      steps below {band['steps_below']:<4} longest "
                        f"{band['longest_streak_below']:<4} deficit "
                        f"{band['deficit_integral']:.4g} kg*steps"
                        f"  terminal margin {band['terminal_margin_kg']:+.4g} kg\n"
                        f"      from {band['origin']}"
                    )
            typer.echo(
                "  against the run's own survival bands (house measure, not a standard):\n"
                f"    o2    low {o2['band_low_kg']:.4g} kg   min {o2['min_kg']:.4g} kg"
                f"   steps below {o2['steps_below']:<4} longest {o2['longest_streak_below']:<4}"
                f" deficit {o2['deficit_integral']:.4g} kg*steps\n"
                f"    water low {water['band_low_l']:.4g} L   min {water['min_l']:.4g} L"
                f"   steps below {water['steps_below']:<4} longest {water['longest_streak_below']:<4}"
                f" deficit {water['deficit_integral']:.4g} L*steps"
            )
            # Water carries a sourced allocation as of [V2 6109]; the house
            # band above stays because it is what attrition actually reads.
            if water.get("allocation_l_per_day") is not None:
                typer.echo(
                    f"  potable water reserve, in crew-days at the standard allocation\n"
                    f"    min {water['min_crew_days']:.4g} crew-days"
                    f"   terminal {water['terminal_crew_days']:.4g} crew-days"
                    f"   ({water['allocation_l_per_day']:.4g} L/day)\n"
                    f"    from {water['allocation_origin']}\n"
                    f"    the standard sets no reserve horizon, so this is reported, not graded"
                )
    raise typer.Exit(exit_codes.SUCCESS)


def _survival_bands(run_dir: Path) -> Optional[dict]:
    """The edges attrition read, from the run's own summary.

    ``survival_bands`` since they were split from the operational alarms;
    ``thresholds`` for runs recorded before that, where the two were one thing.
    """
    path = Path(run_dir) / "summary.json"
    if not path.is_file():
        return None
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    bands = summary.get("survival_bands") or summary.get("thresholds")
    return dict(bands) if isinstance(bands, dict) else None


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
    yardstick = from_reference_limits(_habitat_for(habitat_volume_m3))
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




def scorecard(
    run: str = typer.Argument(..., help="Run id under the results root, or a path to a run."),
    results_root: Optional[Path] = typer.Option(
        None, "--results-root", help="Override results base directory."
    ),
    habitat_volume_m3: Optional[float] = typer.Option(
        None, "--habitat-volume-m3", help="Score CO2 against the standard in this volume."
    ),
    yardstick: str = typer.Option(
        "standard",
        "--yardstick",
        help=(
            "standard = NASA-STD-3001 at the scenario habitat (default). "
            "run = the run's own thresholds -- what EXP-013..016 published. "
            "Refused for runs that applied proposals."
        ),
    ),
    write: bool = typer.Option(False, "--write", help="Write scorecard.json into the run."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """The project scorecard's outputs, and points only where it defines a formula.

    The scorecard fixes actor残存 = 50 × remaining ÷ initial and names the
    quantities for A-D without saying what they are worth. Those axes come back
    unscored on purpose: putting a curve here would define the criterion in code
    instead of in the document.

    Two yardsticks give different totals for the same run (82.230 vs 84.35 for the
    v5 rule arm), and EXP-014 mixed them once. Which one was used is printed, and
    recorded in the JSON, so a number can never be read without its bar.
    """
    if yardstick not in ("standard", "run"):
        raise typer.BadParameter("--yardstick must be 'standard' or 'run'")
    if yardstick == "run" and habitat_volume_m3 is not None:
        raise typer.BadParameter(
            "--yardstick run scores against the run's own thresholds, "
            "so --habitat-volume-m3 has nothing to act on"
        )
    root = Path(results_root) if results_root else default_results_root()
    run_dir = _resolve_run(run, root)
    moved = _bar_moved_by(run_dir)
    # Both yardsticks are checked, because they are not blocked by the same
    # thing. Under ``standard`` CO2 is scored against NASA at a habitat volume,
    # so a moved ``thresholds`` no longer decides anything -- but
    # ``plant_sim.survival`` still sets attrition, which is the 50-point axis
    # baked into the run, and the o2/water bands ``inventory_metrics`` reads
    # under either yardstick. The refusal below used to name ``--yardstick
    # standard`` as the way out while that path never ran this check at all
    # (EXP-021): the escape hatch it pointed at was the unguarded one.
    survival_moved = [m for m in (moved or []) if m.startswith("plant_sim.survival")]
    blocking = list(moved or []) if yardstick == "run" else survival_moved
    if blocking:
        remedy = (
            "Score it with --yardstick standard."
            if yardstick == "run" and not survival_moved
            else "No yardstick can score it: attrition and the o2/water bands "
            "came from the moved values. Re-run without moving them."
        )
        print_error(
            f"this run moved its own bar ({', '.join(blocking)}), so scoring it "
            "against that bar grades it against a line it drew for itself "
            f"(EXP-010). {remedy}"
        )
        raise typer.Exit(exit_codes.USER_ERROR)
    habitat = None if yardstick == "run" else _habitat_for(habitat_volume_m3)
    try:
        card = score_run(run_dir, habitat=habitat)
    except (NotScorable, TelemetryUnreadable) as exc:
        print_error(str(exc))
        raise typer.Exit(NOT_SCORABLE_EXIT) from exc
    card["yardstick"] = (
        "run's own thresholds"
        if yardstick == "run"
        else f"NASA-STD-3001 at {habitat.volume_m3:g} m3"
    )
    # On both yardsticks, not only ``run``. A run from before the guard landed
    # cannot say whether it moved its own bar, and ``plant_sim.survival`` decides
    # attrition under either yardstick -- so the caveat belongs on both. It was
    # printed on ``run`` alone, which left the default (``standard``) the one
    # place where an unverifiable run looked like a verified one (EXP-022).
    if moved is None:
        card["yardstick"] += " (predates scoring_bar_modified -- unverified)"
    # Both belong on the scoring artifact, not only in summary.json. An audit
    # (EXP-021) found scorecard.json carried neither, so a card could be read --
    # or pasted into a comparison -- with no way to ask whether the run had
    # moved its bar or simply run an easier scenario.
    card["scoring_bar_modified"] = moved
    card["operating_point_modified"] = _operating_point_moved_by(run_dir)
    if write:
        (run_dir / "scorecard.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if json_output:
        typer.echo(json.dumps(card, ensure_ascii=False, indent=2))
        raise typer.Exit(exit_codes.SUCCESS)

    gate = card["physics_gate"]
    typer.echo(f"{card['run_id']}  actor_mode={card['actor_mode']}  gate={gate['verdict']}")
    typer.echo(f"  yardstick: {card['yardstick']}")
    # Printed, not just recorded: the audit's 90.000 no-op was read off this
    # very output, and nothing on it said the scenario had been made easier.
    #
    # The bar is printed on the same terms. Under ``standard`` a moved
    # ``thresholds`` does not block -- CO2 is scored against NASA at a habitat
    # volume, so the run's own line no longer decides that axis -- but it still
    # decides the health status the occupant-survival axis reads. An audit
    # (2026-08-29, EXP-022) moved ``co2_storage_{high,critical}_kg`` to 99999
    # and took the same physics, with the same two occupants lost, from 48.93
    # to 52.77 of 90: the alarm simply never rang, so no step counted as
    # critical. It was refused under ``run`` and silent here, which is the
    # weaker guard sitting on the default yardstick.
    if card.get("bands_verified") is None:
        typer.echo("    — 帯を照合する scenario_config.yaml が無く、summary.json の自己申告のまま")
    bar_moved = card.get("scoring_bar_modified")
    if bar_moved:
        typer.echo(f"  ⚠ 採点の基準を変更: {', '.join(bar_moved)}")
        typer.echo("    — この物差しでは拒否されないが、run 自身が引いた線が残っている")
    operating_point = card.get("operating_point_modified")
    if operating_point:
        typer.echo(f"  ⚠ 運用点を変更: {', '.join(operating_point)}")
        typer.echo("    — 既定の運用点で走った run と直接比較できない")
    if not card["scorable"]:
        typer.echo(
            f"  検証無効: {', '.join(gate['failed_checks'])}"
            "  — 物理ゲート不合格のランは採点しない"
        )
        raise typer.Exit(exit_codes.SUCCESS)
    axes = card["axes"]
    a = axes["actor_remaining"]
    typer.echo(
        f"  actor残存      {a['points']:.1f} / {a['max']}"
        f"   ({a['actor_remaining']} / {a['actor_initial']} 人)"
    )
    for key, label in (("A_environment", "A 生存環境"), ("B_margin", "B 資源余裕"),
                       ("C_judgement", "C 判断"), ("D_response", "D 応答")):
        axis = axes[key]
        if not axis.get("applicable", True):
            typer.echo(f"  {label:<12}   —  / {axis['max']}   (actor操作なしのため対象外)")
            continue
        points = axis.get("points")
        shown = f"{points:.1f}" if points is not None else "  ? "
        typer.echo(
            f"  {label:<12} {shown:>6} / {axis['max']}"
            f"   ({axis.get('points_policy', '点数化式が未定義')})"
        )
        if key == "A_environment":
            typer.echo(f"    co2 exposure {axis['co2_exposure_integral']}")
            typer.echo(
                f"    o2 deficit {axis['o2_deficit_integral']}"
                f"   water deficit {axis['water_deficit_integral']}"
            )
        elif key == "C_judgement":
            for subsystem, detail in (axis["response_latency_steps"] or {}).items():
                latency = detail.get("latency_steps")
                shown = f"{latency} step" if latency is not None else detail.get("reason")
                typer.echo(f"    {subsystem:<20} {shown}")
        elif key == "D_response":
            for subsystem, detail in (axis["requested_processed_ratio"] or {}).items():
                typer.echo(
                    f"    {subsystem:<20} 要求 {detail['requested']:.4g} → 実処理"
                    f" {detail['processed']:.4g}  比 {detail['ratio']}"
                )
    total = card["total"]
    points = total.get("points")
    shown = f"{points:.1f}" if points is not None else "  — "
    typer.echo(f"  合計         {shown:>6} / {total['applicable_max']}   ({total['note']})")
    if total["applicable_max"] != 100:
        # "100点満点ランと総合点を直接比較しない"
        typer.echo("    ※ actor操作なしのため満点が異なる。100点満点のランと直接比較しないこと")
    raise typer.Exit(exit_codes.SUCCESS)
