"""Tests for the physics gate.

A gate that passes everything it is shown is indistinguishable from no gate,
so most of what follows is negative controls: a valid trajectory is perturbed
in one species at a time and the matching check must catch it.
"""

from __future__ import annotations

import copy
import json

import pytest

from environment.ssos.eclss.plant_sim.stoichiometry import (
    CH4_PER_H2,
    CO2_PER_H2,
    H2O_PER_H2,
    H2_PER_O2,
    WATER_PER_O2,
)
from scenario.ssos_eclss_loop.physics_gate import (
    FAIL,
    PASS,
    SKIPPED,
    TelemetryUnreadable,
    _plant,
    check_carbon_ledger,
    check_failure_quiescence,
    check_inventories_non_negative,
    check_oxygen_ledger,
    check_readings_present_and_finite,
    check_stoichiometric_residual,
    check_totals_monotonic,
    check_water_ledger,
    evaluate_physics_gate,
    gate_passed,
)

ZERO_TOTALS = {
    "total_co2_vented_kg": 0.0,
    "total_h2_vented_kg": 0.0,
    "total_ch4_vented_kg": 0.0,
    "total_wrs_brine_loss_l": 0.0,
    "total_o2_shortfall_kg": 0.0,
    "total_water_shortfall_l": 0.0,
    "total_co2_generated_kg": 0.0,
    "total_o2_consumed_kg": 0.0,
    "total_o2_generated_kg": 0.0,
    "total_electrolysis_water_kg": 0.0,
    "total_sabatier_co2_used_kg": 0.0,
    "total_wrs_recovered_water_l": 0.0,
    "total_water_regenerated_l": 0.0,
    "total_potable_water_consumed_l": 0.0,
    "total_urine_generated_l": 0.0,
    "total_condensate_generated_l": 0.0,
    "total_unrecoverable_crew_water_l": 0.0,
    "total_o2_delivered_kg": 0.0,
    "total_co2_delivered_kg": 0.0,
    "total_product_water_delivered_l": 0.0,
    "total_external_grey_water_submitted_l": 0.0,
}


def _record(step, *, cabin, o2, product, grey, captured, urine, totals, failures=()):
    plant = dict(ZERO_TOTALS)
    plant.update(totals)
    plant["captured_co2_kg"] = captured
    plant["urine_buffer_l"] = urine
    plant["simulation_time_s"] = float(step) * 1200.0
    return {
        "step": step,
        "co2_storage_kg": cabin,
        "o2_storage_kg": o2,
        "product_water_reserve_l": product,
        "grey_water_collected_l": grey,
        "ars_failure_enabled": "ars" in failures,
        "ogs_failure_enabled": "ogs" in failures,
        "wrs_failure_enabled": "wrs" in failures,
        "raw_topics": {"plant_sim": plant},
    }


def valid_trajectory():
    """Crew metabolism for one interval, then one ARS operation.

    Step 1: 0.1 kg CO2 out, 0.2 kg O2 in, 1.0 L drunk, 0.6 L urine, 0.3 L
    condensate, 0.1 L unrecoverable. Step 2: ARS removes 0.5 kg from the cabin,
    captures 0.4, vents 0.1.
    """
    step0 = _record(
        0, cabin=1.0, o2=2.0, product=50.0, grey=0.0, captured=0.0, urine=0.0, totals={}
    )
    step1 = _record(
        1,
        cabin=1.1,
        o2=1.8,
        product=49.0,
        grey=0.3,
        captured=0.0,
        urine=0.6,
        totals={
            "total_co2_generated_kg": 0.1,
            "total_o2_consumed_kg": 0.2,
            "total_potable_water_consumed_l": 1.0,
            "total_urine_generated_l": 0.6,
            "total_condensate_generated_l": 0.3,
            "total_unrecoverable_crew_water_l": 0.1,
        },
    )
    step2 = _record(
        2,
        cabin=0.6,
        o2=1.8,
        product=49.0,
        grey=0.3,
        captured=0.4,
        urine=0.6,
        totals={
            "total_co2_generated_kg": 0.1,
            "total_o2_consumed_kg": 0.2,
            "total_co2_vented_kg": 0.1,
            "total_potable_water_consumed_l": 1.0,
            "total_urine_generated_l": 0.6,
            "total_condensate_generated_l": 0.3,
            "total_unrecoverable_crew_water_l": 0.1,
        },
    )
    return [step0, step1, step2]


def _write(tmp_path, records):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return run_dir


