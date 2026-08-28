"""Unit tests for the pure plant-sim model (CP1 / Phase 1)."""

from __future__ import annotations

import math

import pytest

from environment.ssos.eclss.plant_sim.config import PlantConfigError, PlantSimConfig
from environment.ssos.eclss.plant_sim.model import PlantModel, per_interval
from environment.ssos.eclss.plant_sim.stoichiometry import (
    CH4_PER_H2,
    CO2_PER_H2,
    H2_PER_O2,
    H2O_PER_H2,
    WATER_PER_O2,
)

APPROX = dict(rel=1e-9, abs=1e-12)


def _model(**overrides) -> PlantModel:
    return PlantModel(PlantSimConfig(**overrides))


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_default_config_is_valid():
    cfg = PlantSimConfig()
    assert cfg.crew_size == 4
    assert cfg.step_seconds == 1200.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"ars_capture_efficiency": 1.5},
        {"sabatier_conversion_efficiency": -0.1},
        {"step_seconds": 0.0},
        {"step_seconds": float("inf")},
        {"crew_size": -1},
        {"initial_cabin_o2_kg": -0.1},
        {"potable_water_kg_day_person": 3.0},  # breaks crew water balance
    ],
)
def test_invalid_config_rejected(overrides):
    with pytest.raises(PlantConfigError):
        PlantSimConfig(**overrides)


def test_from_scenario_config_requires_crew_size():
    with pytest.raises(PlantConfigError, match="plant_sim.crew.size"):
        PlantSimConfig.from_scenario_config({"simulation": {"initial_o2_storage_kg": 1.0}})


