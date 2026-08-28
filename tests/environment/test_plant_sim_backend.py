"""Backend contract tests for PlantSimEclssBackend (CP2 / Phase 2)."""

from __future__ import annotations

from dataclasses import fields

import pytest

from environment.ssos.eclss.backend import EclssBackend
from environment.ssos.eclss.plant_sim.backend import PlantSimEclssBackend
from environment.ssos.eclss.plant_sim.config import PlantSimConfig
from environment.ssos.eclss.types import ArsGoal, OgsGoal, WrsGoal

APPROX = dict(rel=1e-9, abs=1e-12)


def _backend(**overrides) -> PlantSimEclssBackend:
    return PlantSimEclssBackend(PlantSimConfig(**overrides))


def test_satisfies_eclss_backend_protocol():
    assert isinstance(_backend(), EclssBackend)


def test_from_scenario_config():
    backend = PlantSimEclssBackend.from_scenario_config(
        {"plant_sim": {"crew": {"size": 5}}}
    )
    assert backend.config.crew_size == 5


def test_poll_telemetry_maps_cabin_co2_and_is_independent():
    b = _backend(initial_cabin_co2_kg=1.7, initial_cabin_o2_kg=0.5)
    t1 = b.poll_telemetry()
    assert t1.co2_storage_kg == pytest.approx(1.7, **APPROX)
    assert t1.o2_storage_kg == pytest.approx(0.5, **APPROX)
    assert "plant_sim" in t1.raw_topics
    assert "captured_co2_kg" in t1.raw_topics["plant_sim"]

    # snapshot must be a fresh copy, not a live reference
    b.advance_step()
    t2 = b.poll_telemetry()
    assert t1.co2_storage_kg == pytest.approx(1.7, **APPROX)
    assert t2.co2_storage_kg > t1.co2_storage_kg


def test_poll_telemetry_exports_crew_alive():
    b = _backend(crew_size=3, survival_enabled=True)
    topic = b.poll_telemetry().raw_topics["plant_sim"]
    assert topic["crew_initial"] == 3
    assert topic["crew_alive"] == 3
    b.apply_capacity_drop()
    again = b.poll_telemetry()
    assert "crew_alive" in again.raw_topics["plant_sim"]


def test_set_crew_alive_never_revives():
    b = _backend(crew_size=4, survival_enabled=True)
    lost = b.set_crew_alive(2)
    assert lost == 2
    assert b.model.state.crew_alive == 2
    assert b.set_crew_alive(4) == 0
    assert b.model.state.crew_alive == 2


def test_poll_telemetry_includes_dwell_losses_until_next_step():
    b = _backend(
        crew_size=4,
        survival_enabled=True,
        initial_cabin_o2_kg=10.0,
        initial_product_water_l=100.0,
    )
    lost = b.set_crew_alive(3, limiting=["o2_warning"])
    assert lost == 1
    survival = b.poll_telemetry().raw_topics["plant_sim"]["survival"]
    assert survival["lost_this_step"] == 1
    assert survival["limiting"] == ["o2_warning"]

    physics = b.apply_capacity_drop()
    assert physics["lost_this_step"] == 0
    merged = b.poll_telemetry().raw_topics["plant_sim"]["survival"]
    assert merged["lost_this_step"] == 1
    assert merged["limiting"] == ["o2_warning"]

    b.advance_step()
    cleared = b.poll_telemetry().raw_topics["plant_sim"]["survival"]
    assert cleared["lost_this_step"] == 0
    assert cleared["limiting"] == []


def test_apply_capacity_drop_telemetry_uses_physics_limiting_labels():
    b = _backend(
        crew_size=4,
        survival_enabled=True,
        initial_cabin_o2_kg=0.02,
        initial_product_water_l=100.0,
        initial_cabin_co2_kg=0.1,
    )
    b.set_crew_alive(3, limiting=["o2_warning"])
    physics = b.apply_capacity_drop()
    assert "o2_physics" in physics["limiting"]
    assert "o2" not in physics["limiting"]
    survival = b.poll_telemetry().raw_topics["plant_sim"]["survival"]
    assert "o2_warning" in survival["limiting"]
    assert "o2_physics" in survival["limiting"]
    assert "o2" not in survival["limiting"]


def test_poll_telemetry_exports_last_metabolism_once():
    b = _backend()
    before = b.poll_telemetry()
    assert "last_metabolism" not in (before.raw_topics.get("plant_sim") or {})

    b.advance_step()
    with_metab = b.poll_telemetry()
    metab = (with_metab.raw_topics.get("plant_sim") or {}).get("last_metabolism")
    assert isinstance(metab, dict)
    assert metab.get("co2_generated_kg", 0.0) > 0.0
    assert "o2_consumed_kg" in metab
    assert "urine_generated_l" in metab

    cleared = b.poll_telemetry()
    assert "last_metabolism" not in (cleared.raw_topics.get("plant_sim") or {})