# --------------------------------------------------------------------------- #
# the trajectory the negative controls are built from
# --------------------------------------------------------------------------- #
def test_a_consistent_trajectory_passes_every_implemented_check(tmp_path):
    result = evaluate_physics_gate(_write(tmp_path, valid_trajectory()))
    assert result["verdict"] == PASS
    assert gate_passed(result)
    assert result["form"] == "full"
    assert result["failed_checks"] == []
    # Only the check that declares itself unimplemented is skipped.
    assert result["skipped_checks"] == ["capacity_bounds"]


# --------------------------------------------------------------------------- #
# negative controls, one species at a time
# --------------------------------------------------------------------------- #
def test_carbon_ledger_catches_co2_appearing_from_nowhere():
    records = valid_trajectory()
    records[2]["raw_topics"]["plant_sim"]["captured_co2_kg"] += 0.05
    assert check_carbon_ledger(records).status == FAIL


def test_carbon_ledger_catches_co2_vanishing():
    records = valid_trajectory()
    records[2]["co2_storage_kg"] -= 0.05
    result = check_carbon_ledger(records)
    assert result.status == FAIL
    assert result.worst_step == 2


def test_oxygen_ledger_catches_o2_appearing_without_generation():
    records = valid_trajectory()
    records[1]["o2_storage_kg"] += 0.3
    assert check_oxygen_ledger(records).status == FAIL


def test_water_ledger_catches_water_appearing_in_the_product_tank():
    records = valid_trajectory()
    records[1]["product_water_reserve_l"] += 2.0
    assert check_water_ledger(records).status == FAIL


def test_water_ledger_ignores_unrecoverable_crew_water():
    """It is a diagnostic, not a pool flow; counting it double-subtracts."""
    records = valid_trajectory()
    for record in records[1:]:
        record["raw_topics"]["plant_sim"]["total_unrecoverable_crew_water_l"] += 5.0
    assert check_water_ledger(records).status == PASS


def _consistent_ogs(records, *, o2_generated=0.2, co2_used=0.05):
    """Totals for one OGS pass that satisfies all four identities.

    Written out rather than hand-tuned: the hydrogen term caught this fixture
    being physically impossible when it was added, which is what it is for.
    """
    plant = records[-1]["raw_topics"]["plant_sim"]
    plant["total_o2_generated_kg"] = o2_generated
    plant["total_electrolysis_water_kg"] = o2_generated * WATER_PER_O2
    plant["total_sabatier_co2_used_kg"] = co2_used
    plant["total_water_regenerated_l"] = co2_used * (H2O_PER_H2 / CO2_PER_H2)
    plant["total_ch4_vented_kg"] = co2_used * (CH4_PER_H2 / CO2_PER_H2)
    # H2 made by electrolysis, minus what Sabatier consumed, is what is vented.
    plant["total_h2_vented_kg"] = o2_generated * H2_PER_O2 - co2_used / CO2_PER_H2
    return plant


def test_stoichiometric_residual_accepts_a_consistent_ogs_pass():
    records = valid_trajectory()
    _consistent_ogs(records)
    assert check_stoichiometric_residual(records).status == PASS


def test_stoichiometric_residual_catches_hydrogen_from_nowhere():
    """The other three identities can all hold while H2 is invented: 1000 kg
    of it vented from 0.2 kg of O2 used to pass."""
    records = valid_trajectory()
    plant = _consistent_ogs(records)
    plant["total_h2_vented_kg"] = 1000.0
    result = check_stoichiometric_residual(records)
    assert result.status == FAIL
    assert result.violations[0]["identity"] == "hydrogen balance"


def test_stoichiometric_residual_catches_hydrogen_quietly_destroyed():
    records = valid_trajectory()
    plant = _consistent_ogs(records)
    plant["total_h2_vented_kg"] = 0.0
    assert check_stoichiometric_residual(records).status == FAIL


def test_stoichiometric_residual_catches_a_wrong_electrolysis_ratio():
    """Mass can still balance while the chemistry is wrong."""
    records = valid_trajectory()
    plant = records[2]["raw_topics"]["plant_sim"]
    plant["total_o2_generated_kg"] = 0.2
    plant["total_electrolysis_water_kg"] = 0.2 * WATER_PER_O2 * 1.10
    result = check_stoichiometric_residual(records)
    assert result.status == FAIL
    assert "electrolysis" in result.violations[0]["identity"]


