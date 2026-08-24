"""Time-accounting invariants on the plant configuration."""

from __future__ import annotations

import pytest

from environment.ssos.eclss.plant_sim.config import PlantConfigError, PlantSimConfig

def test_an_action_cannot_process_more_time_than_the_step_advances():
    """ARS ran a 4800 s quantum against a 1200 s step: one command bought four
    steps of scrubbing while the world advanced one. That is not a fast
    machine, it is unaccounted time -- and it inflated ARS from 1.08x the
    crew's CO2 output to 4.3x."""
    with pytest.raises(PlantConfigError, match="must be <= step_seconds"):
        PlantSimConfig(step_seconds=1200.0, ars_operation_seconds=4800.0)


def test_a_quantum_shorter_than_the_step_is_allowed():
    """The machine simply ran for part of the interval."""
    cfg = PlantSimConfig(step_seconds=1200.0, ars_operation_seconds=600.0)
    assert cfg.ars_operation_seconds == 600.0


def test_ars_default_matches_the_step_so_one_command_buys_one_step():
    cfg = PlantSimConfig()
    assert cfg.ars_operation_seconds == cfg.step_seconds
    assert cfg.ogs_operation_seconds == cfg.step_seconds
    assert cfg.wrs_operation_seconds == cfg.step_seconds


def test_the_restored_ars_margin_only_just_outpaces_the_crew():
    """Why the number matters: at the corrected quantum ARS removes 0.0625 kg
    a step against 0.0578 kg generated -- a 1.08x margin, the tight design
    point. At 4800 s it was 4.3x and no threshold could be distinguished."""
    from environment.ssos.eclss.plant_sim.model import per_interval

    cfg = PlantSimConfig()
    generated = per_interval(cfg.co2_kg_day_person, cfg.step_seconds) * cfg.crew_size
    removed = per_interval(cfg.ars_capacity_kg_day, cfg.ars_operation_seconds)
    assert 1.0 < removed / generated < 1.2
