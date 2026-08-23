

# --- F1's levels stay mutually exclusive across a chain ----------------------


def test_writing_lenses_moves_a_subsystem_crew_off_that_level():
    """Otherwise the merged config is one load_team refuses, mid-chain."""
    from core.agents.adapter import apply_adapter

    merged = apply_adapter(
        {"team": {"count": 6, "subsystems": ["air_revitalisation", "water_recovery"]}},
        {"schema_version": 1, "fields": {"archetypes": ["failure_mode"]}},
    )
    assert merged["team"]["subsystems"] == []
    assert merged["team"]["archetypes"] == ["failure_mode"]


def test_a_subsystem_crew_survives_an_unrelated_adapter_write():
    from core.agents.adapter import apply_adapter

    merged = apply_adapter(
        {"team": {"count": 6, "subsystems": ["air_revitalisation"]}},
        {"schema_version": 1, "fields": {"team_count": 8}},
    )
    assert merged["team"]["subsystems"] == ["air_revitalisation"]


def test_describe_current_does_not_call_a_subsystem_crew_homogeneous():
    from core.agents.adapter import describe_current

    text = describe_current(
        {
            "team_count": 6,
            "discourse_window": 22,
            "memory_limit": 30,
            "subsystems": ["air_revitalisation", "air_revitalisation", "water_recovery"],
        }
    )
    assert "homogeneous" not in text
    assert "air_revitalisation x2" in text
    assert "not one of the fields you may write" in text
