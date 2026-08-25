"""Tests for the paired re-run that turns a proposal into evidence.

The first test is the one the module exists for: a proposal that only moves
the bar must not be able to buy an improvement with it.
"""

from __future__ import annotations

import json

import pytest

from pathlib import Path

from scenario.jobs.executor import execute_run
from scenario.jobs.spec import RunSpec
from scenario.ssos_eclss_loop.trajectory_metrics import (
    from_frozen_baseline,
    trajectory_metrics,
)
from scenario.ssos_eclss_loop.proposal_evaluation import (
    MIN_REPEATS_FOR_A_STOCHASTIC_VERDICT,
    ProposalEvaluationError,
    evaluate_proposal,
    load_baseline,
    yardstick_changes,
)

BASE_OVERRIDES = {
    "backend": {"kind": "plant_sim"},
    "simulation": {"steps": 30},
    "inject_failures": True,
    "agents": {"mode": "labeled_rule_base"},
}


#: Within rated ARS throughput. The shipped proposer emits 2.25 (1.25x the
#: reference goal), which buys super-rated scrubbing -- see
#: test_a_proposal_that_buys_super_rated_capacity_is_refused.
WITHIN_CAPACITY = [
    {"change_kind": "set_parameter",
     "payload": {"target": "agents.policy.co2_storage_high_kg", "value": 1.35}},
    {"change_kind": "set_parameter",
     "payload": {"target": "thresholds.co2_storage_high_kg", "value": 1.35}},
]


@pytest.fixture
def baseline(tmp_path):
    """A real rule-arm run, proposals and all. 0.08 s, so no need to fake it."""
    result = execute_run(
        RunSpec(
            scenario="ssos_eclss_loop",
            overrides=BASE_OVERRIDES,
            run_id="baseline",
            results_root=tmp_path,
            seed=101,
        )
    )
    assert result.exit_code == 0, result.error
    run_dir = Path(result.run_dir)
    # The shipped proposal exceeds rated ARS; tests about everything *else*
    # start from one that does not, so a capacity refusal cannot mask them.
    _replace_proposal(run_dir, WITHIN_CAPACITY)
    return run_dir


def _replace_proposal(run_dir: Path, changes):
    payload = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))
    payload["changes"] = changes
    (run_dir / "design_proposals.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# the failure this module exists to prevent
# --------------------------------------------------------------------------- #
def test_raising_the_bar_is_graded_on_its_physics_not_on_the_bar(tmp_path, baseline):
    """The hazard, demonstrated on one pair of runs.

    thresholds.* is doubly purposed: it is the alarm the rule acts on *and* the
    bar health scoring reads. Raising co2_storage_critical_kg above the
    trajectory stops the escalated ARS from ever firing, so the physics gets
    worse -- while making the same trajectory look spotless to anything scoring
    against the raised bar.

    Graded on the baseline's frozen bar, the verdict follows the physics.
    Graded on the treated run's own bar, the identical trajectory scores zero
    exposure. Both are computed here so the difference is not a claim.

    The raised value is derived from the baseline run, not written down here: a
    bar is only a hiding place while it sits above the trajectory, and how high
    that is depends on the operating point. A literal 9.0 demonstrated the
    hazard while the crew was four; at crew 50 the cabin passes 9 kg unaided
    and the literal stopped demonstrating anything.

    The band moved is ``co2_storage_high_kg``, the one this operating point
    actually enters. ``co2_storage_critical_kg`` ships at 8.0 kg, which four
    occupants cannot reach inside a run -- 116 steps of generation with no
    removal at all -- so raising it changes nothing and the evaluation rightly
    answers "no_effect". A demonstration needs a threshold the run crosses.
    """
    baseline_summary = json.loads(
        (baseline / "summary.json").read_text(encoding="utf-8")
    )
    own_bar = round(float(baseline_summary["peak_co2_storage_kg"]) * 2.0, 3)

    _replace_proposal(baseline, [
        {"change_kind": "set_parameter",
         "payload": {"target": "thresholds.co2_storage_high_kg", "value": own_bar}},
    ])
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="raise")
    assert ev["yardstick_changes"] == ["thresholds.co2_storage_high_kg"]

    # Held bar: the proposal is judged by what it did to the plant.
    assert ev["verdict"]["label"] == "worse"
    assert ev["aggregate"]["exposure_integral_kg_steps"]["delta"]["mean"] > 0

    # Its own bar: the very same trajectory, scored against the raised value,
    # is spotless.
    treated_dir = Path(ev["pairs"][0]["treated"]["run_dir"])
    treated_summary = json.loads(
        (treated_dir / "summary.json").read_text(encoding="utf-8")
    )
    treated_peak = float(treated_summary["peak_co2_storage_kg"])
    assert treated_peak < own_bar, (
        f"the raised bar {own_bar} kg is not above the treated trajectory "
        f"(peak {treated_peak} kg); the hazard needs a bar the run cannot cross"
    )
    self_scored = trajectory_metrics(
        treated_dir,
        from_frozen_baseline(
            {
                "co2_storage_high_kg": own_bar,
                "co2_storage_critical_kg": baseline_summary["thresholds"][
                    "co2_storage_critical_kg"
                ],
            },
            baseline_run_id="its-own",
        ),
    )
    assert self_scored["co2"]["bands"]["high"]["steps_above"] == 0
    assert ev["pairs"][0]["treated"]["steps_above"] > 0


