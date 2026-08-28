"""Mass-conservation checks for plant_sim chemistry and internal cycles.

Two layers are covered:

1. **Inventory bookkeeping** — every kg/L that leaves one plant tank must appear
   in another tank, a cumulative sink, or an external delivery ledger. These
   close to machine precision because the model uses the same ratios for both
   sides of each transfer.

2. **Reaction stoichiometry** — ratios come from tabulated molecular weights in
   ``stoichiometry.py``. Accept absolute residual up to ``CHEM_MASS_ACCEPT_KG``.
"""

from __future__ import annotations

import pytest

from environment.ssos.eclss.plant_sim.config import PlantSimConfig
from environment.ssos.eclss.plant_sim.model import PlantModel, water_kg_to_l, water_l_to_kg
from environment.ssos.eclss.plant_sim.stoichiometry import (
    CH4_PER_H2,
    CO2_PER_H2,
    H2_PER_O2,
    H2O_PER_H2,
    MW_CH4,
    MW_CO2,
    MW_H2,
    MW_H2O,
    MW_O2,
    WATER_PER_O2,
)

EXACT = dict(rel=1e-12, abs=1e-12)
# Per nominal OGS(+Sabatier) action (0.06 kg water): accept ≤ 2 mg chemical
# residual. Even 1000 such actions accumulate only 2 g — negligible vs crew
# metabolism (~58 g CO₂/step) at plant_sim fidelity.
CHEM_MASS_ACCEPT_KG = 2e-6  # 2 mg


def _model(**overrides) -> PlantModel:
    return PlantModel(PlantSimConfig(**overrides))


def _driven_loop(steps: int, *, with_services: bool = False, **overrides) -> PlantModel:
    m = PlantModel(PlantSimConfig(**overrides))
    for step in range(steps):
        if step > 0:
            m.advance_step()
        m.run_ars(m.config.ars_reference_goal_co2_kg)
        m.run_wrs(2.0)
        m.run_ogs(0.06)
        if with_services and step % 10 == 0:
            m.request_o2(0.01)
            m.request_co2(0.01)
            m.request_product_water(0.05)
            m.submit_grey_water(0.02)
    return m


# --------------------------------------------------------------------------- #
# stoichiometry constants
# --------------------------------------------------------------------------- #
def test_stoichiometry_ratios_match_molecular_weights():
    assert WATER_PER_O2 == pytest.approx((2 * MW_H2O) / MW_O2, **EXACT)
    assert H2_PER_O2 == pytest.approx((2 * MW_H2) / MW_O2, **EXACT)
    assert CO2_PER_H2 == pytest.approx(MW_CO2 / (4 * MW_H2), **EXACT)
    assert H2O_PER_H2 == pytest.approx((2 * MW_H2O) / (4 * MW_H2), **EXACT)
    assert CH4_PER_H2 == pytest.approx(MW_CH4 / (4 * MW_H2), **EXACT)


def test_electrolysis_chemical_residual_within_accept_budget():
    water_kg = 0.06
    o2_kg = water_kg / WATER_PER_O2
    h2_kg = o2_kg * H2_PER_O2
    residual_kg = abs(water_kg - (o2_kg + h2_kg))
    assert residual_kg < CHEM_MASS_ACCEPT_KG  # 2 mg
    assert abs((2 * MW_H2O) - (MW_O2 + 2 * MW_H2)) < 1e-3


def test_sabatier_chemical_residual_within_accept_budget():
    m = _model(initial_captured_co2_kg=10.0)
    r = m.run_ogs(0.06)
    reactants = r["sabatier_co2_used_kg"] + r["sabatier_h2_used_kg"]
    products = r["water_regenerated_kg"] + r["ch4_generated_kg"]
    residual_kg = abs(reactants - products)
    assert residual_kg < CHEM_MASS_ACCEPT_KG  # 2 mg
    assert abs((MW_CO2 + 4 * MW_H2) - (MW_CH4 + 2 * MW_H2O)) < 1e-4


