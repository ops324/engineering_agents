"""Tests for agent archetypes (thinking-style lenses) — additive, persona.py-confined."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agents.persona import (
    ARCHETYPE_LENSES,
    build_personas,
    load_team,
)
from scenario.runner import run_scenario


def test_homogeneous_fallback_matches_legacy_when_no_archetypes():
    """No archetypes => every agent shares the identical persona (backward compatible)."""
    team = load_team({"team": {"count": 4, "persona": "Shared lens."}})

    assert team.archetypes == ()
    personas = build_personas(team)
    assert set(personas) == {f"engineer_{i}" for i in range(1, 5)}
    assert {p.persona for p in personas.values()} == {"Shared lens."}


def test_archetypes_assigned_one_to_one():
    lenses = ["first_principles", "failure_mode", "improviser", "systems_integrator"]
    team = load_team({"team": {"count": 4, "archetypes": lenses}})

    assert dict(team.archetypes) == {
        "engineer_1": "first_principles",
        "engineer_2": "failure_mode",
        "engineer_3": "improviser",
        "engineer_4": "systems_integrator",
    }
    personas = build_personas(team)
    # Each persona carries its lens text plus the shared persona; personas differ.
    for agent_id, lens in team.archetypes:
        assert ARCHETYPE_LENSES[lens] in personas[agent_id].persona
    assert len({p.persona for p in personas.values()}) == 4


def test_archetypes_round_robin_when_fewer_lenses_than_agents():
    team = load_team(
        {"team": {"count": 5, "archetypes": ["first_principles", "failure_mode"]}}
    )
    assert dict(team.archetypes) == {
        "engineer_1": "first_principles",
        "engineer_2": "failure_mode",
        "engineer_3": "first_principles",
        "engineer_4": "failure_mode",
        "engineer_5": "first_principles",
    }


def test_unknown_lens_name_raises():
    with pytest.raises(ValueError, match="Unknown archetype lens"):
        load_team({"team": {"count": 2, "archetypes": ["first_principles", "bogus"]}})


def test_empty_archetypes_list_falls_back_to_homogeneous():
    team = load_team({"team": {"count": 3, "archetypes": []}})
    assert team.archetypes == ()
    assert len({p.persona for p in build_personas(team).values()}) == 1


def _read_summary(run_dir: Path) -> dict:
    return json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))


def test_labeled_run_logs_archetype_composition(tmp_path: Path):
    """Default scrubber config ships archetypes ON; composition is logged even in labeled mode."""
    run_dir = run_scenario(
        "scrubber_degradation",
        output_dir=tmp_path / "labeled",
        overrides={"agents": {"mode": "labeled_rule_base"}},
        recreate_output=True,
    )
    summary = _read_summary(run_dir)
    assert summary["archetypes"] == {
        "engineer_1": "first_principles",
        "engineer_2": "failure_mode",
        "engineer_3": "improviser",
        "engineer_4": "systems_integrator",
    }
    # Labeled mode stays policy-driven: existing invariants unchanged.
    assert summary["agent_ids"] == ["engineer_1", "engineer_2", "engineer_3", "engineer_4"]


def test_archetypes_can_be_disabled_via_override(tmp_path: Path):
    """Removing archetypes reproduces the homogeneous baseline (empty composition map)."""
    run_dir = run_scenario(
        "scrubber_degradation",
        output_dir=tmp_path / "homogeneous",
        overrides={"agents": {"mode": "labeled_rule_base", "team": {"archetypes": []}}},
        recreate_output=True,
    )
    assert _read_summary(run_dir)["archetypes"] == {}


# --- F1 level three: operational subsystem specialisms -----------------------


def test_subsystems_are_dealt_round_robin_and_reach_the_persona():
    team = load_team(
        {
            "team": {
                "count": 6,
                "id_prefix": "eclss_operator",
                "subsystems": [
                    "air_revitalisation",
                    "oxygen_generation",
                    "water_recovery",
                    "fault_detection",
                    "systems_integration",
                    "thermal_humidity",
                ],
            }
        }
    )
    assert [name for _, name in team.subsystems] == [
        "air_revitalisation",
        "oxygen_generation",
        "water_recovery",
        "fault_detection",
        "systems_integration",
        "thermal_humidity",
    ]
    personas = build_personas(team)
    assert len({p.persona for p in personas.values()}) == 6
    assert "Water recovery" in personas["eclss_operator_3"].persona


def test_subsystem_level_does_not_borrow_the_post_run_review_wording():
    """The design lenses read 'the simulation is over'; an operator's must not."""
    team = load_team({"team": {"count": 2, "subsystems": ["air_revitalisation"]}})
    for persona in build_personas(team).values():
        assert "simulation is over" not in persona.persona
        assert "while the run is going" in persona.persona


def test_unknown_subsystem_name_raises():
    with pytest.raises(ValueError, match="Unknown operational subsystem"):
        load_team({"team": {"count": 2, "subsystems": ["water_recovery", "bogus"]}})


def test_setting_both_f1_levels_is_refused():
    """They are levels of one factor, so a config naming both is at no design point."""
    with pytest.raises(ValueError, match="levels of the same factor"):
        load_team(
            {
                "team": {
                    "count": 2,
                    "archetypes": ["first_principles"],
                    "subsystems": ["water_recovery"],
                }
            }
        )


def test_empty_subsystems_list_falls_back_to_homogeneous():
    team = load_team({"team": {"count": 3, "subsystems": []}})
    assert team.subsystems == ()
    assert len({p.persona for p in build_personas(team).values()}) == 1