def test_a_proposal_cannot_bank_the_deaths_it_deleted(tmp_path, baseline):
    """EXP-010, pinned: attrition is graded on the baseline's bands.

    Occupants die from dwelling in a health band, and the band edges are the
    same thresholds.* keys a proposal may move. Raise the alarm above the
    trajectory and the run reports "safe" for every reading: the dwell counter
    never advances, and the run's own crew_remaining reports occupants it
    killed as alive, while the air is measurably worse.

    The frozen-bar defence that protects the CO2 figures cannot reach this --
    attrition happens inside the run, not at scoring time -- so the comparison
    replays the dwell policy over each arm's recorded trajectory under the
    baseline's bands. Both readings are asserted here: what the runs claim
    about themselves, and what the held bands say they did.
    """
    baseline_summary = json.loads(
        (baseline / "summary.json").read_text(encoding="utf-8")
    )
    own_bar = round(float(baseline_summary["peak_co2_storage_kg"]) * 2.0, 3)
    _replace_proposal(baseline, [
        {"change_kind": "set_parameter",
         "payload": {"target": "thresholds.co2_storage_high_kg", "value": own_bar}},
    ])
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="hide")

    def reported_crew(key: str) -> int:
        run_dir = Path(ev["pairs"][0][key]["run_dir"])
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        return int(summary["crew_remaining"])

    # What the runs say about themselves: the proposal saved a life.
    assert reported_crew("treated") > reported_crew("control")

    # What they did. The air got worse ...
    assert ev["aggregate"]["peak_kg"]["delta"]["mean"] > 0
    # ... and counted on the bands the proposal did not set, it saved nobody.
    assert "crew_remaining_frozen" in ev["aggregate"]
    assert ev["aggregate"]["crew_remaining_frozen"]["delta"]["mean"] <= 0
    assert ev["aggregate"]["crew_remaining_frozen"]["improved"] == 0


def test_a_proposal_that_changes_no_behaviour_changes_no_metric(tmp_path, baseline):
    """The guard must not manufacture differences either: a target the labeled
    policy does not read leaves both arms identical."""
    _replace_proposal(baseline, [
        {"change_kind": "set_parameter",
         "payload": {"target": "thresholds.product_water_low_l", "value": 5.0}},
    ])
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="inert")
    assert ev["verdict"]["label"] == "no_effect"
    for metric, stats in ev["aggregate"].items():
        assert stats["delta"]["mean"] == 0.0, metric


def test_the_yardstick_is_the_baselines_not_the_treated_runs(tmp_path, baseline):
    _replace_proposal(baseline, [
        {"change_kind": "set_parameter",
         "payload": {"target": "thresholds.co2_storage_critical_kg", "value": 9.0}},
    ])
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="y")
    assert ev["yardstick"]["source"] == "frozen-baseline"
    assert ev["yardstick"]["detail"]["baseline_run_id"] == baseline.name
    critical = next(b for b in ev["yardstick"]["bands"] if b["name"] == "critical")
    assert critical["threshold_kg"] != 9.0


