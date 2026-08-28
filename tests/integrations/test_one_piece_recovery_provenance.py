"""Provenance export tests for EPS recovery traces (EPS-4)."""

from __future__ import annotations

import json
from pathlib import Path

from integrations.one_piece.client import build_provenance_records
from scenario.runner import run_scenario


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_build_provenance_includes_eps_recovery_record(tmp_path: Path):
    run_dir = run_scenario(
        "scrubber_degradation",
        output_dir=tmp_path / "labeled",
        overrides={"agents": {"mode": "labeled_rule_base"}},
        recreate_output=True,
    )
    records = build_provenance_records(run_dir)
    recovery = [r for r in records if r.get("change_kind") == "request_eps_boost"]
    assert recovery, "expected EPS boost recovery provenance"
    assert recovery[0]["record_type"] == "recovery"
    assert recovery[0]["trace"]["event_kind"] == "/eclss/events/recovery_applied"
    assert recovery[0]["actor"].startswith("engineer_")
    assert recovery[0]["trace"]["message"]
    assert recovery[0]["trace"]["reasoning"]
    assert recovery[0]["trace"]["decision_source"] == "rule"


def test_build_provenance_includes_ssos_operational_records(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "labeled",
        overrides={
            "agents": {"mode": "labeled_rule_base"},
            # Cabin O2 well inside the [V2 6003] normoxia band. At the old tank
            # value of 8.0 kg the cabin reads as asphyxial, so OGS fires first and
            # the first operational record is no longer the ARS one asserted below.
            "simulation": {"initial_o2_storage_kg": 110.0},
        },
        recreate_output=True,
    )
    records = build_provenance_records(run_dir)
    operational = [r for r in records if r.get("record_type") == "operational"]
    assert operational, "expected SSOS operational provenance"
    assert operational[0]["change_kind"] == "air_revitalisation"
    assert operational[0]["trace"]["event_kind"] == "/eclss/events/operational_applied"


def test_build_provenance_includes_operational_rejected(tmp_path: Path):
    run_dir = tmp_path / "rejected_run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps({"scenario": "ssos_eclss_loop"}),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "step": 0,
                "kind": "/eclss/events/operational_rejected",
                "command": {
                    "kind": "air_revitalisation",
                    "payload": {"initial_co2_mass": 1.8},
                    "issued_by": "eclss_operator_1",
                },
                "result": {"success": False, "summary_message": "ARS failed"},
                "message": "ARS failed",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    records = build_provenance_records(run_dir)
    operational = [r for r in records if r.get("record_type") == "operational"]
    assert len(operational) == 1
    assert operational[0]["change_kind"] == "air_revitalisation"
    assert operational[0]["trace"]["event_kind"] == "/eclss/events/operational_rejected"
    assert operational[0]["trace"]["result_success"] is False
    assert operational[0]["trace"]["event_message"] == "ARS failed"
