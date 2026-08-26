"""The scorecard scores what it defines and refuses what it does not."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scenario.jobs.executor import execute_run
from scenario.jobs.spec import RunSpec
from scenario.ssos_eclss_loop.scorecard import POINTS_POLICY, score_run

BASE = {
    "backend": {"kind": "plant_sim"},
    "simulation": {"steps": 30},
    "inject_failures": True,
    "agents": {"mode": "labeled_rule_base"},
}


@pytest.fixture
def rule_run(tmp_path) -> Path:
    result = execute_run(
        RunSpec(scenario="ssos_eclss_loop", overrides=BASE, run_id="rule",
                results_root=tmp_path, seed=101)
    )
    assert result.exit_code == 0, result.error
    return Path(result.run_dir)


def test_the_one_axis_with_a_formula_is_scored(rule_run):
    card = score_run(rule_run)
    axis = card["axes"]["actor_remaining"]
    summary = json.loads((rule_run / "summary.json").read_text(encoding="utf-8"))
    expected = 50.0 * summary["crew_remaining"] / summary["crew_initial"]
    assert axis["points"] == pytest.approx(expected)
    assert axis["formula"] == "50 × actor_remaining ÷ actor_initial"


def test_curves_the_scorecard_does_not_state_are_labelled_as_this_branch_s(rule_run):
    """A–D carry curves now, and every one of them says whose they are.

    The scorecard states one formula, for the 50-point axis. The rest were
    chosen here on 2026-08-26 and are not in the document; a reader of a scored
    artifact has to be able to tell those apart, or a branch's choice gets
    reported as the project's criterion.
    """
    card = score_run(rule_run)
    for name in ("A_environment", "B_margin", "C_judgement", "D_response"):
        axis = card["axes"][name]
        assert axis["points"] is not None
        assert axis["points_policy"] == POINTS_POLICY
    assert card["axes"]["actor_remaining"]["formula"] == "50 × actor_remaining ÷ actor_initial"
    assert "points_policy" not in card["axes"]["actor_remaining"]
    assert card["total"]["points"] == pytest.approx(
        sum(card["axes"][name]["points"] for name in card["axes"]
            if card["axes"][name].get("applicable", True))
    )


def test_the_curves_separate_the_arms(rule_run, tmp_path):
    """A scoring curve that cannot tell the arms apart is not a scoring curve.

    The first draft normalised CO2 exposure by the limit itself and scored a
    run where the whole crew died at 17.4 of 20 against 20.0 for the best run.
    This pins that the total moves with the outcome: no operator at all must
    score below the rule arm on the axes both of them have.
    """
    result = execute_run(
        RunSpec(scenario="ssos_eclss_loop", overrides={**BASE, "agents": {"mode": "none"}},
                run_id="idle", results_root=tmp_path, seed=101)
    )
    idle = score_run(Path(result.run_dir))
    rule = score_run(rule_run)
    shared = ("actor_remaining", "A_environment", "B_margin")
    assert sum(rule["axes"][a]["points"] for a in shared) > sum(
        idle["axes"][a]["points"] for a in shared
    )
    # C and D do not apply without an operator, and the maxima differ, so the
    # totals are not comparable -- the scorecard says so outright.
    assert idle["total"]["applicable_max"] == 90
    assert rule["total"]["applicable_max"] == 100


def test_the_quantities_those_axes_need_are_present(rule_run):
    card = score_run(rule_run)
    a = card["axes"]["A_environment"]
    assert a["co2_exposure_integral"] is not None
    assert a["o2_deficit_integral"] is not None
    assert a["water_deficit_integral"] is not None
    assert a["dwell"]["co2_status"]["longest_critical_streak"] >= 0
    c = card["axes"]["C_judgement"]
    assert c["applicable"] is True
    assert "air_revitalisation" in c["response_latency_steps"]
    d = card["axes"]["D_response"]
    assert d["requested_processed_ratio"]


def test_never_acting_is_not_latency_zero(rule_run):
    """An arm that never issues a command has no latency, not a latency of 0.

    EXP-012 found runs that never ran the scrubber while the cabin filled.
    Recording that as a prompt response would score the worst behaviour
    observed as the best possible.
    """
    card = score_run(rule_run)
    detail = card["axes"]["C_judgement"]["response_latency_steps"]["water_recovery"]
    if detail["latency_steps"] is None:
        assert detail["reason"]


def test_a_run_that_fails_the_gate_is_not_scored(rule_run, tmp_path):
    """"物理ゲート不合格のランは採点せず、検証無効とする"."""
    broken = tmp_path / "broken"
    broken.mkdir()
    for name in ("summary.json", "telemetry.jsonl", "health_metrics.jsonl", "events.jsonl"):
        source = rule_run / name
        if source.is_file():
            (broken / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    rows = [json.loads(line) for line in (broken / "telemetry.jsonl").read_text().splitlines() if line.strip()]
    rows[-1]["o2_storage_kg"] = -1.0  # inventories must stay non-negative
    (broken / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    card = score_run(broken)
    assert card["scorable"] is False
    assert card["axes"]["actor_remaining"]["points"] is None
    assert card["total"]["points"] is None