# --------------------------------------------------------------------------- #
# per-operation inventory ledgers (exact)
# --------------------------------------------------------------------------- #
def test_ars_preserves_co2_mass_across_cabin_capture_and_vent():
    m = _model(initial_cabin_co2_kg=5.0, initial_captured_co2_kg=1.0)
    before = m.state.copy()
    r = m.run_ars(m.config.ars_reference_goal_co2_kg)
    s = m.state

    assert before.cabin_co2_kg - s.cabin_co2_kg == pytest.approx(r["co2_removed_kg"], **EXACT)
    assert s.captured_co2_kg - before.captured_co2_kg == pytest.approx(r["captured_co2_kg"], **EXACT)
    assert s.total_co2_vented_kg - before.total_co2_vented_kg == pytest.approx(
        r["vented_co2_kg"], **EXACT
    )
    assert r["captured_co2_kg"] + r["vented_co2_kg"] == pytest.approx(r["co2_removed_kg"], **EXACT)


def test_electrolysis_inventory_follows_stoichiometric_ratios():
    m = _model(initial_captured_co2_kg=0.0, initial_product_water_l=10.0)
    before = m.state.copy()
    r = m.run_ogs(0.06)
    s = m.state

    assert water_l_to_kg(before.product_water_l - s.product_water_l) == pytest.approx(
        r["processed_water_kg"], **EXACT
    )
    assert s.cabin_o2_kg - before.cabin_o2_kg == pytest.approx(r["o2_generated_kg"], **EXACT)
    assert r["o2_generated_kg"] == pytest.approx(r["processed_water_kg"] / WATER_PER_O2, **EXACT)
    assert r["h2_generated_kg"] == pytest.approx(r["o2_generated_kg"] * H2_PER_O2, **EXACT)
    # No Sabatier: all H2 vents; water tank only loses electrolysis feed.
    assert r["h2_vented_kg"] == pytest.approx(r["h2_generated_kg"], **EXACT)
    assert r["water_regenerated_kg"] == pytest.approx(0.0, abs=1e-15)


def test_h2_mass_splits_exactly_into_sabatier_use_and_vent():
    m = _model(initial_captured_co2_kg=10.0)
    r = m.run_ogs(0.06)
    assert r["sabatier_h2_used_kg"] + r["h2_vented_kg"] == pytest.approx(
        r["h2_generated_kg"], **EXACT
    )


def test_sabatier_inventory_matches_reaction_outputs():
    m = _model(initial_captured_co2_kg=10.0, initial_product_water_l=10.0)
    before = m.state.copy()
    r = m.run_ogs(0.06)
    s = m.state

    assert before.captured_co2_kg - s.captured_co2_kg == pytest.approx(
        r["sabatier_co2_used_kg"], **EXACT
    )
    # Net product-water change = -electrolysis + Sabatier regen
    expected_water_l = before.product_water_l - water_kg_to_l(r["processed_water_kg"]) + water_kg_to_l(
        r["water_regenerated_kg"]
    )
    assert s.product_water_l == pytest.approx(expected_water_l, **EXACT)
    assert s.total_ch4_vented_kg - before.total_ch4_vented_kg == pytest.approx(
        r["ch4_generated_kg"], **EXACT
    )
    assert s.total_h2_vented_kg - before.total_h2_vented_kg == pytest.approx(
        r["h2_vented_kg"], **EXACT
    )


def test_ogs_plus_sabatier_chemical_mass_within_accept_budget():
    m = _model(initial_captured_co2_kg=10.0)
    r = m.run_ogs(0.06)
    reactants = r["processed_water_kg"] + r["sabatier_co2_used_kg"]
    products = (
        r["o2_generated_kg"]
        + r["water_regenerated_kg"]
        + r["ch4_generated_kg"]
        + r["h2_vented_kg"]
    )
    assert abs(reactants - products) < CHEM_MASS_ACCEPT_KG  # 2 mg; 1000 actions → 2 g


def test_sabatier_efficiency_vents_unused_hydrogen():
    m = _model(initial_captured_co2_kg=10.0, sabatier_conversion_efficiency=0.5)
    r = m.run_ogs(0.06)
    assert r["sabatier_h2_used_kg"] == pytest.approx(0.5 * r["h2_generated_kg"], **EXACT)
    assert r["h2_vented_kg"] == pytest.approx(0.5 * r["h2_generated_kg"], **EXACT)
    assert r["sabatier_h2_used_kg"] + r["h2_vented_kg"] == pytest.approx(
        r["h2_generated_kg"], **EXACT
    )


