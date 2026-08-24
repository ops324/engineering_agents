"""CLI surface for the physics gate."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from tests.scenario.test_physics_gate import valid_trajectory
from tools.cli.commands.gate import GATE_FAILED_EXIT
from tools.cli.main import app

runner = CliRunner()


def _run_dir(tmp_path, records, name="run-1"):
    run_dir = tmp_path / name
    run_dir.mkdir()
    (run_dir / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return run_dir


def test_gate_exits_zero_on_a_consistent_run(tmp_path):
    result = runner.invoke(app, ["gate", str(_run_dir(tmp_path, valid_trajectory()))])
    assert result.exit_code == 0
    assert "verdict=pass" in result.stdout
    assert "coverage=" in result.stdout


def test_gate_exit_code_separates_a_void_run_from_a_broken_command(tmp_path):
    """4, not 3: ENVIRONMENT_ERROR already owns 3 and CI must tell them apart."""
    records = valid_trajectory()
    records[2]["o2_storage_kg"] = -1.0
    result = runner.invoke(app, ["gate", str(_run_dir(tmp_path, records))])
    assert result.exit_code == GATE_FAILED_EXIT
    assert GATE_FAILED_EXIT != 3


def test_gate_writes_the_result_beside_the_run(tmp_path):
    run_dir = _run_dir(tmp_path, valid_trajectory())
    result = runner.invoke(app, ["gate", str(run_dir), "--write"])
    assert result.exit_code == 0
    written = json.loads((run_dir / "physics_gate.json").read_text(encoding="utf-8"))
    assert written["verdict"] == "pass"
    assert written["form"] == "full"


def test_gate_all_reports_each_failure_by_run_id(tmp_path):
    _run_dir(tmp_path, valid_trajectory(), name="good")
    broken = valid_trajectory()
    broken[2]["co2_storage_kg"] -= 0.05
    _run_dir(tmp_path, broken, name="bad")
    result = runner.invoke(app, ["gate", "--all", "--results-root", str(tmp_path)])
    assert result.exit_code == GATE_FAILED_EXIT
    assert "2 runs: 1 pass, 1 fail" in result.stdout
    assert "FAIL bad: carbon_ledger" in result.stdout


def test_gate_without_a_target_is_a_usage_error(tmp_path):
    result = runner.invoke(app, ["gate", "--results-root", str(tmp_path)])
    assert result.exit_code == 2
