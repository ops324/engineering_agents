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


def test_score_refuses_to_pick_a_yardstick_for_you(tmp_path, run_dir):
    """Defaulting would mean grading a run against thresholds its own proposal
    may have moved."""
    result = runner.invoke(app, ["score", str(run_dir)])
    assert result.exit_code == 2
    assert "--baseline" in result.output


def test_score_refuses_two_yardsticks_at_once(tmp_path, run_dir):
    result = runner.invoke(
        app, ["score", str(run_dir), "--baseline", str(run_dir), "--habitat-volume", "61.3"]
    )
    assert result.exit_code == 2


def test_score_against_a_frozen_baseline(tmp_path, run_dir):
    result = runner.invoke(app, ["score", str(run_dir), "--baseline", str(run_dir)])
    assert result.exit_code == 0
    assert "yardstick=frozen-baseline" in result.output
    assert "not scored: o2" in result.output


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
