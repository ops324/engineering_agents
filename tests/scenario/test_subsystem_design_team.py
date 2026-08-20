"""Post-run design proposals come from subsystem proposers, not operators."""

from __future__ import annotations

import json
import threading
import time

import pytest

from core.agents.persona import (
    DESIGN_SUBSYSTEM_LENSES,
    build_design_personas,
    load_design_team,
)
from core.llm.base import LLMClient
from scenario.agents.ssos_eclss_loop_team import SsosEclssLoopTeam

_SUBSYSTEMS = [
    "air_revitalisation",
    "oxygen_generation",
    "water_recovery",
    "fault_detection",
    "systems_integration",
]


def _summary(steps: int = 4) -> dict:
    return {
        "steps": steps,
        "peak_co2_storage_kg": 2.1,
        "final_o2_storage_kg": 0.4,
        "thresholds": {"co2_storage_high_kg": 1.5, "o2_storage_low_kg": 0.45},
    }


def _config(with_design_team: bool, mode: str = "llm") -> dict:
    config = {
        "mode": mode,
        "team": {"count": 4, "id_prefix": "eclss_operator"},
        "policy": {},
        "llm": {"provider": "ollama"},
    }
    if with_design_team:
        config["design_team"] = {"id_prefix": "design", "subsystems": list(_SUBSYSTEMS)}
    return config


class _FakeClient(LLMClient):
    """Returns one action_profile change, tagged with the caller's subsystem."""

    def __init__(self, max_concurrency: int = 8) -> None:
        super().__init__(max_concurrency=max_concurrency)
        self.prompts: list[str] = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def generate(self, prompt: str) -> str:
        with self.lock:
            self.prompts.append(prompt)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(0.03)
        with self.lock:
            self.in_flight -= 1
        return json.dumps(
            {
                "message": "post-run assessment",
                "reasoning": "run evidence",
                "changes": [
                    {
                        "change_kind": "action_profile",
                        "payload": {"subsystem": "ars", "fields": {"initial_co2_mass": 2.0}},
                    }
                ],
            }
        )

    def check_connection(self) -> bool:
        return True


def _team(config: dict, client: LLMClient) -> SsosEclssLoopTeam:
    team = SsosEclssLoopTeam(config)
    team.llm_client = client
    for agent in team.agents.values():
        agent.llm_client = client
    for agent in team.design_agents.values():
        agent.llm_client = client
    return team


# --- config loading -------------------------------------------------------


def test_load_design_team_absent_returns_none():
    assert load_design_team({}) is None
    assert load_design_team({"design_team": {}}) is None
    assert load_design_team({"design_team": {"subsystems": []}}) is None


def test_load_design_team_builds_one_agent_per_subsystem():
    cfg = load_design_team({"design_team": {"subsystems": _SUBSYSTEMS}})
    assert cfg is not None
    assert cfg.count == len(_SUBSYSTEMS)
    assert cfg.agent_ids == tuple(f"design_{name}" for name in _SUBSYSTEMS)
    assert dict(cfg.subsystems)["design_water_recovery"] == "water_recovery"


def test_load_design_team_rejects_unknown_subsystem():
    with pytest.raises(ValueError, match="Unknown design subsystem"):
        load_design_team({"design_team": {"subsystems": ["warp_core"]}})


def test_design_personas_carry_their_subsystem_lens():
    cfg = load_design_team({"design_team": {"subsystems": _SUBSYSTEMS}})
    personas = build_design_personas(cfg)
    for agent_id, subsystem in cfg.subsystems:
        assert DESIGN_SUBSYSTEM_LENSES[subsystem] in personas[agent_id].persona


# --- separation from operators -------------------------------------------


def test_design_agents_are_disjoint_from_operators():
    team = SsosEclssLoopTeam(_config(with_design_team=True))
    assert set(team.design_agents).isdisjoint(team.agents)
    assert all(aid.startswith("design_") for aid in team.design_agents)


def test_proposals_are_attributed_to_subsystem_agents_not_operators():
    client = _FakeClient()
    team = _team(_config(with_design_team=True), client)

    proposals = team.propose_post_run_design(_summary())

    assert proposals["proposer_kind"] == "subsystem_design_team"
    proposers = {c["proposed_by"] for c in proposals["changes"]}
    assert proposers == set(team.design_team_cfg.agent_ids)
    # No operator may appear as a proposer once a design team is configured.
    assert proposers.isdisjoint(team.team_cfg.agent_ids)


def test_every_subsystem_contributes_and_is_labelled():
    client = _FakeClient()
    team = _team(_config(with_design_team=True), client)

    proposals = team.propose_post_run_design(_summary())

    contributed = {c["subsystem"] for c in proposals["contributions"]}
    assert contributed == set(_SUBSYSTEMS)


def test_subsystem_proposers_run_simultaneously():
    client = _FakeClient()
    team = _team(_config(with_design_team=True), client)

    team.propose_post_run_design(_summary())

    assert client.max_in_flight > 1, "design proposers should not be serialised"


def test_each_proposer_is_told_only_its_own_subsystem():
    client = _FakeClient()
    team = _team(_config(with_design_team=True), client)

    team.propose_post_run_design(_summary())

    for subsystem in _SUBSYSTEMS:
        matching = [p for p in client.prompts if f"you answer for {subsystem}" in p]
        assert len(matching) == 1, f"expected exactly one prompt for {subsystem}"


# --- backward compatibility ----------------------------------------------


def test_without_design_team_an_operator_still_proposes():
    client = _FakeClient()
    team = _team(_config(with_design_team=False), client)

    proposals = team.propose_post_run_design(_summary())

    assert team.design_agents == {}
    assert "proposer_kind" not in proposals
    assert proposals["proposed_by"] in team.team_cfg.agent_ids


def test_labeled_rule_base_is_unaffected_by_design_team():
    config = _config(with_design_team=True, mode="labeled_rule_base")
    team = SsosEclssLoopTeam(config)

    proposals = team.propose_post_run_design(_summary())

    # Rule mode never calls an LLM, so the proposal stays operator-attributed.
    assert proposals["decision_source"] == "rule"
    assert proposals["proposed_by"] in team.team_cfg.agent_ids


def test_merged_proposals_still_validate():
    from scenario.ssos_eclss_loop.design_proposals import validate_design_proposals

    client = _FakeClient()
    team = _team(_config(with_design_team=True), client)

    proposals = team.propose_post_run_design(_summary())

    assert validate_design_proposals(proposals) == []