def test_failure_quiescence_catches_ogs_processing_while_down():
    records = valid_trajectory()
    for record in records[1:]:
        record["ogs_failure_enabled"] = True
    records[2]["raw_topics"]["plant_sim"]["total_o2_generated_kg"] = 0.2
    result = check_failure_quiescence(records)
    assert result.status == FAIL
    assert result.violations[0]["subsystem"] == "ogs"


def test_failure_quiescence_catches_ars_removing_co2_while_down():
    """ARS has no total only it writes, so conservation catches it instead."""
    records = valid_trajectory()
    for record in records[1:]:
        record["ars_failure_enabled"] = True
    result = check_failure_quiescence(records)
    assert result.status == FAIL
    assert result.violations[0]["subsystem"] == "ars"
    assert result.violations[0]["co2_removed_kg"] == pytest.approx(0.5)


def test_failure_quiescence_ignores_a_subsystem_down_at_only_one_end():
    """Coarse sampling is not evidence of a violation."""
    records = valid_trajectory()
    records[1]["ars_failure_enabled"] = True
    assert check_failure_quiescence(records).status == PASS


def test_non_negative_catches_an_emptied_tank_going_past_zero():
    records = valid_trajectory()
    records[2]["o2_storage_kg"] = -0.4
    result = check_inventories_non_negative(records)
    assert result.status == FAIL
    assert result.violations[0]["field"] == "o2_storage_kg"


def test_non_negative_tolerates_a_clamp_sized_undershoot():
    records = valid_trajectory()
    records[2]["o2_storage_kg"] = -1e-13
    assert check_inventories_non_negative(records).status == PASS


def test_monotonic_catches_a_cumulative_total_going_backwards():
    records = valid_trajectory()
    records[2]["raw_topics"]["plant_sim"]["total_co2_generated_kg"] = 0.05
    result = check_totals_monotonic(records)
    assert result.status == FAIL
    assert result.violations[0]["field"] == "total_co2_generated_kg"


def test_missing_reading_is_caught():
    records = valid_trajectory()
    del records[1]["o2_storage_kg"]
    result = check_readings_present_and_finite(records)
    assert result.status == FAIL
    assert result.violations[0]["reason"] == "missing"


def test_non_finite_reading_is_caught():
    records = valid_trajectory()
    records[1]["product_water_reserve_l"] = float("nan")
    assert check_readings_present_and_finite(records).status == FAIL


# --------------------------------------------------------------------------- #
# retroactive form
# --------------------------------------------------------------------------- #
def _strip_totals(records):
    stripped = copy.deepcopy(records)
    for record in stripped:
        plant = record["raw_topics"]["plant_sim"]
        for name in list(plant):
            if name.startswith("total_"):
                del plant[name]
    return stripped


def test_a_run_without_totals_is_not_failed_for_lacking_them(tmp_path):
    """"Not recorded" must not read as "physics violated"."""
    result = evaluate_physics_gate(_write(tmp_path, _strip_totals(valid_trajectory())))
    assert result["verdict"] == PASS
    assert result["form"] == "retroactive"
    assert set(result["skipped_checks"]) == {
        "totals_monotonic",
        "carbon_ledger",
        "oxygen_ledger",
        "water_ledger",
        "stoichiometric_residual",
        "failure_quiescence",
        "capacity_bounds",
    }


def test_the_retroactive_form_still_catches_a_negative_inventory(tmp_path):
    records = _strip_totals(valid_trajectory())
    records[2]["product_water_reserve_l"] = -3.0
    result = evaluate_physics_gate(_write(tmp_path, records))
    assert result["verdict"] == FAIL
    assert result["failed_checks"] == ["inventories_non_negative"]


