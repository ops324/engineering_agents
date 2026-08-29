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


def test_o2_does_not_match_a_message_that_only_says_co2():
    """``"o2" in text`` is true of every message that says co2, and almost every
    message says co2.

    An audit (2026-08-29, EXP-033) found 5 of 36 ``request_o2`` commands in the
    v3/v4/v5 runs attributed to an operator message that never mentions O2 at
    all -- the provenance said an operator announced a command they were not
    talking about. ``"ars"`` has the same shape and matched ``clears`` three
    times. This is the same substring trap that, inside the analysis itself,
    counted CO2 deaths as O2 deaths because ``"o2" in "co2_warning"``.

    The keywords are alphanumeric, so ``\\b`` sits between ``c`` and ``o2`` and
    would not help; the match requires a non-alphanumeric neighbour instead.
    """
    from integrations.one_piece.client import _mentions

    assert not _mentions("co2 at 2.60kg confirms drift persists", "o2")
    assert _mentions("o2 at 103.93kg clears 95kg cutoff", "o2")
    assert not _mentions("consensus holds: o2 clears 95kg cutoff", "ars")
    assert _mentions("starting ars air_revitalisation", "ars")
    assert not _mentions("logs show nothing", "ogs")
    assert _mentions("starting ogs oxygen_generation", "ogs")
    assert _mentions("co2 drift continues", "co2")


def test_a_request_o2_command_is_matched_to_a_message_about_o2(tmp_path: Path):
    """End to end through the exporter, on the shape the audit found in the runs.

    Both messages are the same step and actor, and both mention co2; only one
    mentions O2. Before the fix the first one won, because it came first.
    """
    from integrations.one_piece.client import _find_operational_message

    messages = [
        {"step": 7, "from_role": "eclss_actor_1", "message_type": "operational_command",
         "message": "CO2 at 2.64kg confirms drift persists; holding ARS."},
        {"step": 7, "from_role": "eclss_actor_1", "message_type": "operational_command",
         "message": "Requesting O2 to shore up reserves against the CO2 drift."},
    ]
    matched = _find_operational_message(7, "eclss_actor_1", "request_o2", messages)
    assert matched is not None
    assert "Requesting O2" in matched["message"]