def test_a_change_that_moves_physics_does_show(tmp_path, baseline):
    """The counterpart: the guard must not simply flatten everything."""
    _replace_proposal(baseline, [
        {"change_kind": "action_profile",
         "payload": {"subsystem": "ars", "action": "air_revitalisation",
                     "fields": {"initial_co2_mass": 0.2}}},
    ])
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="phys")
    assert ev["verdict"]["label"] in {"worse", "mixed"}
    assert any(s["worsened"] for s in ev["aggregate"].values())


# --------------------------------------------------------------------------- #
# replication, required where it is required
# --------------------------------------------------------------------------- #
def test_a_deterministic_arm_settles_on_one_pair(tmp_path, baseline):
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="det")
    assert ev["deterministic"] is True
    assert len(ev["pairs"]) == 1
    assert ev["verdict"]["label"] != "inconclusive"


def test_a_stochastic_arm_gets_numbers_but_no_verdict_from_one_pair(tmp_path, baseline):
    """Measured CV is 26% and same-seed spread is 0.78x between-seed spread, so
    one llm pair is an anecdote with two arms."""
    summary = json.loads((baseline / "summary.json").read_text(encoding="utf-8"))
    summary["agents_mode"] = "llm"
    (baseline / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="sto")
    assert ev["deterministic"] is False
    assert ev["verdict"]["label"] == "inconclusive"
    assert "stochastic" in ev["verdict"]["reason"]
    assert ev["pairs"], "the numbers are still reported"


def test_the_stochastic_threshold_is_stated_not_implied():
    assert MIN_REPEATS_FOR_A_STOCHASTIC_VERDICT >= 3


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #
def test_a_live_ros2_baseline_is_refused(tmp_path, baseline):
    """Real-time SSOS with no step synchronisation is not a matched pair."""
    summary = json.loads((baseline / "summary.json").read_text(encoding="utf-8"))
    summary["backend"] = "ros2"
    (baseline / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ProposalEvaluationError, match="matched pair"):
        evaluate_proposal(baseline, results_root=tmp_path)


def test_a_proposal_with_no_changes_is_refused(tmp_path, baseline):
    _replace_proposal(baseline, [])
    with pytest.raises(ProposalEvaluationError, match="no changes"):
        evaluate_proposal(baseline, results_root=tmp_path)


def test_a_baseline_missing_its_artifacts_is_refused(tmp_path, baseline):
    (baseline / "design_proposals.json").unlink()
    with pytest.raises(ProposalEvaluationError, match="design_proposals.json"):
        load_baseline(baseline)


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def test_threshold_only_targets_are_named_for_the_reader(tmp_path, baseline):
    """Not a score adjustment: a proposal made only of these is proposing an
    alarm setting, not a plant."""
    _replace_proposal(baseline, [
        {"change_kind": "set_parameter",
         "payload": {"target": "thresholds.co2_storage_high_kg", "value": 1.1}},
        {"change_kind": "action_profile",
         "payload": {"subsystem": "ars", "action": "air_revitalisation",
                     "fields": {"initial_co2_mass": 1.5}}},
    ])
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="lbl")
    assert ev["yardstick_changes"] == ["thresholds.co2_storage_high_kg"]


def test_each_pair_records_both_run_directories(tmp_path, baseline):
    """Provenance: the claim must be traceable to the runs behind it."""
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="prov")
    pair = ev["pairs"][0]
    assert Path(pair["control"]["run_dir"]).is_dir()
    assert Path(pair["treated"]["run_dir"]).is_dir()
    assert pair["control"]["run_dir"] != pair["treated"]["run_dir"]


def test_yardstick_changes_ignores_non_threshold_targets():
    assert yardstick_changes({"changes": [
        {"change_kind": "set_parameter",
         "payload": {"target": "agents.policy.co2_storage_high_kg", "value": 1.0}},
    ]}) == []


def test_a_proposal_that_buys_super_rated_capacity_is_refused(tmp_path, baseline):
    """PlantModel scales ARS removal by goal/reference with no ceiling, so an
    action_profile proposal can ask for throughput the hardware does not have.
    The gate catches it and the arm is refused rather than credited.

    The proposal the shipped labeled proposer emits (initial_co2_mass 2.25,
    1.25x the reference) is already over the line.
    """
    _replace_proposal(baseline, [
        {"change_kind": "action_profile",
         "payload": {"subsystem": "ars", "action": "air_revitalisation",
                     "fields": {"initial_co2_mass": 1800.0}}},
    ])
    with pytest.raises(ProposalEvaluationError, match="capacity_bounds"):
        evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="over")
