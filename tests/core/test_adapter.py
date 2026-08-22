"""Tests for the adapter layer — design.md §4.

The claim this layer makes is structural: a proposal that loosens a gate
cannot be expressed. A claim like that is worth only as much as the test that
tries to break it, so most of what follows is attempts to write somewhere the
adapter is not supposed to reach.
"""

from __future__ import annotations

import json

import pytest

from core.agents.adapter import (
    ADAPTER_FIELDS,
    ADAPTER_SCHEMA_VERSION,
    BASELINE_ADAPTER,
    adapter_provenance,
    apply_adapter,
    load_adapter,
    validate_adapter,
    write_adapter,
)

AGENTS_CONFIG = {
    "mode": "llm",
    "memory_limit": 30,
    "discourse_window": 22,
    "team": {"count": 10, "id_prefix": "eclss_operator", "persona": "..."},
    "policy": {"request_co2_before_ogs": False, "ogs_goal": {"input_water_mass": 0.15}},
    "llm": {"provider": "vllm", "model": "qwen3-8b", "temperature": 0.45},
}


def adapter(**fields):
    return {"schema_version": ADAPTER_SCHEMA_VERSION, "fields": fields}


# --- the frozen boundary --------------------------------------------------

@pytest.mark.parametrize(
    "field",
    [
        "thresholds.co2_storage_critical_kg",   # the alarm line itself
        "co2_storage_critical_kg",
        "policy.ogs_goal.input_water_mass",     # P1 operating policy
        "policy",
        "llm.model",                            # F6 is a registered factor, not an adapter
        "mode",                                 # rule base vs llm
        "evaluator",
        "command_admissibility",
    ],
)
def test_the_adapter_cannot_name_anything_outside_its_surface(field):
    errors = validate_adapter(adapter(**{field: 1}))
    assert errors, f"{field} was accepted; the frozen boundary is advisory, not structural"
    assert "not an adapter field" in errors[0]


def test_rejection_leaves_the_config_untouched():
    with pytest.raises(ValueError):
        apply_adapter(AGENTS_CONFIG, adapter(**{"thresholds.co2_storage_critical_kg": 99}))
    assert AGENTS_CONFIG["policy"]["ogs_goal"]["input_water_mass"] == 0.15


def test_a_partly_valid_update_is_not_partly_applied():
    """One writable field and one that is not. Applying the writable half would
    produce a configuration nobody proposed."""
    with pytest.raises(ValueError):
        apply_adapter(AGENTS_CONFIG, adapter(team_count=4, **{"llm.model": "something-else"}))


def test_dotted_and_dunder_paths_do_not_traverse():
    for probe in ("team.count", "__class__", "team.count.__class__", "../thresholds"):
        assert validate_adapter(adapter(**{probe: 3})), f"{probe} was accepted"


# --- unimplemented surfaces are refused, not stored -----------------------

@pytest.mark.parametrize("field", ["R.similarity", "X.order", "S.give_up_after"])
def test_surfaces_without_an_implementation_are_rejected_by_name(field):
    errors = validate_adapter(adapter(**{field: 1}))
    assert errors and "no implementation" in errors[0]


def test_the_schema_holds_only_fields_that_move_something():
    """Every field must land somewhere the engine reads. The engine reads
    team.count, team.archetypes, discourse_window and memory_limit."""
    engine_reads = {("team", "count"), ("team", "archetypes"), ("discourse_window",), ("memory_limit",)}
    assert {spec.path for spec in ADAPTER_FIELDS.values()} == engine_reads


# --- values ---------------------------------------------------------------

def test_writable_fields_land_where_the_engine_looks():
    out = apply_adapter(
        AGENTS_CONFIG,
        adapter(team_count=6, archetypes=["first_principles", "failure_mode"],
                discourse_window=8, memory_limit=12),
    )
    assert out["team"]["count"] == 6
    assert out["team"]["archetypes"] == ["first_principles", "failure_mode"]
    assert out["discourse_window"] == 8
    assert out["memory_limit"] == 12


def test_an_empty_adapter_changes_nothing():
    """F7=absent, stated as values. The run is the run that would have happened."""
    assert apply_adapter(AGENTS_CONFIG, adapter()) == AGENTS_CONFIG
    assert validate_adapter({"fields": BASELINE_ADAPTER}) == []


def test_applying_does_not_mutate_the_caller_s_config():
    apply_adapter(AGENTS_CONFIG, adapter(team_count=3))
    assert AGENTS_CONFIG["team"]["count"] == 10


def test_unknown_lenses_are_rejected_with_the_known_ones_named():
    errors = validate_adapter(adapter(archetypes=["first_principles", "vibes"]))
    assert errors and "unknown lens" in errors[0] and "first_principles" in errors[0]


def test_bounds_reject_rather_than_crash_mid_batch():
    assert validate_adapter(adapter(team_count=0))
    assert validate_adapter(adapter(team_count=21))
    assert validate_adapter(adapter(memory_limit=-1))
    assert validate_adapter(adapter(team_count=True))      # bool is not an int here
    assert validate_adapter(adapter(team_count="10"))


def test_a_stored_adapter_from_another_schema_version_is_not_reinterpreted():
    errors = validate_adapter({"schema_version": 99, "fields": {"team_count": 4}})
    assert errors and "schema_version" in errors[0]


# --- provenance and round trip -------------------------------------------

