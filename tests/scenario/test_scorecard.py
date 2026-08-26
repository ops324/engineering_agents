"""The scorecard scores what it defines and refuses what it does not."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scenario.jobs.executor import execute_run
from scenario.jobs.spec import RunSpec
from scenario.ssos_eclss_loop.scorecard import score_run

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


def test_the_axes_without_a_formula_are_not_invented(rule_run):
    """The refusal is the feature.

    A curve chosen here would define "better" in a scoring module rather than in
    the document the team agreed, and every comparison after it would inherit
    that choice without anyone deciding it.
    """
    card = score_run(rule_run)
    for name in ("A_environment", "B_margin", "C_judgement", "D_response"):
        axis = card["axes"][name]
        assert axis["points"] is None
        assert axis["undefined_reason"]
    assert card["total"]["points"] is None
    assert set(card["total"]["unscored_axes"]) == {
        "A_environment", "B_margin", "C_judgement", "D_response"
    }


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
