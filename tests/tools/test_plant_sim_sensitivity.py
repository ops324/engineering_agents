"""Tests for plant_sim sensitivity (N-sweep + YAML knobs, non-UI core)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

import matplotlib.pyplot as plt
import pytest

from environment.ssos.eclss.plant_sim.config import PlantSimConfig
from environment.ssos.eclss.plant_sim.model import PlantModel
from environment.ssos.eclss.units import water_kg_to_l
from tools.plant_sim_sensitivity import (
    SLIDER_SPECS,
    _policy_action_goals,
    apply_sensitivity_overrides,
    ars_nameplate_kg,
    close_crew_water,
    load_dynamics,
    load_ssos_yaml,
    make_sweep_figure,
    metabolism_demand_per_step,
    ogs_nameplate,
    run_sensitivity,
    sensitivity_figure,
    sweep,
    tank_effect,
    wrs_nameplate_l,
    yaml_defaults,
)


def test_yaml_defaults_match_scenario_file():
    scenario, _agents = load_ssos_yaml()
    defaults = yaml_defaults()
    assert defaults["simulation.initial_o2_storage_kg"] == pytest.approx(
        float(scenario["simulation"]["initial_o2_storage_kg"])
    )
    assert defaults["plant_sim.crew.o2_kg_day_person"] == pytest.approx(
        float(scenario["plant_sim"]["crew"]["o2_kg_day_person"])
    )
    assert int(defaults["plant_sim.crew.size"]) == int(scenario["plant_sim"]["crew"]["size"])


def test_slider_specs_contain_yaml_defaults():
    defaults = yaml_defaults()
    for spec in SLIDER_SPECS:
        value = defaults[spec.key]
        assert spec.minimum <= value <= spec.maximum, spec.key


def test_overrides_do_not_mutate_source_yaml_dict():
    scenario, _agents = load_ssos_yaml()
    original = float(scenario["plant_sim"]["crew"]["o2_kg_day_person"])
    patched = apply_sensitivity_overrides(scenario, {"plant_sim.crew.o2_kg_day_person": original * 2})
    assert scenario["plant_sim"]["crew"]["o2_kg_day_person"] == pytest.approx(original)
    assert patched["plant_sim"]["crew"]["o2_kg_day_person"] == pytest.approx(original * 2)
    assert patched["plant_sim"]["survival"]["enabled"] is False


def test_crew_water_closes_to_potable():
    crew = {
        "potable_water_kg_day_person": 4.0,
        "urine_kg_day_person": 1.50,
        "condensate_kg_day_person": 0.75,
        "unrecoverable_water_kg_day_person": 0.03,
    }
    close_crew_water(crew)
    assert crew["urine_kg_day_person"] + crew["condensate_kg_day_person"] + crew[
        "unrecoverable_water_kg_day_person"
    ] == pytest.approx(4.0)


def test_o2_rate_override_scales_metabolism_column():
    base_rows, _ = run_sensitivity({}, n_max=2, steps=4)
    hot_rows, patched = run_sensitivity(
        {"plant_sim.crew.o2_kg_day_person": 1.68},
        n_max=2,
        steps=4,
    )
    PlantSimConfig.from_scenario_config(patched)
    base = next(row for row in base_rows if row.n == 2 and row.mode == "none").per_step()
    hot = next(row for row in hot_rows if row.n == 2 and row.mode == "none").per_step()
    assert hot.o2_demand_kg == pytest.approx(2 * base.o2_demand_kg, rel=1e-6)
    assert tank_effect(hot, "o2", "metabolism") == pytest.approx(
        2 * tank_effect(base, "o2", "metabolism")
    )


def test_ars_capacity_override_scales_nameplate():
    base_rows, _ = run_sensitivity({}, n_max=1, steps=4)
    fat_rows, _ = run_sensitivity({"plant_sim.ars.capacity_kg_day": 9.0}, n_max=1, steps=4)
    base = next(row for row in base_rows if row.mode == "ars")
    fat = next(row for row in fat_rows if row.mode == "ars")
    assert fat.ars_nameplate_kg == pytest.approx(2 * base.ars_nameplate_kg, rel=1e-6)


def test_larger_initial_o2_reduces_tank_starvation():
    starved, _ = run_sensitivity(
        {"simulation.initial_o2_storage_kg": 0.05},
        n_max=4,
        steps=8,
    )
    rich, _ = run_sensitivity(
        {"simulation.initial_o2_storage_kg": 5.0},
        n_max=4,
        steps=8,
    )
    starved_row = next(row for row in starved if row.n == 4 and row.mode == "none").per_step()
    rich_row = next(row for row in rich if row.n == 4 and row.mode == "none").per_step()
    assert starved_row.o2_metabolism_kg < rich_row.o2_demand_kg - 1e-6
    assert rich_row.o2_metabolism_kg == pytest.approx(rich_row.o2_demand_kg, rel=1e-5)


def test_sensitivity_figure_is_3x4():
    rows, _ = run_sensitivity({}, n_max=2, steps=4)
    fig = sensitivity_figure(rows, baseline_rows=rows, yaml_n=4)
    assert fig.axes
    assert len(fig.get_axes()) >= 12
    plt.close(fig)


def test_metabolism_demand_scales_with_n():
    plant = PlantSimConfig()
    o2_1, co2_1, water_1 = metabolism_demand_per_step(1, plant)
    o2_4, co2_4, water_4 = metabolism_demand_per_step(4, plant)
    assert o2_4 == pytest.approx(4 * o2_1)
    assert co2_4 == pytest.approx(4 * co2_1)
    assert water_4 == pytest.approx(4 * water_1)


def test_nameplates_are_positive_machine_ratings():
    plant = PlantSimConfig()
    o2, water = ogs_nameplate(0.15, plant)
    assert ars_nameplate_kg(1.8, plant) > 0.0
    assert o2 > 0.0
    assert water > 0.0
    assert wrs_nameplate_l(2.0, plant) > 0.0
    # Used to assert 3.6 buys twice 1.8. Under the rated-capacity invariant the
    # nameplate is a property of the machine, so a bigger goal reads the same.
    assert ars_nameplate_kg(3.6, plant) == pytest.approx(ars_nameplate_kg(1.8, plant))


def test_sweep_demand_scales_and_ops_flat_vs_n():
    rows = sweep(n_max=3, steps=5)
    none_rows = {row.n: row for row in rows if row.mode == "none"}
    ars_rows = {row.n: row for row in rows if row.mode == "ars"}
    ogs_rows = {row.n: row for row in rows if row.mode == "ogs"}
    wrs_rows = {row.n: row for row in rows if row.mode == "wrs"}

    one = none_rows[1].per_step()
    three = none_rows[3].per_step()
    assert three.o2_demand_kg == pytest.approx(3 * one.o2_demand_kg, rel=1e-6)
    assert three.co2_demand_kg == pytest.approx(3 * one.co2_demand_kg, rel=1e-6)
    assert three.water_demand_l == pytest.approx(3 * one.water_demand_l, rel=1e-6)
    assert three.co2_ops_kg == 0.0
    assert three.o2_ops_kg == 0.0

    assert tank_effect(three, "o2", "metabolism") == pytest.approx(
        3 * tank_effect(one, "o2", "metabolism")
    )
    assert ars_rows[3].ars_nameplate_kg == pytest.approx(ars_rows[1].ars_nameplate_kg)
    assert ogs_rows[3].ogs_nameplate_o2_kg == pytest.approx(ogs_rows[1].ogs_nameplate_o2_kg)
    assert wrs_rows[3].wrs_nameplate_l == pytest.approx(wrs_rows[1].wrs_nameplate_l)
    assert tank_effect(ars_rows[3].per_step(), "co2", "ops") == pytest.approx(
        tank_effect(ars_rows[1].per_step(), "co2", "ops")
    )
    assert tank_effect(ogs_rows[3].per_step(), "o2", "ops") == pytest.approx(
        tank_effect(ogs_rows[1].per_step(), "o2", "ops")
    )
    assert tank_effect(wrs_rows[3].per_step(), "water", "ops") == pytest.approx(
        tank_effect(wrs_rows[1].per_step(), "water", "ops")
    )


def test_sweep_ars_reduces_cabin_co2_vs_none():
    rows = sweep(n_max=2, steps=8)
    none_n2 = next(row for row in rows if row.n == 2 and row.mode == "none")
    ars_n2 = next(row for row in rows if row.n == 2 and row.mode == "ars")
    assert ars_n2.co2_ops_kg > 0.0
    assert ars_n2.co2_net_kg < none_n2.co2_net_kg


def test_sweep_ogs_adds_o2_and_uses_water():
    rows = sweep(n_max=1, steps=8)
    none_row = next(row for row in rows if row.mode == "none")
    ogs_row = next(row for row in rows if row.mode == "ogs")
    assert ogs_row.o2_ops_kg > 0.0
    assert ogs_row.o2_net_kg > none_row.o2_net_kg
    assert ogs_row.water_ops_l > 0.0


def test_tank_effect_uses_inventory_sign():
    rows = sweep(n_max=1, steps=8)
    none_row = next(row for row in rows if row.mode == "none").per_step()
    ars_row = next(row for row in rows if row.mode == "ars").per_step()
    ogs_row = next(row for row in rows if row.mode == "ogs").per_step()
    wrs_row = next(row for row in rows if row.mode == "wrs").per_step()

    assert tank_effect(none_row, "co2", "metabolism") > 0.0
    assert tank_effect(none_row, "o2", "metabolism") < 0.0
    assert tank_effect(none_row, "water", "metabolism") < 0.0
    assert tank_effect(none_row, "co2", "ops") == pytest.approx(0.0, abs=1e-12)

    assert tank_effect(ars_row, "co2", "ops") < 0.0
    assert tank_effect(ogs_row, "o2", "ops") > 0.0
    assert tank_effect(ogs_row, "water", "ops") < 0.0
    assert tank_effect(wrs_row, "water", "ops") > 0.0
    assert tank_effect(none_row, "o2", "level") == pytest.approx(none_row.final_o2_kg)
    assert tank_effect(none_row, "water", "level") == pytest.approx(none_row.final_water_l)


def test_ending_tank_equals_initial_plus_campaign_delta():
    rows = sweep(n_max=2, steps=8)
    row = next(item for item in rows if item.n == 2 and item.mode == "none")
    assert row.final_co2_kg == pytest.approx(row.initial_co2_kg + row.co2_net_kg)
    assert row.final_o2_kg == pytest.approx(row.initial_o2_kg + row.o2_net_kg)
    assert row.final_water_l == pytest.approx(row.initial_water_l + row.water_net_l)
    stepped = row.per_step()
    assert tank_effect(stepped, "co2", "level") == pytest.approx(row.final_co2_kg)
    assert tank_effect(stepped, "o2", "level") == pytest.approx(row.initial_o2_kg + row.o2_net_kg)


def test_sweep_figure_row_shares_ylim_and_shows_y_ticks():
    rows = sweep(n_max=3, steps=5)
    fig = make_sweep_figure(rows)
    fig.canvas.draw()
    by_row: dict[float, list] = defaultdict(list)
    for ax in fig.axes:
        by_row[round(ax.get_position().y0, 3)].append(ax)
    assert len(by_row) == 3
    for group in by_row.values():
        group_sorted = sorted(group, key=lambda ax: ax.get_position().x0)
        assert len(group_sorted) == 4
        rate_ylims = [tuple(ax.get_ylim()) for ax in group_sorted[:3]]
        assert rate_ylims[0] == rate_ylims[1] == rate_ylims[2]
        for ax in group_sorted:
            labels = [t.get_text() for t in ax.get_yticklabels() if t.get_text()]
            assert labels
    plt.close(fig)


def test_yaml_matches_plant_sim_dynamics():
    scenario, agents, plant, policy = load_dynamics()
    crew = scenario["plant_sim"]["crew"]
    sim = scenario["simulation"]
    time = scenario["plant_sim"]["time"]
    assert plant.o2_kg_day_person == pytest.approx(float(crew["o2_kg_day_person"]))
    assert plant.co2_kg_day_person == pytest.approx(float(crew["co2_kg_day_person"]))
    assert plant.potable_water_kg_day_person == pytest.approx(
        float(crew["potable_water_kg_day_person"])
    )
    assert plant.step_seconds == pytest.approx(float(time["step_seconds"]))
    assert plant.ars_capacity_kg_day == pytest.approx(
        float(scenario["plant_sim"]["ars"]["capacity_kg_day"])
    )
    assert plant.initial_o2_kg == pytest.approx(float(sim["initial_o2_storage_kg"]))
    assert plant.initial_cabin_co2_kg == pytest.approx(float(sim["initial_co2_storage_kg"]))
    assert plant.initial_product_water_l == pytest.approx(float(sim["initial_product_water_l"]))
    ars_goal, ogs_water, wrs_urine = _policy_action_goals(policy, plant)
    yaml_policy = (agents.get("actor") or {}).get("policy") or agents.get("policy") or {}
    assert ars_goal == pytest.approx(float(yaml_policy["ars_goal"]["initial_co2_mass"]))
    assert ogs_water == pytest.approx(float(yaml_policy["ogs_goal"]["input_water_mass"]))
    assert wrs_urine == pytest.approx(float(yaml_policy["wrs_goal"]["urine_volume"]))


def test_demand_and_nameplate_match_plant_model_probes():
    scenario, agents, plant, policy = load_dynamics()
    ars_goal, ogs_water, wrs_urine = _policy_action_goals(policy, plant)

    o2, co2, water = metabolism_demand_per_step(4, plant)
    model = PlantModel(
        replace(plant, crew_size=4, survival_enabled=False, initial_o2_kg=1.0e6, initial_product_water_l=1.0e6)
    )
    metab = model.advance_step()
    assert o2 == pytest.approx(metab["o2_demand_kg"])
    assert co2 == pytest.approx(metab["co2_generated_kg"])
    assert water == pytest.approx(metab["water_demand_kg"])

    dt = plant.step_seconds / 86400.0
    assert o2 == pytest.approx(4 * plant.activity_factor * plant.o2_kg_day_person * dt)
    # Rated against the step, and without the goal scale: both used to be in here,
    # and between them they were the whole of the over-capacity defect.
    assert ars_nameplate_kg(ars_goal, plant) == pytest.approx(
        plant.ars_capacity_kg_day * plant.step_seconds / 86400.0
    )
    ogs_o2, ogs_h2o = ogs_nameplate(ogs_water, plant)
    unconstrained = PlantModel(
        replace(plant, initial_product_water_l=1.0e6, survival_enabled=False)
    ).run_ogs(ogs_water)
    assert ogs_o2 == pytest.approx(unconstrained["o2_generated_kg"])
    assert ogs_h2o == pytest.approx(water_kg_to_l(unconstrained["processed_water_kg"]))
    # wrs_max_feed_l_per_operation used to be the only ceiling WRS had. It is still
    # a ceiling, but wrs_capacity_l_day is far below it at this step length, so the
    # rating is what the nameplate now reads.
    wrs_rated_l = plant.wrs_capacity_l_day * plant.step_seconds / 86400.0
    assert wrs_rated_l < plant.wrs_max_feed_l_per_operation
    assert wrs_nameplate_l(wrs_urine, plant) == pytest.approx(
        min(wrs_urine, wrs_rated_l) * plant.wrs_urine_recovery
    )