def test_provenance_says_which_surfaces_were_touched():
    p = adapter_provenance(adapter(team_count=6, memory_limit=12))
    assert p["surfaces_touched"] == ["C", "M"]
    assert p["self_modification"] is True
    assert adapter_provenance(adapter())["self_modification"] is False


def test_round_trip_through_a_file(tmp_path):
    path = tmp_path / "adapter.json"
    write_adapter(path, adapter(team_count=6))
    assert load_adapter(path)["fields"] == {"team_count": 6}


def test_an_invalid_adapter_is_never_written(tmp_path):
    path = tmp_path / "adapter.json"
    with pytest.raises(ValueError):
        write_adapter(path, adapter(**{"thresholds.co2_storage_critical_kg": 99}))
    assert not path.exists()


def test_a_file_that_reaches_outside_the_surface_fails_to_load(tmp_path):
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps({"schema_version": 1, "fields": {"policy": {}}}))
    with pytest.raises(ValueError):
        load_adapter(path)


# --- the Meta agent's side ------------------------------------------------

from core.agents.adapter import (  # noqa: E402
    META_ADAPTER_PERSONA,
    meta_adapter_contract,
    partition_proposal,
    proposal_provenance,
)


def test_the_contract_is_generated_from_the_schema():
    """A hand-written contract drifts from the fields it describes. This one
    cannot: every writable field appears, and nothing else does."""
    contract = meta_adapter_contract()
    for name in ADAPTER_FIELDS:
        assert f'"{name}"' in contract
    assert "thresholds" in contract and "outside this surface" in contract


def test_the_persona_does_not_invite_plant_changes():
    assert "do not propose changes to it" in META_ADAPTER_PERSONA
    assert "legitimate answer" in META_ADAPTER_PERSONA   # proposing nothing is allowed


def test_a_proposal_keeps_the_writable_part_and_records_the_rest():
    accepted, rejected = partition_proposal(
        {"team_count": 6, "thresholds.co2_storage_critical_kg": 99, "R.similarity": "cosine"}
    )
    assert accepted == {"team_count": 6}
    assert [r["field"] for r in rejected] == [
        "thresholds.co2_storage_critical_kg", "R.similarity",
    ]
    assert "not an adapter field" in rejected[0]["reason"]
    assert "no implementation" in rejected[1]["reason"]


def test_an_out_of_range_value_is_rejected_not_clamped():
    accepted, rejected = partition_proposal({"team_count": 400})
    assert accepted == {} and rejected and "above the maximum" in rejected[0]["reason"]


def test_a_non_object_fields_value_is_a_rejection_not_a_crash():
    accepted, rejected = partition_proposal("team_count=6")
    assert accepted == {} and rejected[0]["field"] == "(fields)"


def test_attempts_on_the_frozen_surface_are_counted():
    """The design claims a gate-loosening proposal cannot be expressed. The
    count is what turns that claim into something measurable."""
    accepted, rejected = partition_proposal({"team_count": 6, "policy": {}, "llm.model": "x"})
    p = proposal_provenance({
        "proposed_by": "meta_agent_1", "decision_source": "llm",
        "adapter": {"fields": accepted}, "rejected": rejected,
    })
    assert p["frozen_surface_attempts"] == 2
    assert p["accepted_fields"] == ["team_count"]
    assert p["rejected_fields"] == ["policy", "llm.model"]
    assert p["proposes_change"] is True


def test_proposing_nothing_is_recorded_as_a_result():
    p = proposal_provenance({
        "proposed_by": "meta_agent_1", "decision_source": "llm",
        "adapter": {"fields": {}}, "rejected": [],
    })
    assert p["proposes_change"] is False and p["frozen_surface_attempts"] == 0


def test_an_accepted_proposal_is_directly_applicable():
    """What the Meta agent proposes must be what the next run can apply, with
    no second translation step to disagree with the first."""
    accepted, _ = partition_proposal({"team_count": 6, "policy": {}})
    out = apply_adapter(AGENTS_CONFIG, {"schema_version": ADAPTER_SCHEMA_VERSION, "fields": accepted})
    assert out["team"]["count"] == 6


# --- what the Meta agent can see ------------------------------------------

from core.agents.adapter import describe_current  # noqa: E402


def test_the_agent_is_shown_the_values_it_is_being_asked_to_change():
    """The pilot ran four generations without this and oscillated 150/50/150/50
    on a field it could not observe, justifying each direction fluently."""
    text = describe_current(
        {"team_count": 10, "discourse_window": 22, "memory_limit": 30, "archetypes": []}
    )
    assert "discourse_window=22" in text
    assert "memory_limit=30" in text
    assert "team_count=10" in text


def test_the_lens_composition_is_shown_as_counts_not_as_a_list():
    """Ten agents holding four lenses is 3/3/2/2, and the proportion is the
    thing being revised — a bare list of names does not show it."""
    text = describe_current({
        "team_count": 10, "discourse_window": 22, "memory_limit": 30,
        "archetypes": ["failure_mode"] * 7 + ["improviser"] * 3,
    })
    assert "failure_mode x7" in text and "improviser x3" in text


def test_a_homogeneous_crew_says_so_rather_than_showing_an_empty_list():
    text = describe_current(
        {"team_count": 10, "discourse_window": 22, "memory_limit": 30, "archetypes": []}
    )
    assert "homogeneous" in text


def test_the_contract_explains_that_repeats_set_the_proportion():
    """The lever existed from the start and was never described, so three
    generations of proposals never used it."""
    contract = meta_adapter_contract()
    assert "Repeating a name weights the allocation" in contract
    assert "seven and three" in contract
