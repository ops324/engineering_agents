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
from scenario.ssos_eclss_loop.physics_gate import evaluate_physics_gate, gate_passed
from scenario.ssos_eclss_loop.scorecard import score_run
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
    """EXP-010, pinned at both layers.

    Occupants die from dwelling in a health band. Those edges used to be the
    same thresholds.* keys a proposal may move, so raising an alarm above the
    trajectory made the run report "safe" for every reading, the dwell counter
    never advanced, and occupants the proposal had killed were reported alive.

    Two things now stand between a proposal and that outcome, and this asserts
    both, because either alone would leave a way back:

    - the edges live in plant_sim.survival.bands, which is not in
      ALLOWED_SET_PARAMETER_TARGETS, so the alarm moves and the lethal edge
      does not; and
    - the comparison counts occupants by replaying the dwell policy over each
      arm's trajectory under the baseline's bands, so a run that did move them
      would still be counted on the bar it was measured against.

    Measured with the alarm raised from 2.0 to 4.76 kg: cabin peak 2.380 ->
    2.760, three occupants in both arms, and the band still reading 2.0 in the
    treated run's own summary.
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

    def summary_of(key: str) -> dict:
        run_dir = Path(ev["pairs"][0][key]["run_dir"])
        return json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    control, treated = summary_of("control"), summary_of("treated")

    # The alarm moved; the edge that kills did not.
    assert treated["thresholds"]["co2_storage_high_kg"] == own_bar
    assert (
        treated["survival_bands"]["co2_storage_high_kg"]
        == control["survival_bands"]["co2_storage_high_kg"]
    )

    # The air got worse ...
    assert ev["aggregate"]["peak_kg"]["delta"]["mean"] > 0
    # ... and the run's own occupant count does not improve for it. This is the
    # assertion that fails first if the bands are ever coupled back.
    assert int(treated["crew_remaining"]) <= int(control["crew_remaining"])

    # The second layer agrees: counted on bands the proposal did not set, the
    # proposal saved nobody.
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


def test_a_proposal_can_no_longer_buy_super_rated_capacity(tmp_path, baseline):
    """This asserted a refusal until the rated-capacity invariant landed.

    PlantModel used to scale ARS removal by goal/reference with no ceiling, so
    initial_co2_mass 1800 bought a thousand times the rated machine and the physics
    gate was the only thing standing in front of it -- the arm was refused.

    The invariant removes the capability, so the gate has nothing to catch and the
    run is evaluated normally. That is the intended end state, and it is the reason
    capacity_bounds should now be unable to fire: if it ever does, the invariant is
    broken. What has NOT changed is that asking for 1000x rated is a bad judgement,
    and the scorecard's C axis still scores it as one -- the harm is gone, the
    mistake is not.
    """
    _replace_proposal(baseline, [
        {"change_kind": "action_profile",
         "payload": {"subsystem": "ars", "action": "air_revitalisation",
                     "fields": {"initial_co2_mass": 1800.0}}},
    ])
    ev = evaluate_proposal(baseline, results_root=tmp_path, run_id_prefix="over")

    treated = Path(ev["pairs"][0]["treated"]["run_dir"])
    gate = evaluate_physics_gate(treated)
    assert gate_passed(gate)
    capacity = next(c for c in gate["checks"] if c["name"] == "capacity_bounds")
    assert capacity["status"] == "pass"
    assert "ars 1.0" in capacity["detail"] or "ars 0.9" in capacity["detail"], capacity["detail"]

    # Against the control arm of the same pair, so the claim is "this proposal is
    # scored worse for asking" and not a threshold picked to fit. sizing_score is a
    # mean over every sized command, so ARS at 0.001 of rated is diluted by the OGS
    # and WRS commands around it -- the comparison is what carries the meaning.
    control = Path(ev["pairs"][0]["control"]["run_dir"])
    sizing = score_run(treated)["axes"]["C_judgement"]["request_sizing"]
    control_sizing = score_run(control)["axes"]["C_judgement"]["request_sizing"]
    assert sizing["oversized_by_kind"].get("air_revitalisation", 0) > 0
    assert not control_sizing["oversized_by_kind"].get("air_revitalisation")
    assert sizing["within_capacity_fraction"] < control_sizing["within_capacity_fraction"]
    assert sizing["sizing_score"] < control_sizing["sizing_score"]


def test_the_arms_do_not_re_run_the_designer(tmp_path):
    """The designer runs after the simulation, so it cannot touch a trajectory,
    and whatever it proposes inside an arm is written and never read -- the
    proposal under test comes from the baseline.

    Inheriting agents.design.mode therefore buys nothing. With llm it costs two
    27B passes per repeat: measured at 4.5 minutes each against a 0.4 second
    evaluation, which would have tripled the bill for EXP-025 without changing
    a single number.
    """
    from scenario.ssos_eclss_loop.proposal_evaluation import _overrides_for

    config = {
        "agents": {"actor": {"mode": "labeled_rule_base"}, "design": {"mode": "llm"}},
        "simulation": {"steps": 30},
    }
    arms = _overrides_for(config, seed=101)
    assert arms["agents"]["design"]["mode"] == "none"
    # The actor is what the arms are re-running; it carries over untouched.
    assert arms["agents"]["actor"]["mode"] == "labeled_rule_base"
    # And the caller's config is not mutated.
    assert config["agents"]["design"]["mode"] == "llm"