def test_wrs_preserves_water_volume_across_buffers_product_and_brine():
    m = _model(initial_urine_buffer_l=5.0, initial_grey_water_l=3.0, initial_product_water_l=20.0)
    before = m.state.copy()
    r = m.run_wrs(2.5)
    s = m.state

    feed = r["urine_feed_l"] + r["grey_feed_l"]
    assert feed == pytest.approx(r["recovered_water_l"] + r["brine_loss_l"], **EXACT)
    assert before.urine_buffer_l - s.urine_buffer_l == pytest.approx(r["urine_feed_l"], **EXACT)
    assert before.grey_water_l - s.grey_water_l == pytest.approx(r["grey_feed_l"], **EXACT)
    assert s.product_water_l - before.product_water_l == pytest.approx(r["recovered_water_l"], **EXACT)
    assert s.total_wrs_brine_loss_l - before.total_wrs_brine_loss_l == pytest.approx(
        r["brine_loss_l"], **EXACT
    )


def test_resource_services_move_mass_without_creating_it():
    m = _model(
        initial_cabin_o2_kg=1.0,
        initial_captured_co2_kg=1.0,
        initial_product_water_l=10.0,
        initial_grey_water_l=0.0,
    )
    before = m.state.copy()

    o2 = m.request_o2(0.25)
    co2 = m.request_co2(0.4)
    water = m.request_product_water(1.5)
    grey = m.submit_grey_water(0.75)
    s = m.state

    assert before.cabin_o2_kg - s.cabin_o2_kg == pytest.approx(o2, **EXACT)
    assert s.total_o2_delivered_kg == pytest.approx(o2, **EXACT)
    assert before.captured_co2_kg - s.captured_co2_kg == pytest.approx(co2, **EXACT)
    assert s.total_co2_delivered_kg == pytest.approx(co2, **EXACT)
    assert before.product_water_l - s.product_water_l == pytest.approx(water, **EXACT)
    assert s.total_product_water_delivered_l == pytest.approx(water, **EXACT)
    assert s.grey_water_l - before.grey_water_l == pytest.approx(grey, **EXACT)
    assert s.total_external_grey_water_submitted_l == pytest.approx(grey, **EXACT)


# --------------------------------------------------------------------------- #
# multi-step species conservation (exact inventory ledgers)
# --------------------------------------------------------------------------- #
def test_scenario_o2_conservation_with_services():
    m = _driven_loop(72, with_services=True, initial_cabin_o2_kg=2.0, initial_product_water_l=150.0)
    s = m.state
    c = m.config
    total_in = c.initial_cabin_o2_kg + s.total_o2_generated_kg
    total_out = s.cabin_o2_kg + s.total_o2_consumed_kg + s.total_o2_delivered_kg
    assert total_in == pytest.approx(total_out, **EXACT)


def test_scenario_co2_conservation_with_services():
    m = _driven_loop(72, with_services=True)
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
    assert total_in == pytest.approx(total_out, **EXACT)


def test_scenario_total_water_conservation_including_buffers():
    """All water tanks + sinks + electrolysis feed + deliveries must close."""
    m = _driven_loop(
        72,
        with_services=True,
        initial_product_water_l=150.0,
        initial_urine_buffer_l=1.0,
        initial_grey_water_l=0.5,
    )
    s = m.state
    c = m.config
    total_in = (
        c.initial_product_water_l
        + c.initial_urine_buffer_l
        + c.initial_grey_water_l
        + s.total_external_grey_water_submitted_l
        + s.total_water_regenerated_l
    )
    total_out = (
        s.product_water_l
        + s.urine_buffer_l
        + s.grey_water_l
        + s.total_unrecoverable_crew_water_l
        + s.total_wrs_brine_loss_l
        + s.total_electrolysis_water_kg  # density 1.0 kg/L
        + s.total_product_water_delivered_l
    )
    assert total_in == pytest.approx(total_out, abs=1e-9)


def test_crew_metabolism_does_not_create_or_destroy_water():
    m = _model(initial_product_water_l=50.0)
    before = m.state.copy()
    m.advance_step()
    s = m.state
    drunk = before.product_water_l - s.product_water_l
    produced = (
        (s.urine_buffer_l - before.urine_buffer_l)
        + (s.grey_water_l - before.grey_water_l)
        + (s.total_unrecoverable_crew_water_l - before.total_unrecoverable_crew_water_l)
    )
    assert drunk == pytest.approx(produced, **EXACT)
    assert drunk == pytest.approx(s.total_potable_water_consumed_l - before.total_potable_water_consumed_l, **EXACT)
