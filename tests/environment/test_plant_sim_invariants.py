"""Invariant / conservation / determinism tests (CP1 / Phase 1)."""

from __future__ import annotations

from dataclasses import fields

import pytest

from environment.ssos.eclss.plant_sim.config import PlantSimConfig
from environment.ssos.eclss.plant_sim.model import PlantModel

APPROX = dict(rel=1e-9, abs=1e-9)

_INVENTORY_FIELDS = (
    "cabin_co2_kg",
    "captured_co2_kg",
    "cabin_o2_kg",
    "product_water_l",
    "urine_buffer_l",
    "grey_water_l",
)


def _run_closed_loop(steps: int, cfg: PlantSimConfig | None = None) -> PlantModel:
    """Simulate an agent that drives every subsystem once per step."""
    m = PlantModel(cfg or PlantSimConfig())
    for step in range(steps):
        if step > 0:
            m.advance_step()
        m.run_ars(m.config.ars_reference_goal_co2_kg)
        m.run_wrs(2.0)
        m.run_ogs(0.06)
    return m


def test_72_step_run_stays_finite_and_nonnegative():
    m = _run_closed_loop(72)
    s = m.state
    import math

    for f in fields(s):
        assert math.isfinite(getattr(s, f.name)), f.name
    for name in _INVENTORY_FIELDS:
        assert getattr(s, name) >= 0.0, name


def test_scenario_co2_conservation():
    m = _run_closed_loop(72)
    s = m.state
    c = m.config
    total_in = c.initial_cabin_co2_kg + c.initial_captured_co2_kg + s.total_co2_generated_kg
    total_out = (
        s.cabin_co2_kg
        + s.captured_co2_kg
        + s.total_co2_vented_kg
        + s.total_co2_delivered_kg
        + s.total_sabatier_co2_used_kg
    )
    assert total_in == pytest.approx(total_out, **APPROX)


def test_scenario_water_conservation():
    m = _run_closed_loop(72)
    s = m.config, m.state
    cfg, st = s
    # product_water balance:
    #   final = initial - crew drink - electrolysis + WRS recovery + Sabatier regen
    expected = (
        cfg.initial_product_water_l
        - st.total_potable_water_consumed_l
        - st.total_electrolysis_water_kg
        + st.total_wrs_recovered_water_l
        + st.total_water_regenerated_l
        - st.total_product_water_delivered_l
    )
    assert st.product_water_l == pytest.approx(expected, **APPROX)


def test_run_is_deterministic():
    a = _run_closed_loop(50)
    b = _run_closed_loop(50)
    for f in fields(a.state):
        assert getattr(a.state, f.name) == getattr(b.state, f.name), f.name


def test_nominal_no_shortfall_over_24h():
    # 72 steps * 1200 s = 24 h, with generous starting water/o2
    m = _run_closed_loop(72, PlantSimConfig(initial_cabin_o2_kg=2.0, initial_product_water_l=150.0))
    assert m.state.total_o2_shortfall_kg == pytest.approx(0.0, abs=1e-9)
    assert m.state.total_water_shortfall_l == pytest.approx(0.0, abs=1e-9)


def test_cabin_co2_does_not_diverge():
    m = _run_closed_loop(72, PlantSimConfig(initial_cabin_o2_kg=2.0, initial_product_water_l=150.0))
    # ARS capacity (4.5 kg/day) exceeds crew generation (4.16 kg/day) -> bounded
    assert m.state.cabin_co2_kg < 2.5
