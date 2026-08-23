from core.agents.memory import AgentMemory, DiscourseBuffer, TeamMemoryStore
from core.agents.types import AgentMessage, StepAgentOutcome
from environment.protocol import CommandKind, RecoveryCommand


def test_agent_memory_trims_to_limit():
    mem = AgentMemory(agent_id="monitor", limit=3)
    for i in range(5):
        mem.append(f"entry {i}")
    assert mem.recent() == ["entry 2", "entry 3", "entry 4"]


def test_discourse_buffer_trims_to_window():
    buf = DiscourseBuffer(window=2)
    for i in range(3):
        buf.extend(
            [
                AgentMessage(
                    step=i,
                    from_role="monitor",
                    to_role="team",
                    message=f"m{i}",
                    message_type="alert",
                )
            ]
        )
    assert len(buf.recent()) == 2
    assert buf.recent()[0].message == "m1"


def test_team_memory_store_commit_step():
    store = TeamMemoryStore(agent_ids=["operator"], memory_limit=4, discourse_window=4)
    outcome = StepAgentOutcome(
        messages=[
            AgentMessage(
                step=1,
                from_role="operator",
                to_role="team",
                message="Boost fan",
                message_type="recovery_command",
                reasoning="CO2 high",
                metadata={"deliberation_phase": "action", "llm_memory": "watch bypass"},
            )
        ],
        commands=[
            RecoveryCommand(kind=CommandKind.SET_FAN_SPEED, value=1.0, issued_by="operator")
        ],
    )
    store.commit_step(outcome)
    assert len(store.discourse.recent()) == 1
    entries = store.agent_memories["operator"].recent()
    assert any("watch bypass" in e for e in entries)
    assert any("set_fan_speed" in e for e in entries)


# --- F3: memory policy -------------------------------------------------------


def test_baseline_policy_is_a_recency_window_that_discards():
    from core.agents.memory import AgentMemory

    mem = AgentMemory(agent_id="a", limit=3)
    for i in range(6):
        mem.append(f"entry {i} co2")
    assert mem.entries == ["entry 3 co2", "entry 4 co2", "entry 5 co2"]
    assert mem.retrieve("co2") == mem.recent(3)


def test_retrieval_keeps_history_and_surfaces_by_overlap():
    from core.agents.memory import AgentMemory

    mem = AgentMemory(agent_id="a", limit=2, policy="naive_retrieval")
    mem.append("step 1: co2 scrubber saturated")
    mem.append("step 2: water reserve nominal")
    mem.append("step 3: crew asleep")
    mem.append("step 4: oxygen storage falling")
    assert len(mem.entries) == 4, "retrieval cannot rank what was discarded"
    got = mem.retrieve("co2 scrubber needs attention")
    assert "step 1: co2 scrubber saturated" in got
    assert len(got) == 2


def test_retrieval_is_deterministic_and_keeps_chronological_order():
    from core.agents.memory import AgentMemory

    def build():
        mem = AgentMemory(agent_id="a", limit=3, policy="naive_retrieval")
        for text in ["co2 high", "o2 low", "co2 vent opened", "water nominal"]:
            mem.append(text)
        return mem

    first = build().retrieve("co2")
    assert first == build().retrieve("co2")
    assert first == ["co2 high", "co2 vent opened", "water nominal"]


def test_no_match_still_returns_entries_rather_than_nothing():
    from core.agents.memory import AgentMemory

    mem = AgentMemory(agent_id="a", limit=2, policy="naive_retrieval")
    mem.append("co2 high")
    mem.append("o2 low")
    assert mem.retrieve("zzzz") == ["co2 high", "o2 low"]


def test_unknown_memory_policy_is_refused():
    import pytest as _pytest
    from core.agents.memory import TeamMemoryStore

    with _pytest.raises(ValueError, match="memory_policy"):
        TeamMemoryStore(agent_ids=["a"], memory_policy="hypothesis")