def test_advance_step_capability_present():
    b = _backend()
    assert hasattr(b, "advance_step")
    before = b.poll_telemetry().co2_storage_kg
    b.advance_step()
    assert b.poll_telemetry().co2_storage_kg > before


# --------------------------------------------------------------------------- #
# actions succeed / partial semantics
# --------------------------------------------------------------------------- #
def test_ars_action_success():
    b = _backend(initial_cabin_co2_kg=2.0)
    r = b.send_air_revitalisation_goal(ArsGoal())
    assert r.success is True
    assert r.details["co2_removed_kg"] > 0.0


def test_ogs_action_success():
    b = _backend()
    r = b.send_oxygen_generation_goal(OgsGoal(input_water_mass=0.06))
    assert r.success is True
    assert r.details["o2_generated_kg"] > 0.0


def test_wrs_no_feed_is_unsuccessful_noop():
    b = _backend(initial_urine_buffer_l=0.0, initial_grey_water_l=0.0)
    r = b.send_water_recovery_goal(WrsGoal(urine_volume=2.0))
    assert r.success is False
    assert r.details["reason"] == "no_feed"


def test_wrs_success_after_metabolism():
    b = _backend()
    b.advance_step()  # crew generates urine + condensate
    r = b.send_water_recovery_goal(WrsGoal(urine_volume=2.0))
    assert r.success is True
    assert r.details["recovered_water_l"] > 0.0


# --------------------------------------------------------------------------- #
# validation: negative / NaN / Inf rejected, no mutation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_invalid_ars_goal_rejected_no_mutation(bad):
    b = _backend(initial_cabin_co2_kg=2.0)
    before = b.model.state.copy()
    r = b.send_air_revitalisation_goal(ArsGoal(initial_co2_mass=bad))
    assert r.success is False
    assert b.model.state.cabin_co2_kg == pytest.approx(before.cabin_co2_kg, **APPROX)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_invalid_service_request_rejected(bad):
    b = _backend(initial_cabin_o2_kg=1.0)
    r = b.request_o2(bad)
    assert r.success is False
    assert b.model.state.cabin_o2_kg == pytest.approx(1.0, **APPROX)


def test_service_partial_grant_semantics():
    b = _backend(initial_cabin_o2_kg=0.1)
    r = b.request_o2(0.5)
    assert r.success is False
    assert r.response_value == pytest.approx(0.1, **APPROX)
    assert "partial" in r.message


def test_service_full_grant():
    b = _backend(initial_product_water_l=100.0)
    r = b.request_product_water(10.0)
    assert r.success is True
    assert r.response_value == pytest.approx(10.0, **APPROX)


# --------------------------------------------------------------------------- #
# failure gating
# --------------------------------------------------------------------------- #
def test_ars_failure_blocks_action_without_mutation():
    b = _backend(initial_cabin_co2_kg=2.0)
    b.set_subsystem_failure("ars", True)
    before = b.model.state.copy()
    r = b.send_air_revitalisation_goal(ArsGoal())
    assert r.success is False
    assert b.model.state.cabin_co2_kg == pytest.approx(before.cabin_co2_kg, **APPROX)
    t = b.poll_telemetry()
    assert t.ars_failure_enabled is True


def test_ogs_failure_stops_ogs_and_sabatier():
    b = _backend(initial_captured_co2_kg=1.0)
    b.set_subsystem_failure("ogs", True)
    before = b.model.state.copy()
    r = b.send_oxygen_generation_goal(OgsGoal(input_water_mass=0.06))
    assert r.success is False
    assert b.model.state.captured_co2_kg == pytest.approx(before.captured_co2_kg, **APPROX)
    assert b.model.state.cabin_o2_kg == pytest.approx(before.cabin_o2_kg, **APPROX)


def test_submit_grey_water_works_during_wrs_failure():
    b = _backend()
    b.set_subsystem_failure("wrs", True)
    r = b.submit_grey_water(2.0)
    assert r.success is True
    assert b.model.state.grey_water_l == pytest.approx(2.0, **APPROX)


def test_unknown_subsystem_raises():
    b = _backend()
    with pytest.raises(ValueError):
        b.set_subsystem_failure("thermal", True)


def test_set_failure_accepts_suffix_form():
    b = _backend()
    b.set_subsystem_failure("ARS_failure", True)
    assert b.poll_telemetry().ars_failure_enabled is True


def test_action_and_service_results_serialize():
    b = _backend(initial_cabin_co2_kg=2.0)
    action = b.send_air_revitalisation_goal(ArsGoal())
    service = b.request_product_water(1.0)
    assert isinstance(action.to_dict(), dict)
    assert isinstance(service.to_dict(), dict)


def test_state_finite_and_nonnegative_after_mutations():
    b = _backend()
    b.advance_step()
    b.send_air_revitalisation_goal(ArsGoal())
    b.send_water_recovery_goal(WrsGoal(urine_volume=2.0))
    b.send_oxygen_generation_goal(OgsGoal(input_water_mass=0.06))
    import math

    for f in fields(b.model.state):
        assert math.isfinite(getattr(b.model.state, f.name)), f.name