def test_ledger_skip_names_the_field_it_needed():
    records = _strip_totals(valid_trajectory())
    result = check_carbon_ledger(records)
    assert result.status == SKIPPED
    assert "total_co2_generated_kg" in result.detail


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def test_a_run_with_no_telemetry_is_an_error_not_a_pass(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(TelemetryUnreadable):
        evaluate_physics_gate(run_dir)


def test_an_empty_telemetry_file_is_an_error_not_a_pass(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "telemetry.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(TelemetryUnreadable):
        evaluate_physics_gate(run_dir)


# --------------------------------------------------------------------------- #
# backends that model fewer pools
# --------------------------------------------------------------------------- #
def mock_backend_trajectory():
    """What the ``mock`` backend actually emits.

    No grey-water loop, no ``raw_topics`` at all -- shape taken from a real
    ``ea run ssos_eclss_loop --backend mock`` trajectory.
    """
    return [
        {
            "step": step,
            "co2_storage_kg": 1.3 + 0.06 * step,
            "o2_storage_kg": 2.98 - 0.01 * step,
            "product_water_reserve_l": 51.0 - 0.2 * step,
            "ars_failure_enabled": False,
            "ogs_failure_enabled": False,
            "wrs_failure_enabled": False,
        }
        for step in range(4)
    ]


def test_a_backend_without_a_grey_water_loop_is_not_failed_for_lacking_one(tmp_path):
    """mock models no grey water; requiring it fails runs for what they never had."""
    result = evaluate_physics_gate(_write(tmp_path, mock_backend_trajectory()))
    assert result["verdict"] == PASS
    assert result["form"] == "retroactive"
    assert "readings_present_and_finite" not in result["failed_checks"]


def test_a_backend_without_a_grey_water_loop_still_gets_the_checks_it_can_have(tmp_path):
    records = mock_backend_trajectory()
    records[2]["o2_storage_kg"] = -0.5
    result = evaluate_physics_gate(_write(tmp_path, records))
    assert result["verdict"] == FAIL
    assert result["failed_checks"] == ["inventories_non_negative"]


def test_a_missing_reading_every_backend_emits_is_still_a_failure(tmp_path):
    records = mock_backend_trajectory()
    del records[1]["co2_storage_kg"]
    result = evaluate_physics_gate(_write(tmp_path, records))
    assert result["verdict"] == FAIL
    assert result["failed_checks"] == ["readings_present_and_finite"]


def test_the_water_ledger_skips_rather_than_raises_without_a_grey_water_pool():
    """Declared needs, not discovered ones: no KeyError on the first access."""
    records = mock_backend_trajectory()
    for record in records:
        record["raw_topics"] = {"plant_sim": dict(ZERO_TOTALS)}
    result = check_water_ledger(records)
    assert result.status == SKIPPED
    assert "grey_water_collected_l" in result.detail


# --------------------------------------------------------------------------- #
# every ledger term defended
# --------------------------------------------------------------------------- #
def _plant_driven_trajectory(steps=25):
    """Telemetry generated by driving PlantModel through every mutating op.

    The hand-written fixture above leaves all subsystem and service totals at
    zero, so any ledger term multiplied by a zero delta is untested — eight of
    them could be deleted with the suite still green. Here OGS, WRS, Sabatier
    and all four resource services actually run, so every term carries a
    non-zero delta and dropping one breaks the balance.
    """
    from environment.ssos.eclss.plant_sim.backend import PlantSimEclssBackend

    backend = PlantSimEclssBackend()
    model = backend.model
    records = []
    for step in range(steps):
        model.advance_step()
        # OGS before ARS on purpose. Sabatier consumes every gram of H2 it can
        # find CO2 for, so with a full capture tank total_h2_vented_kg stays at
        # zero and the hydrogen identity is never exercised. Electrolysing
        # while the tank is empty is what makes H2 vent.
        if step % 3 == 0:
            model.run_ogs(0.05)
        if step % 2 == 0:
            model.run_ars(1.8)
        if step % 4 == 0:
            model.run_wrs(1.0)
        if step % 5 == 0:
            model.request_o2(0.001)
            model.request_co2(0.001)
            model.request_product_water(0.2)
            model.submit_grey_water(0.3)
        snap = backend.poll_telemetry()
        records.append({"step": step, **snap.to_dict()})
    return records


def test_a_plant_driven_trajectory_closes_every_ledger():
    records = _plant_driven_trajectory()
    plant = _plant(records[-1])
    # The fixture is only a defence if every term actually moved.
    for name in (
        "total_co2_delivered_kg",
        "total_sabatier_co2_used_kg",
        "total_o2_delivered_kg",
        "total_external_grey_water_submitted_l",
        "total_product_water_delivered_l",
        "total_wrs_brine_loss_l",
        "total_water_regenerated_l",
        "total_electrolysis_water_kg",
        "total_h2_vented_kg",
    ):
        assert float(plant[name]) > 0.0, f"{name} never moved; the ledger term is undefended"
    for check in (
        check_carbon_ledger,
        check_oxygen_ledger,
        check_water_ledger,
        check_stoichiometric_residual,
    ):
        assert check(records).status == PASS, check.__name__


def test_the_plant_driven_trajectory_still_catches_an_injection(tmp_path):
    """It must not pass everything — the same trajectory with mass added fails."""
    records = _plant_driven_trajectory()
    records[-1]["co2_storage_kg"] = float(records[-1]["co2_storage_kg"]) + 0.5
    assert check_carbon_ledger(records).status == FAIL
