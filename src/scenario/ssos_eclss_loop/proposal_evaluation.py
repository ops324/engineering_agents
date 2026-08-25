"""Measure what a design proposal did, by running the scenario again without it.

A proposal in ``design_proposals.json`` is a claim: an agent read the run it
just lived through and says a parameter should change. Nothing tests the claim.
``--apply-proposals`` applies it to the next run and the two summaries are then
read side by side by hand.

This closes that with a paired re-run, and three decisions in it are
load-bearing.

**The control is re-run, never read off the baseline.** Comparing a treated run
against the summary that *generated* the proposal folds two differences into
one number: what the proposal did, and how much the pipeline moves on its own
between two runs of the same config. On the rule arm that second term is zero
and re-running is merely cheap insurance. On the LLM arm it is 26% and would
otherwise be attributed to the proposal.

**Both arms are scored on the baseline's yardstick, frozen before either arm
exists.** Four of the seven targets ``set_parameter`` may write are
``thresholds.*``, and those are the keys health scoring reads -- so a proposal
can lower the bar it is about to be measured against. A real one does:
``product_water_low_l`` 50.0 -> 40.0 on a run finishing at 44.07 L, turning
``warning`` into ``safe`` with no physical change. Scoring both arms on the
inherited bar makes that move worth exactly nothing.

**Replication is required where it is required, and refused where it is not.**
``labeled_rule_base`` produces byte-identical *telemetry* run to run (summary
timestamps and paths differ), so one pair settles it. ``llm`` does not
reproduce: five runs of one identical config at seed 101, temperature 0.0 gave
peak CO2 of 2.01-3.49 kg, two ending ``warning`` and three ``critical``.

Whether the seed helps at all is *not* established. Pooled within-seed spread
is 0.82x between-seed spread (n=5 and n=4 against n=10), but F = 1.48 on (9,7)
df, p ~ 0.6 -- consistent with the seed fixing nothing and equally consistent
with it fixing a good deal. The two sets also share two runs. So the honest
statement is that a single LLM pair cannot separate a proposal from
run-to-run variation, not that pairing is known to be worthless. This module
returns the numbers and withholds the label.
"""

from __future__ import annotations