def test_capacity_drop_o2_floor_and_no_revival():
    m = _model(
        crew_size=4,
        survival_enabled=True,
        initial_cabin_o2_kg=0.03,
        initial_product_water_l=100.0,
        initial_cabin_co2_kg=0.1,
        cabin_co2_critical_kg=2.2,
    )
    per = m.per_person_o2_demand_kg()
    expected = int(0.03 // per)
    result = m.apply_capacity_drop()
    assert m.state.crew_alive == expected
    assert result["lost_this_step"] == 4 - expected
    assert "o2" in result["limiting"]
    m.state.cabin_o2_kg = 10.0
    m.apply_capacity_drop()
    assert m.state.crew_alive == expected


def test_capacity_drop_co2_critical_does_not_cut_crew():
    m = _model(
        crew_size=4,
        survival_enabled=True,
        initial_cabin_o2_kg=10.0,
        initial_product_water_l=100.0,
        initial_cabin_co2_kg=2.2,
        cabin_co2_critical_kg=2.2,
    )
    result = m.apply_capacity_drop()
    assert m.state.crew_alive == 4
    assert result["lost_this_step"] == 0
    assert "co2" not in result["limiting"]


def test_capacity_drop_attributes_lost_to_o2_when_both_bind():
    m = _model(
        crew_size=4,
        survival_enabled=True,
        initial_cabin_o2_kg=10.0,
        initial_product_water_l=100.0,
    )
    o2_pp = m.per_person_o2_demand_kg()
    water_pp = m.per_person_water_demand_l()
    m.state.cabin_o2_kg = 2 * o2_pp
    m.state.product_water_l = 1 * water_pp
    result = m.apply_capacity_drop()
    assert m.state.crew_alive == 1
    assert result["lost_this_step"] == 3
    assert result["limiting"] == ["o2", "water"]
    assert m.state.crew_lost_o2 == 3
    assert m.state.crew_lost_water == 0
    assert m.state.crew_lost_total == 3


def test_metabolism_scales_with_crew_alive_when_survival_enabled():
    full = _model(crew_size=4, survival_enabled=True, initial_cabin_o2_kg=10.0)
    half = _model(crew_size=4, survival_enabled=True, initial_cabin_o2_kg=10.0)
    half.state.crew_alive = 2
    full.advance_step()
    half.advance_step()
    assert half.state.total_co2_generated_kg == pytest.approx(
        0.5 * full.state.total_co2_generated_kg, **APPROX
    )


def test_zero_crew_has_zero_metabolism():
    m = _model(crew_size=4, survival_enabled=True)
    m.state.crew_alive = 0
    before = m.state.copy()
    metab = m.advance_step()
    assert metab["co2_generated_kg"] == pytest.approx(0.0, abs=1e-12)
    assert m.state.cabin_co2_kg == pytest.approx(before.cabin_co2_kg, **APPROX)


def test_from_scenario_config_merges_and_defaults():
    cfg = PlantSimConfig.from_scenario_config(
        {
            "simulation": {"initial_o2_storage_kg": 1.0},
            "plant_sim": {"crew": {"size": 6}, "ars": {"capture_efficiency": 0.9}},
        }
    )
    assert cfg.crew_size == 6
    assert cfg.ars_capture_efficiency == 0.9
    assert cfg.initial_cabin_o2_kg == 1.0
    assert cfg.step_seconds == 1200.0  # default preserved


# --------------------------------------------------------------------------- #
# advance_step (crew metabolism)
# --------------------------------------------------------------------------- #
def test_advance_step_expected_rates():
    m = _model()
    c = m.config
    factor = c.crew_size * c.activity_factor
    before = m.state.copy()

    m.advance_step()
    s = m.state

    assert s.cabin_co2_kg == pytest.approx(
        before.cabin_co2_kg + per_interval(c.co2_kg_day_person, c.step_seconds) * factor, **APPROX
    )
    assert s.cabin_o2_kg == pytest.approx(
        before.cabin_o2_kg - per_interval(c.o2_kg_day_person, c.step_seconds) * factor, **APPROX
    )
    assert s.simulation_time_s == pytest.approx(c.step_seconds)


def test_advance_step_scales_linearly_with_time():
    a = _model(step_seconds=1200.0)
    b = _model(step_seconds=2400.0)
    a.advance_step()
    b.advance_step()
    assert b.state.total_co2_generated_kg == pytest.approx(2 * a.state.total_co2_generated_kg, **APPROX)


def test_crew_water_ledger_closes():
    m = _model()
    for _ in range(10):
        m.advance_step()
    s = m.state
    assert s.total_potable_water_consumed_l == pytest.approx(
        s.total_urine_generated_l
        + s.total_condensate_generated_l
        + s.total_unrecoverable_crew_water_l,
        **APPROX,
    )


def test_crew_does_not_produce_water_when_dehydrated():
    m = _model(initial_product_water_l=0.0)
    m.advance_step()
    s = m.state
    assert s.urine_buffer_l == pytest.approx(0.0, abs=1e-12)
    assert s.grey_water_l == pytest.approx(0.0, abs=1e-12)
    assert s.total_water_shortfall_l > 0.0


def test_o2_shortfall_recorded_when_depleted():
    m = _model(initial_cabin_o2_kg=0.0)
    m.advance_step()
    assert m.state.cabin_o2_kg == pytest.approx(0.0, abs=1e-12)
    assert m.state.total_o2_shortfall_kg > 0.0


# --------------------------------------------------------------------------- #
# ARS
# --------------------------------------------------------------------------- #
def test_ars_capacity_and_capture():
    m = _model(initial_cabin_co2_kg=5.0)
    r = m.run_ars(m.config.ars_reference_goal_co2_kg)  # scale = 1.0
    # Rated against the step, not against ars_operation_seconds: a 4800 s quantum
    # cannot bill four steps of throughput to the one step it happens in.
    cap = per_interval(m.config.ars_capacity_kg_day, m.config.step_seconds)
    assert r["co2_removed_kg"] == pytest.approx(cap, **APPROX)
    assert r["elapsed_seconds"] == pytest.approx(m.config.step_seconds, **APPROX)
    assert r["captured_co2_kg"] == pytest.approx(cap * m.config.ars_capture_efficiency, **APPROX)
    assert r["captured_co2_kg"] + r["vented_co2_kg"] == pytest.approx(r["co2_removed_kg"], **APPROX)


def test_ars_goal_cannot_buy_capacity():
    """The goal says "more urgently". It used to say "with a bigger machine".

    This test asserted the opposite until the rated-capacity invariant landed:
    scale = 2.0 removed twice the rating. That is the hole an audit walked through
    with initial_co2_mass = 1800, which bought 1000x rated and was reported as an
    improvement. The goal is still carried and still reported as ``ordered_kg``;
    it just no longer moves the ceiling.
    """
    cap = per_interval(PlantSimConfig().ars_capacity_kg_day, PlantSimConfig().step_seconds)
    for scale in (2.0, 1000.0):
        m = _model(initial_cabin_co2_kg=5.0e6)
        r = m.run_ars(scale * m.config.ars_reference_goal_co2_kg)
        assert r["goal_scale"] == pytest.approx(scale, **APPROX)
        assert r["ordered_kg"] > cap  # the order is preserved, and visible
        assert r["co2_removed_kg"] == pytest.approx(cap, **APPROX)
        assert r["limited_by"] == "rated_step_capacity"


def test_ars_rating_is_shared_across_actions_in_one_step():
    """A per-action bound is defeated by issuing the action twice (EXP-012)."""
    m = _model(initial_cabin_co2_kg=5.0)
    cap = per_interval(m.config.ars_capacity_kg_day, m.config.step_seconds)
    total = sum(m.run_ars(m.config.ars_reference_goal_co2_kg)["co2_removed_kg"] for _ in range(6))
    assert total == pytest.approx(cap, **APPROX)
    assert m.state.co2_removed_this_step_kg == pytest.approx(cap, **APPROX)


def test_advance_step_restores_the_rated_allowance():
    m = _model(initial_cabin_co2_kg=5.0)
    cap = per_interval(m.config.ars_capacity_kg_day, m.config.step_seconds)
    m.run_ars(m.config.ars_reference_goal_co2_kg)
    assert m.run_ars(m.config.ars_reference_goal_co2_kg)["co2_removed_kg"] == pytest.approx(
        0.0, abs=1e-15
    )
    m.advance_step()
    assert m.state.co2_removed_this_step_kg == pytest.approx(0.0, abs=1e-15)
    assert m.run_ars(m.config.ars_reference_goal_co2_kg)["co2_removed_kg"] == pytest.approx(
        cap, **APPROX
    )


def test_ars_cannot_remove_more_than_inventory():
    m = _model(initial_cabin_co2_kg=0.05)
    r = m.run_ars(m.config.ars_reference_goal_co2_kg)
    assert r["co2_removed_kg"] == pytest.approx(0.05, **APPROX)
    assert m.state.cabin_co2_kg == pytest.approx(0.0, abs=1e-12)
    assert r["limited_by"] == "cabin_co2_inventory"


# --------------------------------------------------------------------------- #
# OGS + Sabatier
# --------------------------------------------------------------------------- #
def test_ogs_electrolysis_stoichiometry():
    m = _model(initial_captured_co2_kg=0.0)
    r = m.run_ogs(0.06)
    assert r["processed_water_kg"] == pytest.approx(0.06, **APPROX)
    assert r["o2_generated_kg"] == pytest.approx(0.06 / WATER_PER_O2, **APPROX)
    assert r["h2_generated_kg"] == pytest.approx(r["o2_generated_kg"] * H2_PER_O2, **APPROX)
    # no captured CO2 -> all H2 vented, no water regenerated
    assert r["sabatier_h2_used_kg"] == pytest.approx(0.0, abs=1e-15)
    assert r["h2_vented_kg"] == pytest.approx(r["h2_generated_kg"], **APPROX)
    assert r["water_regenerated_kg"] == pytest.approx(0.0, abs=1e-15)


def test_ogs_capacity_capped():
    m = _model(initial_product_water_l=1000.0)
    max_o2 = per_interval(m.config.ogs_max_o2_kg_day, m.config.ogs_operation_seconds)
    max_water = max_o2 * WATER_PER_O2
    r = m.run_ogs(999.0)
    assert r["processed_water_kg"] == pytest.approx(max_water, **APPROX)
    assert r["limited_by"] == ["ogs_capacity"]


def test_ogs_limited_by_available_water():
    m = _model(initial_product_water_l=0.01)
    r = m.run_ogs(5.0)
    assert r["processed_water_kg"] == pytest.approx(0.01, **APPROX)
    assert r["limited_by"] == ["product_water"]


def test_sabatier_full_reaction_when_co2_available():
    m = _model(initial_captured_co2_kg=10.0)
    r = m.run_ogs(0.06)
    h2 = r["h2_generated_kg"]
    assert r["sabatier_h2_used_kg"] == pytest.approx(h2, **APPROX)
    assert r["sabatier_co2_used_kg"] == pytest.approx(h2 * CO2_PER_H2, **APPROX)
    assert r["water_regenerated_kg"] == pytest.approx(h2 * H2O_PER_H2, **APPROX)
    assert r["ch4_generated_kg"] == pytest.approx(h2 * CH4_PER_H2, **APPROX)
    # Sabatier mass balance: CO2 + 4H2 -> CH4 + 2H2O.
    # Accept ≤ 2 mg per action (1000 actions → 2 g). Inventory bookkeeping is exact.
    reactants = r["sabatier_co2_used_kg"] + r["sabatier_h2_used_kg"]
    products = r["water_regenerated_kg"] + r["ch4_generated_kg"]
    assert abs(reactants - products) < 2e-6  # 2 mg


def test_sabatier_partial_when_co2_limited():
    # tiny captured CO2 limits the reaction below H2 availability
    m = _model(initial_captured_co2_kg=0.0005)
    r = m.run_ogs(0.06)
    assert 0.0 < r["sabatier_h2_used_kg"] < r["h2_generated_kg"]
    assert m.state.captured_co2_kg == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------------- #
# WRS
# --------------------------------------------------------------------------- #
def test_wrs_recovers_from_buffers():
    # Feeds sized under the step rating (13.5 L/day -> 0.1875 L/step), so this
    # test still measures the recovery ledger and not the new ceiling.
    m = _model(initial_urine_buffer_l=5.0, initial_grey_water_l=0.05)
    r = m.run_wrs(0.05)
    assert r["urine_feed_l"] == pytest.approx(0.05, **APPROX)
    assert r["grey_feed_l"] == pytest.approx(0.05, **APPROX)
    assert r["urine_recovered_l"] == pytest.approx(0.05 * m.config.wrs_urine_recovery, **APPROX)
    assert r["grey_recovered_l"] == pytest.approx(0.05 * m.config.wrs_grey_recovery, **APPROX)
    assert r["fully_satisfied"] is True
    # WRS ledger: feed = recovered + brine
    assert r["urine_feed_l"] + r["grey_feed_l"] == pytest.approx(
        r["recovered_water_l"] + r["brine_loss_l"], **APPROX
    )


def test_wrs_does_not_create_water_beyond_buffer():
    m = _model(initial_urine_buffer_l=0.0, initial_grey_water_l=0.0)
    before_water = m.state.product_water_l
    r = m.run_wrs(2.0)  # goal asks for 2 L urine, but buffer is empty
    assert r["has_feed"] is False
    assert r["recovered_water_l"] == pytest.approx(0.0, abs=1e-15)
    assert m.state.product_water_l == pytest.approx(before_water, **APPROX)


def test_wrs_capacity_limits_total_feed():
    """WRS had no throughput rating at all -- only a 10 L batch cap, which is 80x
    what crew 4 puts into the buffers in a step. This asserted that batch cap."""
    m = _model(initial_urine_buffer_l=100.0, initial_grey_water_l=100.0)
    rated = per_interval(m.config.wrs_capacity_l_day, m.config.step_seconds)
    r = m.run_wrs(100.0)
    assert rated < m.config.wrs_max_feed_l_per_operation  # the rating binds first
    assert r["urine_feed_l"] + r["grey_feed_l"] == pytest.approx(rated, **APPROX)
    assert r["processed_feed_l"] == pytest.approx(rated, **APPROX)
    assert r["limited_by"] == ["wrs_capacity"]


def test_wrs_rating_is_shared_across_actions_in_one_step():
    m = _model(initial_urine_buffer_l=100.0, initial_grey_water_l=100.0)
    rated = per_interval(m.config.wrs_capacity_l_day, m.config.step_seconds)
    total = sum(m.run_wrs(100.0)["processed_feed_l"] for _ in range(4))
    assert total == pytest.approx(rated, **APPROX)
    m.advance_step()
    assert m.run_wrs(100.0)["processed_feed_l"] == pytest.approx(rated, **APPROX)


def test_wrs_reports_an_empty_buffer():
    m = _model(initial_urine_buffer_l=0.0, initial_grey_water_l=0.0)
    r = m.run_wrs(0.05)
    assert r["fully_satisfied"] is False
    assert r["limited_by"] == ["urine_buffer"]


# --------------------------------------------------------------------------- #
# services
# --------------------------------------------------------------------------- #
def test_request_co2_draws_only_captured():
    m = _model(initial_captured_co2_kg=1.0, initial_cabin_co2_kg=2.0)
    granted = m.request_co2(0.4)
    assert granted == pytest.approx(0.4, **APPROX)
    assert m.state.captured_co2_kg == pytest.approx(0.6, **APPROX)
    assert m.state.cabin_co2_kg == pytest.approx(2.0, **APPROX)  # untouched


def test_request_partial_when_short():
    m = _model(initial_cabin_o2_kg=0.1)
    granted = m.request_o2(0.5)
    assert granted == pytest.approx(0.1, **APPROX)
    assert m.state.cabin_o2_kg == pytest.approx(0.0, abs=1e-12)


def test_submit_grey_water_adds_and_records_external():
    m = _model()
    m.submit_grey_water(3.0)
    assert m.state.grey_water_l == pytest.approx(3.0, **APPROX)
    assert m.state.total_external_grey_water_submitted_l == pytest.approx(3.0, **APPROX)
