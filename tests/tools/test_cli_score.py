"""CLI surface for trajectory scoring and proposal evaluation."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from scenario.jobs.executor import execute_run
from scenario.jobs.spec import RunSpec
from tools.cli.commands.score import NOT_SCORABLE_EXIT
from tools.cli.main import app

runner = CliRunner()

OVERRIDES = {
    "backend": {"kind": "plant_sim"},
    "simulation": {"steps": 30},
    "inject_failures": True,
    "agents": {"mode": "labeled_rule_base"},
}


@pytest.fixture
def run_dir(tmp_path):
    result = execute_run(
        RunSpec(
            scenario="ssos_eclss_loop",
            overrides=OVERRIDES,
            run_id="demo",
            results_root=tmp_path,
            seed=101,
        )
    )
    assert result.exit_code == 0, result.error
    return result.run_dir


def test_score_defaults_to_the_bar_nothing_here_can_edit(tmp_path, run_dir):
    """No flags means NASA-STD-3001 at the scenario habitat. Defaulting to the
    run's *own* thresholds would grade it against a bar its own proposal may
    have moved; defaulting to the standard cannot drift at all."""
    result = runner.invoke(app, ["score", str(run_dir)])
    assert result.exit_code == 0
    assert "yardstick=nasa-std-3001" in result.output
    assert "[V2 6004]" in result.output


def test_score_refuses_two_yardsticks_at_once(tmp_path, run_dir):
    result = runner.invoke(
        app, ["score", str(run_dir), "--baseline", str(run_dir), "--habitat-volume", "61.3"]
    )
    assert result.exit_code == 2


def test_score_against_a_frozen_baseline(tmp_path, run_dir):
    result = runner.invoke(app, ["score", str(run_dir), "--baseline", str(run_dir)])
    assert result.exit_code == 0
    assert "yardstick=frozen-baseline" in result.output
    # Named as unscorable against a standard, and still reported: leaving the
    # axis out entirely is how a run with the best CO2 and the worst survival
    # reads as the best run (EXP-011).
    assert "not scored against a standard: o2" in result.output
    assert "o2    low" in result.output
    assert "water low" in result.output


def test_score_against_the_standard_needs_a_habitat(tmp_path, run_dir):
    result = runner.invoke(app, ["score", str(run_dir), "--habitat-volume", "61.3"])
    assert result.exit_code == 0
    assert "yardstick=nasa-std-3001" in result.output
    assert "[V2 6004]" in result.output


def test_score_writes_its_result_beside_the_run(tmp_path, run_dir):
    result = runner.invoke(
        app, ["score", str(run_dir), "--baseline", str(run_dir), "--write"]
    )
    assert result.exit_code == 0
    written = json.loads((run_dir / "trajectory_metrics.json").read_text(encoding="utf-8"))
    assert written["yardstick"]["source"] == "frozen-baseline"


def test_score_exits_four_when_the_run_cannot_be_scored(tmp_path, run_dir):
    """Unscorable is not a low score, and a CI step must tell them apart."""
    (run_dir / "telemetry.jsonl").write_text("", encoding="utf-8")
    result = runner.invoke(app, ["score", str(run_dir), "--baseline", str(run_dir)])
    assert result.exit_code == NOT_SCORABLE_EXIT


def test_evaluate_reports_both_arms_and_a_verdict(tmp_path, run_dir):
    result = runner.invoke(app, ["evaluate", str(run_dir), "--results-root", str(tmp_path)])
    assert result.exit_code == 0
    assert "deterministic=True" in result.output
    assert "verdict:" in result.output
    assert "control" in result.output and "treated" in result.output


def test_evaluate_flags_a_proposal_that_moves_an_alarm_setting(tmp_path, run_dir):
    payload = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))
    payload["changes"] = [
        {"change_kind": "set_parameter",
         "payload": {"target": "thresholds.co2_storage_high_kg", "value": 1.1}}
    ]
    (run_dir / "design_proposals.json").write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(app, ["evaluate", str(run_dir), "--results-root", str(tmp_path)])
    assert result.exit_code == 0
    assert "an alarm setting, not a plant" in result.output


def test_evaluate_writes_its_result_beside_the_baseline(tmp_path, run_dir):
    result = runner.invoke(
        app, ["evaluate", str(run_dir), "--results-root", str(tmp_path), "--write"]
    )
    assert result.exit_code == 0
    written = json.loads((run_dir / "proposal_evaluation.json").read_text(encoding="utf-8"))
    assert written["deterministic"] is True
    assert written["pairs"]


def test_evaluate_refuses_a_baseline_with_no_proposal(tmp_path, run_dir):
    payload = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))
    payload["changes"] = []
    (run_dir / "design_proposals.json").write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(app, ["evaluate", str(run_dir), "--results-root", str(tmp_path)])
    assert result.exit_code == NOT_SCORABLE_EXIT


# --------------------------------------------------------------------------- #
# the documented exit contract, which the first cut did not keep
# --------------------------------------------------------------------------- #
def test_score_exits_four_when_the_named_baseline_has_no_thresholds(tmp_path, run_dir):
    """from_frozen_baseline raises NotScorable; the call used to sit outside
    the handler and surfaced as a traceback with exit 1."""
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "summary.json").write_text(json.dumps({"scenario": "ssos_eclss_loop"}), encoding="utf-8")
    result = runner.invoke(app, ["score", str(run_dir), "--baseline", str(bare)])
    assert result.exit_code == NOT_SCORABLE_EXIT


def test_score_rejects_an_unknown_band_instead_of_printing_nothing(tmp_path, run_dir):
    """Exiting 0 with no rows reads as 'nothing above threshold'."""
    result = runner.invoke(
        app, ["score", str(run_dir), "--baseline", str(run_dir), "--band", "nope"]
    )
    assert result.exit_code == 2
    assert "unknown band" in result.output


def test_evaluate_rejects_an_unknown_band_before_paying_for_the_runs(tmp_path, run_dir):
    result = runner.invoke(
        app,
        ["evaluate", str(run_dir), "--results-root", str(tmp_path),
         "--band", "nope"],
    )
    assert result.exit_code == 2
    assert "unknown band" in result.output


def test_gate_fails_a_corrupt_reading_rather_than_crashing(tmp_path, run_dir):
    """A non-numeric reading is exactly what readings_present_and_finite is
    for; float() used to raise and exit 1, which is the CLI-broken code."""
    from tools.cli.commands.gate import GATE_FAILED_EXIT

    rows = [
        json.loads(line)
        for line in (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[1]["co2_storage_kg"] = "NaN-ish"
    (run_dir / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["gate", str(run_dir)])
    assert result.exit_code == GATE_FAILED_EXIT
    assert "readings_present_and_finite" in result.output


def test_scorecard_names_the_yardstick_it_used(tmp_path, run_dir):
    """A total is meaningless without its bar. EXP-014 mixed the two once, so the
    scorecard says which one produced the number it just printed."""
    result = runner.invoke(app, ["scorecard", str(run_dir)])
    assert result.exit_code == 0
    assert "yardstick: NASA-STD-3001 at 388 m3" in result.stdout


def test_the_two_yardsticks_give_different_totals(tmp_path, run_dir):
    """Not a formality: the v5 rule arm is 82.230 against its own thresholds and
    84.35 against the standard. EXP-013..016 published the former."""
    standard = runner.invoke(app, ["scorecard", str(run_dir), "--json"])
    own = runner.invoke(app, ["scorecard", str(run_dir), "--yardstick", "run", "--json"])
    assert standard.exit_code == 0 and own.exit_code == 0
    a = json.loads(standard.stdout)
    b = json.loads(own.stdout)
    assert a["yardstick"] != b["yardstick"]
    assert a["axes"]["B_margin"]["points"] != b["axes"]["B_margin"]["points"]
    assert a["axes"]["actor_remaining"]["points"] == b["axes"]["actor_remaining"]["points"]


def test_the_run_s_own_yardstick_is_refused_once_a_proposal_could_have_moved_it(
    tmp_path, run_dir
):
    """The hole EXP-010 closed, reopened by a flag, would be worse than no flag.

    ``--apply-proposals`` writes the four ``thresholds.*`` keys that health
    scoring reads, so for such a run "its own thresholds" may be thresholds its
    own proposal moved. The standard yardstick stays available.
    """
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["apply_proposals_path"] = "design_proposals.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    refused = runner.invoke(app, ["scorecard", str(run_dir), "--yardstick", "run"])
    assert refused.exit_code == 2
    assert runner.invoke(app, ["scorecard", str(run_dir)]).exit_code == 0


def test_the_run_s_own_yardstick_takes_no_habitat(tmp_path, run_dir):
    result = runner.invoke(
        app, ["scorecard", str(run_dir), "--yardstick", "run", "--habitat-volume-m3", "388"]
    )
    assert result.exit_code != 0