import copy
import json
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from scenario.jobs.executor import execute_run
from scenario.jobs.spec import RunSpec
from scenario.ssos_eclss_loop.physics_gate import evaluate_physics_gate, gate_passed
from scenario.ssos_eclss_loop.survival import resolve_survival_bands
from scenario.ssos_eclss_loop.survival_replay import replay_survival
from scenario.ssos_eclss_loop.trajectory_metrics import (
    inventory_metrics,
    NotScorable,
    Yardstick,
    from_frozen_baseline,
    trajectory_metrics,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "0.1.0"
SCENARIO = "ssos_eclss_loop"

#: Backends whose physics can be replayed as a matched pair. ``ros2`` runs
#: against a live SSOS in real time with no step synchronisation (BL-007), so
#: two runs of it are not a pair and their difference is not attributable.
REPRODUCIBLE_BACKENDS = frozenset({"mock", "plant_sim"})

#: Agent modes with no stochastic element. Verified, not assumed: two 20-step
#: labeled runs at one seed produce byte-identical telemetry.
DETERMINISTIC_MODES = frozenset({"none", "labeled_rule_base"})

#: Observed spread of peak_co2 across same-seed llm repeats: SD/mean = 0.27 on
#: n=5 at seed 101. Quoted as an order of magnitude, not an estimate -- the 95%
#: CI on a CV from n=5 runs roughly 16% to 76%, and the underlying outcome is
#: bimodal rather than a mean with a spread.
OBSERVED_LLM_SPREAD = 0.27

#: Below this, a stochastic arm gets its numbers reported and no verdict.
MIN_REPEATS_FOR_A_STOCHASTIC_VERDICT = 3

#: Metrics compared between arms. Each is read off the trajectory under the
#: held yardstick, so none of them can be improved by moving a threshold.
#: ``peak_kg`` is band-independent and was computed but never compared. Without
#: it, a spike to 31 kg and a plateau at 3 kg tie on every other figure: same
#: steps above, same integral, same streak, same terminal margin.
#: ``o2_steps_below`` joins these because CO2 alone can rank a run backwards:
#: EXP-011's best cabin CO2 of ten runs was also its worst survival, three
#: occupants taken by O2 dwell while the air stayed clean. O2 and water carry no
#: standard here -- reference_limits records both as named gaps -- so they are
#: scored against the frozen survival bands, which is where the deaths come from.
LOWER_IS_BETTER = (
    "steps_above", "exposure_integral_kg_steps", "longest_streak", "peak_kg",
    "o2_steps_below",
)
#: ``crew_remaining_frozen`` is the occupant count this trajectory would have
#: kept under the *baseline's* bands, not the count the run reported. The run's
#: own figure is unusable for comparison: attrition fires on the same
#: thresholds.* keys a proposal may move, so raising an alarm above the
#: trajectory deletes the deaths it caused (EXP-010). Present only when the
#: baseline enabled survival and recorded a crew.
HIGHER_IS_BETTER = ("terminal_margin_kg", "crew_remaining_frozen", "o2_min_kg", "water_min_l")

#: The figures ``_band_figures`` reads off one band of the yardstick.
#: ``crew_remaining_frozen`` is graded like these and carries a direction like
#: these, but it comes from replaying the dwell policy, not from the band, so
#: it is not looked for here.
BAND_FIGURES = (
    "steps_above", "exposure_integral_kg_steps", "longest_streak", "peak_kg",
    "terminal_margin_kg",
)


class ProposalEvaluationError(RuntimeError):
    """The evaluation cannot be run, or could not be trusted if it were."""


@dataclass(frozen=True)
class Baseline:
    run_dir: Path
    summary: Dict[str, Any]
    config: Dict[str, Any]
    proposals: Dict[str, Any]
    proposals_path: Path

    @property
    def agents_mode(self) -> str:
        return str(self.summary.get("agents_mode", "none"))

    @property
    def is_deterministic(self) -> bool:
        return self.agents_mode in DETERMINISTIC_MODES


def load_baseline(run_dir: Path) -> Baseline:
    """Read what a re-run needs, and refuse what cannot be replayed."""
    run_dir = Path(run_dir)
    paths = {
        name: run_dir / name
        for name in ("summary.json", "scenario_config.yaml", "design_proposals.json")
    }
    for name, path in paths.items():
        if not path.is_file():
            raise ProposalEvaluationError(f"{run_dir} is missing {name}")

    summary = json.loads(paths["summary.json"].read_text(encoding="utf-8"))
    if summary.get("scenario") != SCENARIO:
        raise ProposalEvaluationError(
            f"expected scenario {SCENARIO!r}, got {summary.get('scenario')!r}"
        )
    backend = summary.get("backend")
    if backend not in REPRODUCIBLE_BACKENDS:
        raise ProposalEvaluationError(
            f"backend {backend!r} cannot be replayed as a matched pair; "
            f"use one of {sorted(REPRODUCIBLE_BACKENDS)}"
        )
    proposals = json.loads(paths["design_proposals.json"].read_text(encoding="utf-8"))
    if not proposals.get("changes"):
        raise ProposalEvaluationError("design_proposals.json proposes no changes")

    return Baseline(
        run_dir=run_dir,
        summary=summary,
        config=yaml.safe_load(paths["scenario_config.yaml"].read_text(encoding="utf-8")) or {},
        proposals=proposals,
        proposals_path=paths["design_proposals.json"],
    )


def yardstick_changes(proposals: Dict[str, Any]) -> List[str]:
    """``thresholds.*`` targets the proposal moves.

    Harmless to the score -- both arms are graded on the inherited bar -- but
    worth naming: a proposal made only of these is proposing a new alarm
    setting, not a new plant, and a reader comparing it against one that
    changed hardware should be able to see the difference.
    """
    moved = []
    for change in proposals.get("changes") or []:
        if change.get("change_kind") != "set_parameter":
            continue
        target = str((change.get("payload") or {}).get("target", ""))
        if target.startswith("thresholds."):
            moved.append(target)
    return moved


def _overrides_for(config: Dict[str, Any], seed: Optional[int]) -> Dict[str, Any]:
    """The baseline's own effective config, optionally re-pointed at one seed.

    Using the resolved config rather than rebuilding from flags is what makes
    the two arms differ in the proposal and nothing else: every override,
    failure injection and agent setting the baseline resolved carries over
    verbatim.
    """
    overrides = copy.deepcopy(config)
    for key in ("name", "description", "output"):
        overrides.pop(key, None)
    if seed is not None:
        simulation = dict(overrides.get("simulation") or {})
        simulation["seed"] = int(seed)
        overrides["simulation"] = simulation
    return overrides


def _run_arm(
    *,
    baseline: Baseline,
    seed: Optional[int],
    run_id: str,
    results_root: Optional[Path],
    treated: bool,
) -> Path:
    spec = RunSpec(
        scenario=SCENARIO,
        overrides=_overrides_for(baseline.config, seed),
        run_id=run_id,
        results_root=results_root,
        seed=seed,
        apply_proposals_path=baseline.proposals_path if treated else None,
    )
    result = execute_run(spec)
    if result.exit_code != 0:
        raise ProposalEvaluationError(f"{run_id} failed: {result.error}")
    run_dir = Path(result.run_dir)
    gate = evaluate_physics_gate(run_dir)
    if not gate_passed(gate):
        raise ProposalEvaluationError(
            f"{run_id} failed the physics gate ({', '.join(gate['failed_checks'])}); "
            f"a void run cannot serve as evidence for or against a proposal"
        )
    return run_dir


def _band_figures(metrics: Dict[str, Any], band: str) -> Dict[str, float]:
    bands = metrics["co2"]["bands"]
    if band not in bands:
        raise ProposalEvaluationError(
            f"unknown band {band!r}; this yardstick has {', '.join(bands)}"
        )
    stats = dict(bands[band])
    stats["peak_kg"] = metrics["co2"]["peak_kg"]
    return {name: float(stats[name]) for name in BAND_FIGURES}


def _summarise(values: Sequence[float]) -> Dict[str, float]:
    out: Dict[str, float] = {"mean": round(statistics.mean(values), 6), "n": len(values)}
    if len(values) > 1:
        out["sd"] = round(statistics.stdev(values), 6)
    return out


def evaluate_proposal(
    baseline_run_dir: Path,
    *,
    yardstick: Optional[Yardstick] = None,
    repeats: int = 1,
    seeds: Optional[Sequence[int]] = None,
    results_root: Optional[Path] = None,
    run_id_prefix: Optional[str] = None,
    band: Optional[str] = None,
) -> Dict[str, Any]:
    """Run control and treated, and report the difference under a held bar."""
    baseline = load_baseline(Path(baseline_run_dir))

    if not baseline.is_deterministic and repeats < MIN_REPEATS_FOR_A_STOCHASTIC_VERDICT:
        logger.warning(
            "agents_mode=%r is stochastic (measured CV %.0f%%); %d repeat(s) will "
            "produce numbers but no verdict.",
            baseline.agents_mode,
            OBSERVED_LLM_SPREAD * 100,
            repeats,
        )

    if yardstick is None:
        # The freeze is only worth something if the baseline's thresholds are
        # the ones the scenario started with. A baseline that was itself run
        # with --apply-proposals has already written a previous proposal's
        # thresholds into its own summary, so freezing them freezes a bar that
        # has moved -- and this is an iterative design loop, so that is the
        # normal case, not an edge one.
        if baseline.summary.get("apply_proposals_path"):
            raise ProposalEvaluationError(
                f"{baseline.run_dir.name} was produced with --apply-proposals "
                f"({baseline.summary['apply_proposals_path']}), so its thresholds "
                f"are a previous proposal's, not the scenario's. Pass an explicit "
                f"yardstick -- from_reference_limits() cannot drift, nothing in "
                f"this repository can edit NASA-STD-3001."
            )
        # The baseline's own thresholds, frozen here, before either arm runs.
        yardstick = from_frozen_baseline(
            baseline.summary.get("thresholds") or baseline.config.get("thresholds") or {},
            baseline_run_id=baseline.run_dir.name,
        )

    # The most stringent band of whichever yardstick is in use. Band names are
    # a property of the yardstick -- "critical" exists on a frozen baseline,
    # "ppCO2 nominal (1-hour average)" on the standard -- so a hardcoded
    # default only works for one of them.
    if band is None:
        band = yardstick.bands[0].name

    # Occupants are graded on the baseline's bands for the same reason the CO2
    # figures are, and with more at stake: the run's own crew_remaining is
    # computed under whatever thresholds were live in that arm, so a proposal
    # that raises an alarm above the trajectory reports a crew it killed as
    # alive (EXP-010). Absent -- and then simply not compared -- when the
    # baseline ran no occupants.
    # The bands attrition read, which are no longer the operational alarms: a
    # run records both, and scoring against the alarms would count shortfalls
    # on edges that never took anyone. Frozen from the baseline, like the CO2
    # yardstick, so the treated arm cannot be graded on edges it moved.
    frozen_bands: Dict[str, Any] = dict(
        baseline.summary.get("survival_bands")
        or resolve_survival_bands(
            baseline.config.get("plant_sim"),
            baseline.summary.get("thresholds") or baseline.config.get("thresholds") or {},
        )
    )
    inventory_scorable = {"o2_storage_low_kg", "product_water_low_l"} <= set(frozen_bands)

    frozen_crew: Optional[Dict[str, Any]] = None
    survival_cfg = dict((baseline.config.get("plant_sim") or {}).get("survival") or {})
    crew_initial = baseline.summary.get("crew_initial")
    if survival_cfg.get("enabled") and crew_initial is not None:
        frozen_crew = {
            "thresholds": frozen_bands,
            "survival": survival_cfg,
            "crew_initial": int(crew_initial),
        }

    prefix = run_id_prefix or f"eval__{baseline.run_dir.name}"
    if seeds is None:
        base_seed = baseline.summary.get("seed")
        seeds = [base_seed] * repeats if base_seed is not None else [None] * repeats

    pairs: List[Dict[str, Any]] = []
    for index, seed in enumerate(seeds):
        tag = f"{prefix}__r{index + 1}"
        control_dir = _run_arm(
            baseline=baseline, seed=seed, run_id=f"{tag}__control",
            results_root=results_root, treated=False,
        )
        treated_dir = _run_arm(
            baseline=baseline, seed=seed, run_id=f"{tag}__treated",
            results_root=results_root, treated=True,
        )
        control = trajectory_metrics(control_dir, yardstick)
        treated = trajectory_metrics(treated_dir, yardstick)
        c_fig, t_fig = _band_figures(control, band), _band_figures(treated, band)
        for run_dir, figures in ((control_dir, c_fig), (treated_dir, t_fig)):
            if inventory_scorable:
                inventory = inventory_metrics(run_dir, frozen_bands)
                figures["o2_min_kg"] = float(inventory["o2"]["min_kg"])
                figures["o2_steps_below"] = float(inventory["o2"]["steps_below"])
                figures["water_min_l"] = float(inventory["water"]["min_l"])
            if frozen_crew is not None:
                figures["crew_remaining_frozen"] = float(
                    replay_survival(
                        run_dir,
                        frozen_crew["thresholds"],
                        frozen_crew["survival"],
                        crew_initial=frozen_crew["crew_initial"],
                        bands_from=baseline.run_dir.name,
                    )["crew_remaining"]
                )
        pairs.append({
            "repeat": index + 1,
            "seed": seed,
            "control": {"run_dir": str(control_dir), **c_fig},
            "treated": {"run_dir": str(treated_dir), **t_fig},
            "delta": {k: round(t_fig[k] - c_fig[k], 6) for k in c_fig},
        })

    aggregate: Dict[str, Any] = {}
    for metric in LOWER_IS_BETTER + HIGHER_IS_BETTER:
        # crew_remaining_frozen is absent on backends without occupant
        # survival; a missing metric is not compared rather than defaulted.
        if metric not in pairs[0]["delta"]:
            continue
        deltas = [p["delta"][metric] for p in pairs]
        direction = -1 if metric in LOWER_IS_BETTER else 1
        improved = sum(1 for d in deltas if d * direction > 0)
        worsened = sum(1 for d in deltas if d * direction < 0)
        aggregate[metric] = {
            "direction": "lower_is_better" if direction < 0 else "higher_is_better",
            "delta": _summarise(deltas),
            "improved": improved,
            "worsened": worsened,
            "unchanged": len(deltas) - improved - worsened,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_run_id": baseline.run_dir.name,
        "baseline_run_dir": str(baseline.run_dir),
        "agents_mode": baseline.agents_mode,
        "backend": baseline.summary.get("backend"),
        "deterministic": baseline.is_deterministic,
        "band": band,
        "yardstick": yardstick.to_dict(),
        "proposal": {
            "proposed_by": baseline.proposals.get("proposed_by"),
            "decision_source": baseline.proposals.get("decision_source"),
            "changes": baseline.proposals.get("changes"),
        },
        # Not a score adjustment; a label. A proposal made only of these is
        # proposing an alarm setting, not a plant.
        "yardstick_changes": yardstick_changes(baseline.proposals),
        "pairs": pairs,
        "aggregate": aggregate,
        "verdict": _verdict(aggregate, pairs, baseline.is_deterministic),
    }


def _verdict(
    aggregate: Dict[str, Any],
    pairs: Sequence[Dict[str, Any]],
    deterministic: bool,
) -> Dict[str, Any]:
    """A direction only where the evidence can carry one.

    Deterministic arms settle on a single pair -- there is nothing for a second
    to disagree with. Stochastic arms need a majority across repeats, and below
    ``MIN_REPEATS_FOR_A_STOCHASTIC_VERDICT`` get their numbers and no label.
    """
    n = len(pairs)
    if not deterministic and n < MIN_REPEATS_FOR_A_STOCHASTIC_VERDICT:
        return {
            "label": "inconclusive",
            "reason": (
                f"agents_mode is stochastic and {n} repeat(s) cannot separate the "
                f"proposal from run-to-run variation (observed spread "
                f"~{OBSERVED_LLM_SPREAD:.0%} on n=5)"
            ),
        }
    needed = 1 if deterministic else n // 2 + 1
    better = [m for m, s in aggregate.items() if s["improved"] >= needed]
    worse = [m for m, s in aggregate.items() if s["worsened"] >= needed]
    if better and not worse:
        label = "improved"
    elif worse and not better:
        label = "worse"
    elif better and worse:
        label = "mixed"
    else:
        label = "no_effect"
    return {
        "label": label,
        "reason": f"{len(better)} metric(s) improved, {len(worse)} worsened, across {n} pair(s)",
        "improved_metrics": better,
        "worsened_metrics": worse,
    }


def write_proposal_evaluation(path: Path, evaluation: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
