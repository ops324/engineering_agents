"""Unit tests for SsosEclssLoopTeam."""

from __future__ import annotations

import pytest

from core.agents.base import Team
from scenario.agents.eclss_loop_types import EclssLoopObservation
from scenario.agents.ssos_eclss_loop_team import SsosEclssLoopTeam
from environment.ssos.eclss.types import ArsGoal, OgsGoal, EclssTelemetrySnapshot
from scenario.ssos_eclss_loop.loop_mock_backend import LoopMockEclssBackend


def _team_config():
    return {
        "mode": "labeled_rule_base",
        "memory_limit": 4,
        "discourse_window": 4,
        "team": {"count": 2, "id_prefix": "op", "persona": "operator"},
        "policy": {
            "co2_storage_high_kg": 1.5,
            "o2_storage_low_kg": 0.45,
            "request_co2_before_ogs": True,
            "request_co2_amount": 0.01,
            "ars_goal": {"initial_co2_mass": 1.8},
            "ogs_goal": {"input_water_mass": 0.015},
        },
    }


def test_set_crew_alive_drops_tail_and_noop_when_empty():
    team = SsosEclssLoopTeam(_team_config())
    lost = team.set_crew_alive(1)
    assert lost == ["op_2"]
    assert team.active_ids == ["op_1"]
    lost_again = team.set_crew_alive(0)
    assert lost_again == ["op_1"]
    backend = LoopMockEclssBackend({"simulation": {}, "mock_dynamics": {}})
    snap = backend.poll_telemetry()
    obs = EclssLoopObservation(step=3, telemetry=snap, health={"overall": "critical"})
    outcome = team.run_step(backend, obs)
    assert outcome.commands == []
    assert outcome.messages == []


def test_action_rep_id_does_not_resurrect_dead_operators():
    team = SsosEclssLoopTeam(_team_config())
    team.set_crew_alive(0)
    assert team.active_ids == []
    with pytest.raises(ValueError, match="no surviving operators"):
        team._action_rep_id(0)


def test_post_run_design_does_not_revive_depleted_actors():
    from scenario.agents.ssos_post_run_design import (
        ActorTeamSnapshot,
        DesignReviewBundle,
        PostRunDesignAgent,
    )

    team = SsosEclssLoopTeam(_team_config())
    team.set_crew_alive(0)
    designer = PostRunDesignAgent(
        {"mode": "labeled_rule_base", "team": {"count": 4, "id_prefix": "eclss_designer"}}
    )
    proposal = designer.propose(
        DesignReviewBundle(
            summary={"steps": 3, "crew_remaining": 0},
            scenario_config={},
            baseline_graph={},
            policy=team.policy,
            actor_snapshot=ActorTeamSnapshot(agent_ids=[], mode="labeled_rule_base", policy=team.policy),
        )
    )
    assert proposal["proposed_by"].startswith("eclss_designer_")
    assert not proposal["proposed_by"].startswith("op_")
    assert hasattr(team, "set_crew_alive")
    assert not hasattr(team, "propose_post_run_design")


def test_team_applies_ars_to_backend():
    team = SsosEclssLoopTeam(_team_config())
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 1.7, "initial_o2_storage_kg": 0.5},
            "mock_dynamics": {},
        }
    )
    snap = backend.poll_telemetry()
    obs = EclssLoopObservation(step=0, telemetry=snap, health={"overall": "warning"})
    outcome = team.run_step(backend, obs)
    assert len(outcome.commands) == 1
    assert outcome.commands[0].kind == "air_revitalisation"

    events = team.apply_outcome(backend, outcome)
    assert len(events) == 1
    assert events[0]["kind"] == "/eclss/events/operational_applied"
    assert backend.last_ars_goal is not None
    assert backend.poll_telemetry().co2_storage_kg < 1.7


def test_team_no_design_change_commands():
    team = SsosEclssLoopTeam(_team_config())
    backend = LoopMockEclssBackend({"simulation": {}, "mock_dynamics": {}})
    snap = EclssTelemetrySnapshot(co2_storage_kg=0.8, o2_storage_kg=0.6)
    obs = EclssLoopObservation(step=0, telemetry=snap, health={"overall": "safe"})
    outcome = team.run_step(backend, obs)
    assert outcome.commands == []


def test_llm_situation_uses_health_status_keys():
    from scenario.agents.ssos_eclss_loop_team import build_llm_situation

    snap = EclssTelemetrySnapshot(co2_storage_kg=1.6, o2_storage_kg=0.42)
    obs = EclssLoopObservation(
        step=2,
        telemetry=snap,
        health={
            "overall": "warning",
            "co2_status": "warning",
            "o2_status": "warning",
            "water_status": "safe",
        },
    )
    situation = build_llm_situation(obs)
    assert "co2_status=warning" in situation
    assert "o2_status=warning" in situation
    assert "water_status=safe" in situation
    assert "co2_storage=unknown" not in situation


def test_ssos_eclss_loop_team_is_team_subclass():
    team = SsosEclssLoopTeam(_team_config())
    assert isinstance(team, Team)


def test_team_rearms_ars_when_ineffective():
    team = SsosEclssLoopTeam(_team_config())
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 1.6, "initial_o2_storage_kg": 0.5},
            "mock_dynamics": {"co2_growth_kg_per_step": 0.0, "ars_co2_reduction_kg": 0.0},
        }
    )
    snap0 = backend.poll_telemetry()
    obs0 = EclssLoopObservation(step=0, telemetry=snap0, health={"overall": "warning"})
    outcome0 = team.run_step(backend, obs0)
    team.apply_outcome(backend, outcome0)
    assert team.state.ars_invoked is True
    assert snap0.co2_storage_kg == backend.poll_telemetry().co2_storage_kg

    backend.advance_step()
    snap1 = backend.poll_telemetry()
    obs1 = EclssLoopObservation(step=1, telemetry=snap1, health={"overall": "warning"})
    outcome1 = team.run_step(backend, obs1)
    assert any(cmd.kind == "air_revitalisation" for cmd in outcome1.commands)


def test_team_keeps_ars_running_until_the_recovery_target():
    """Recovery ends below the alarm line, not on it.

    This asserted the opposite until the rated-capacity invariant landed: the
    latch cleared the moment CO2 dipped under co2_storage_high_kg, and the
    warning band was one-shot. Both were survivable while a single ARS action
    removed four steps of crew CO2. At the rated margin it removes 1.082 steps'
    worth, so stopping on the line hands the cabin straight back to the band on
    the next breath -- and two consecutive steps in the band costs an occupant.
    Measured over 50 steps, the old rule left CO2 climbing from 1.3 to 3.0 kg
    with 37 consecutive steps in the band; recovering to a target holds it out.
    """
    team = SsosEclssLoopTeam(_team_config())
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 1.7, "initial_o2_storage_kg": 0.5},
            "mock_dynamics": {"co2_growth_kg_per_step": 0.1, "ars_co2_reduction_kg": 0.4},
        }
    )
    co2_high = float(team.policy["co2_storage_high_kg"])
    target = co2_high - float(team.policy.get("co2_recovery_margin_kg", 0.25))

    snap = backend.poll_telemetry()
    outcome0 = team.run_step(
        backend, EclssLoopObservation(step=0, telemetry=snap, health={"overall": "warning"})
    )
    team.apply_outcome(backend, outcome0)
    assert team.state.ars_invoked is True

    # Under the alarm line, but not yet recovered: keep running.
    backend.advance_step()
    snap1 = backend.poll_telemetry()
    assert target < snap1.co2_storage_kg < co2_high
    outcome1 = team.run_step(
        backend, EclssLoopObservation(step=1, telemetry=snap1, health={"overall": "safe"})
    )
    assert any(cmd.kind == "air_revitalisation" for cmd in outcome1.commands)
    assert team.state.ars_invoked is True
    team.apply_outcome(backend, outcome1)

    # At the recovery target: the episode closes and ARS stands down.
    backend.advance_step()
    snap2 = backend.poll_telemetry()
    assert snap2.co2_storage_kg <= target
    outcome2 = team.run_step(
        backend, EclssLoopObservation(step=2, telemetry=snap2, health={"overall": "safe"})
    )
    assert team.state.ars_invoked is False
    assert not any(cmd.kind == "air_revitalisation" for cmd in outcome2.commands)


def test_team_escalates_ars_on_critical_band():
    cfg = _team_config()
    cfg["policy"]["co2_storage_critical_kg"] = 2.2
    team = SsosEclssLoopTeam(cfg)
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 2.5, "initial_o2_storage_kg": 0.5},
            "mock_dynamics": {"ars_co2_reduction_kg": 0.1},
        }
    )
    snap = backend.poll_telemetry()
    obs = EclssLoopObservation(step=0, telemetry=snap, health={"overall": "critical"})
    outcome = team.run_step(backend, obs)
    assert any(c.kind == "air_revitalisation" for c in outcome.commands)
    team.apply_outcome(backend, outcome)
    assert team.state.ars_critical_escalated is True
    assert team.state.ars_invoked is True
    assert backend.last_ars_goal is not None
    # Escalated mass = policy ars_goal (1.8) * 1.5
    assert backend.last_ars_goal.initial_co2_mass == pytest.approx(2.7)


def test_team_keeps_ars_while_critical_after_partial_recovery():
    """Critical ARS must not stall when CO₂ drops but stays in the critical band."""
    cfg = _team_config()
    cfg["policy"]["co2_storage_high_kg"] = 1.5
    cfg["policy"]["co2_storage_critical_kg"] = 2.2
    team = SsosEclssLoopTeam(cfg)
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 2.5, "initial_o2_storage_kg": 0.5},
            # Partial drop: leave storage above critical (2.2) after first ARS.
            "mock_dynamics": {
                "ars_co2_reduction_kg": 0.2,
                "ars_reference_co2_mass_kg": 2.7,
                "co2_growth_kg_per_step": 0.0,
            },
        }
    )
    snap0 = backend.poll_telemetry()
    obs0 = EclssLoopObservation(step=0, telemetry=snap0, health={"overall": "critical"})
    outcome0 = team.run_step(backend, obs0)
    assert any(c.kind == "air_revitalisation" for c in outcome0.commands)
    team.apply_outcome(backend, outcome0)
    assert team.state.ars_critical_escalated is True

    backend.advance_step()
    snap1 = backend.poll_telemetry()
    assert snap1.co2_storage_kg < 2.5
    assert snap1.co2_storage_kg >= 2.2
    obs1 = EclssLoopObservation(step=1, telemetry=snap1, health={"overall": "critical"})
    outcome1 = team.run_step(backend, obs1)
    assert any(
        c.kind == "air_revitalisation" for c in outcome1.commands
    ), "must keep dispatching ARS while still in critical after partial recovery"


def test_loop_mock_request_o2_withdraws_plant_storage():
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_o2_storage_kg": 0.1},
            "mock_dynamics": {},
        }
    )
    backend.request_o2(0.025)
    assert backend.poll_telemetry().o2_storage_kg == pytest.approx(0.075)


def test_loop_mock_ars_scales_with_goal_mass():
    cfg = {
        "simulation": {"initial_co2_storage_kg": 2.0, "initial_o2_storage_kg": 0.5},
        "mock_dynamics": {"ars_co2_reduction_kg": 0.35, "ars_reference_co2_mass_kg": 1.8},
    }
    low = LoopMockEclssBackend(cfg)
    high = LoopMockEclssBackend(cfg)
    low.send_air_revitalisation_goal(ArsGoal(initial_co2_mass=0.9))
    high.send_air_revitalisation_goal(ArsGoal(initial_co2_mass=1.8))
    # Half reference → half reduction (0.175); full reference → 0.35
    assert low.poll_telemetry().co2_storage_kg == pytest.approx(2.0 - 0.175)
    assert high.poll_telemetry().co2_storage_kg == pytest.approx(2.0 - 0.35)


def test_loop_mock_water_tracks_ogs_without_double_subtract():
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_product_water_l": 50.0, "initial_o2_storage_kg": 0.4},
            "mock_dynamics": {},
        }
    )
    before = backend.poll_telemetry().product_water_reserve_l
    backend.send_oxygen_generation_goal(OgsGoal(input_water_mass=5.0))
    after = backend.poll_telemetry()
    assert after.product_water_reserve_l == pytest.approx(before - 5.0)
    assert backend._telemetry.product_water_reserve_l == pytest.approx(after.product_water_reserve_l)


def test_loop_mock_request_co2_withdraws_storage():
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 1.0},
            "mock_dynamics": {},
        }
    )
    result = backend.request_co2(0.25)
    assert result.success
    assert result.response_value == pytest.approx(0.25)
    assert backend.poll_telemetry().co2_storage_kg == pytest.approx(0.75)


def test_loop_mock_request_co2_rejects_partial_insufficient():
    """SSOS /ars/request_co2 rejects when storage cannot cover the full request."""
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 0.1},
            "mock_dynamics": {},
        }
    )
    before = backend.poll_telemetry().co2_storage_kg
    result = backend.request_co2(0.25)
    assert result.success is False
    assert result.response_value == pytest.approx(0.0)
    assert "insufficient" in (result.message or "").lower()
    assert backend.poll_telemetry().co2_storage_kg == pytest.approx(before)


def test_loop_mock_request_co2_exact_amount_succeeds():
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 0.25},
            "mock_dynamics": {},
        }
    )
    result = backend.request_co2(0.25)
    assert result.success
    assert result.response_value == pytest.approx(0.25)
    assert backend.poll_telemetry().co2_storage_kg == pytest.approx(0.0)


def test_loop_mock_failure_blocks_ars_physics():
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 2.0},
            "mock_dynamics": {"ars_co2_reduction_kg": 0.5},
        }
    )
    backend.set_subsystem_failure("ars", True)
    before = backend.poll_telemetry().co2_storage_kg
    result = backend.send_air_revitalisation_goal(ArsGoal(initial_co2_mass=1.8))
    assert result.success is False
    assert backend.poll_telemetry().co2_storage_kg == before


def test_loop_mock_rejects_negative_request_o2():
    backend = LoopMockEclssBackend(
        {"simulation": {"initial_o2_storage_kg": 0.5}, "mock_dynamics": {}}
    )
    before = backend.poll_telemetry().o2_storage_kg
    result = backend.request_o2(-0.1)
    assert result.success is False
    assert backend.poll_telemetry().o2_storage_kg == before


def test_loop_mock_request_o2_withdraws_storage():
    backend = LoopMockEclssBackend(
        {"simulation": {"initial_o2_storage_kg": 1.0}, "mock_dynamics": {}}
    )
    result = backend.request_o2(0.25)
    assert result.success
    assert result.response_value == pytest.approx(0.25)
    assert backend.poll_telemetry().o2_storage_kg == pytest.approx(0.75)


def test_loop_mock_request_o2_rejects_partial_insufficient():
    """SSOS /ogs/request_o2 rejects when storage cannot cover the full request."""
    backend = LoopMockEclssBackend(
        {"simulation": {"initial_o2_storage_kg": 0.1}, "mock_dynamics": {}}
    )
    before = backend.poll_telemetry().o2_storage_kg
    result = backend.request_o2(0.25)
    assert result.success is False
    assert result.response_value == pytest.approx(0.0)
    assert "insufficient" in (result.message or "").lower()
    assert backend.poll_telemetry().o2_storage_kg == pytest.approx(before)


def test_loop_mock_request_o2_exact_amount_succeeds():
    backend = LoopMockEclssBackend(
        {"simulation": {"initial_o2_storage_kg": 0.25}, "mock_dynamics": {}}
    )
    result = backend.request_o2(0.25)
    assert result.success
    assert result.response_value == pytest.approx(0.25)
    assert backend.poll_telemetry().o2_storage_kg == pytest.approx(0.0)


def test_llm_operational_parse_rejects_negative_amount():
    team = SsosEclssLoopTeam({"mode": "llm", "team": {"count": 1, "id_prefix": "op"}, "llm": {}})
    cmd, note = team._parse_llm_operational_command(
        {"kind": "request_o2", "payload": {"amount": -5.0}},
        issued_by="op_1",
    )
    assert cmd is None
    assert note is not None


def test_apply_command_emits_rejected_on_failure():
    from environment.ssos.eclss.types import ActionResult, ArsGoal
    from scenario.agents.eclss_loop_types import EclssOperationalCommand

    class _FailingBackend:
        def send_air_revitalisation_goal(self, goal: ArsGoal) -> ActionResult:
            return ActionResult(success=False, summary_message="ARS failed")

    team = SsosEclssLoopTeam(_team_config())
    event = team._apply_command(
        _FailingBackend(),  # type: ignore[arg-type]
        EclssOperationalCommand(
            kind="air_revitalisation",
            payload={"initial_co2_mass": 1.8},
            issued_by="op_1",
        ),
    )
    assert event is not None
    assert event["kind"] == "/eclss/events/operational_rejected"


def test_llm_operational_parse_air_revitalisation_and_request_co2():
    team = SsosEclssLoopTeam({"mode": "llm", "team": {"count": 1, "id_prefix": "op"}, "llm": {}})
    cmd, note = team._parse_llm_operational_command(
        {
            "kind": "air_revitalisation",
            "payload": {"initial_co2_mass": 1200.0, "initial_moisture_content": 20.0},
        },
        issued_by="op_1",
    )
    assert note is None
    assert cmd is not None
    assert cmd.kind == "air_revitalisation"
    assert cmd.payload["initial_co2_mass"] == 1200.0

    cmd2, note2 = team._parse_llm_operational_command(
        {"kind": "request_co2", "payload": {"amount": 15.0}},
        issued_by="op_1",
    )
    assert note2 is None
    assert cmd2 is not None
    assert cmd2.payload["amount"] == 15.0


def test_llm_design_parse_accepts_ssos_change_kinds():
    from scenario.agents.ssos_post_run_design import parse_llm_design_proposals

    changes, notes = parse_llm_design_proposals(
        [
            {
                "change_kind": "action_profile",
                "payload": {
                    "subsystem": "ars",
                    "action": "air_revitalisation",
                    "fields": {"initial_co2_mass": 2000.0},
                },
            },
            {
                "change_kind": "set_parameter",
                "payload": {"target": "agents.policy.co2_storage_high_kg", "value": 1600.0},
            },
        ]
    )
    assert not notes
    assert len(changes) == 2
    assert changes[0]["change_kind"] == "action_profile"


def test_llm_design_parse_keeps_any_number_of_valid_changes():
    from scenario.agents.ssos_post_run_design import parse_llm_design_proposals

    raw = [
        {
            "change_kind": "action_profile",
            "payload": {
                "subsystem": subsystem,
                "action": action,
                "fields": fields,
            },
        }
        for subsystem, action, fields in (
            ("ars", "air_revitalisation", {"initial_co2_mass": 2.0}),
            ("ogs", "oxygen_generation", {"input_water_mass": 0.2}),
            ("wrs", "water_recovery_systems", {"urine_volume": 3.0}),
        )
    ] + [
        {
            "change_kind": "set_parameter",
            "payload": {"target": "agents.policy.co2_storage_high_kg", "value": 1.4},
        },
        {
            "change_kind": "service_config",
            "payload": {"service": "request_co2", "amount": 0.03},
        },
    ]
    changes, notes = parse_llm_design_proposals(raw)
    assert not notes
    assert len(changes) == 5


def test_llm_design_parse_rejects_unknown_action_profile_fields():
    from scenario.agents.ssos_post_run_design import parse_llm_design_proposals

    changes, notes = parse_llm_design_proposals(
        [
            {
                "change_kind": "action_profile",
                "payload": {
                    "subsystem": "ogs",
                    "fields": {
                        "input_water_mass": 10.0,
                        "duration_steps": 5,
                    },
                },
            }
        ]
    )
    assert changes == []
    assert notes


def test_llm_design_parse_rejects_unknown_set_parameter_and_negative_profile():
    from scenario.agents.ssos_post_run_design import parse_llm_design_proposals

    unknown, unknown_notes = parse_llm_design_proposals(
        [
            {
                "change_kind": "set_parameter",
                "payload": {"target": "simulation.steps", "value": 9},
            }
        ]
    )
    assert unknown == []
    assert unknown_notes

    negative, negative_notes = parse_llm_design_proposals(
        [
            {
                "change_kind": "action_profile",
                "payload": {
                    "subsystem": "ars",
                    "fields": {"initial_co2_mass": -1.0},
                },
            }
        ]
    )
    assert negative == []
    assert negative_notes


def test_llm_design_parse_accepts_canonical_actor_policy_target():
    from scenario.agents.ssos_post_run_design import parse_llm_design_proposals

    changes, notes = parse_llm_design_proposals(
        [
            {
                "change_kind": "set_parameter",
                "payload": {
                    "target": "agents.actor.policy.co2_storage_high_kg",
                    "value": 1.4,
                },
            }
        ]
    )
    assert not notes
    assert changes[0]["payload"]["target"] == "agents.actor.policy.co2_storage_high_kg"


def test_llm_design_parse_rejects_empty_graph_rewire():
    from scenario.agents.ssos_post_run_design import parse_llm_design_proposals

    changes, notes = parse_llm_design_proposals(
        [{"change_kind": "graph_rewire", "payload": {}}]
    )
    assert changes == []
    assert notes


def test_post_run_message_step_is_last_zero_based_index():
    from scenario.agents.ssos_post_run_design import post_run_message_step

    assert post_run_message_step({"steps": 8}) == 7
    assert post_run_message_step({"steps": 1}) == 0
    assert post_run_message_step({"steps": 0}) == 0
    assert post_run_message_step({}) == 0


def test_action_rep_ids_default_is_single_rep():
    team = SsosEclssLoopTeam(_team_config())
    assert team.max_actions_per_step == 1
    assert team._action_rep_ids(0) == [team._action_rep_id(0)]
    assert team._action_rep_ids(1) == [team._action_rep_id(1)]


def test_action_rep_ids_rotates_window():
    cfg = _team_config()
    cfg["team"] = {"count": 4, "id_prefix": "op", "persona": "operator"}
    cfg["max_actions_per_step"] = 2
    team = SsosEclssLoopTeam(cfg)
    assert team.max_actions_per_step == 2
    assert team._action_rep_ids(0) == ["op_1", "op_2"]
    assert team._action_rep_ids(1) == ["op_2", "op_3"]
    assert team._action_rep_ids(3) == ["op_4", "op_1"]
    assert team._action_rep_id(0) == "op_1"


def test_max_actions_per_step_accepts_integral_float():
    cfg = _team_config()
    cfg["max_actions_per_step"] = 2.0
    team = SsosEclssLoopTeam(cfg)
    assert team.max_actions_per_step == 2


def test_max_actions_per_step_clamped_to_team_count():
    cfg = _team_config()
    cfg["max_actions_per_step"] = 99
    team = SsosEclssLoopTeam(cfg)
    assert team.max_actions_per_step == 2
    assert team._action_rep_ids(0) == ["op_1", "op_2"]


@pytest.mark.parametrize("bad", [0, -1, "nope", None, 2.9, True, "2.9"])
def test_max_actions_per_step_rejects_invalid(bad):
    cfg = _team_config()
    cfg["max_actions_per_step"] = bad
    with pytest.raises(ValueError, match="max_actions_per_step"):
        SsosEclssLoopTeam(cfg)


def test_labeled_mode_still_uses_one_policy_rep_when_max_actions_is_higher():
    cfg = _team_config()
    cfg["max_actions_per_step"] = 2
    team = SsosEclssLoopTeam(cfg)
    backend = LoopMockEclssBackend(
        {
            "simulation": {"initial_co2_storage_kg": 1.7, "initial_o2_storage_kg": 0.5},
            "mock_dynamics": {},
        }
    )
    snap = backend.poll_telemetry()
    obs = EclssLoopObservation(step=0, telemetry=snap, health={"overall": "warning"})
    outcome = team.run_step(backend, obs)
    assert len(outcome.commands) == 1
    assert outcome.commands[0].kind == "air_revitalisation"
    assert outcome.commands[0].issued_by == "op_1"


def test_llm_step_runs_multiple_action_reps(monkeypatch):
    import json

    action_prompts: list[str] = []

    class FakeClient:
        def generate(self, prompt: str) -> str:
            if "phase: action" in prompt.lower():
                action_prompts.append(prompt)
                return json.dumps(
                    {
                        "message": "dispatch ARS",
                        "reasoning": "test",
                        "commands": [
                            {
                                "kind": "air_revitalisation",
                                "payload": {
                                    "initial_co2_mass": 1.8,
                                    "initial_moisture_content": 25.0,
                                    "initial_contaminants": 5.0,
                                },
                            }
                        ],
                    }
                )
            return json.dumps({"message": "watching", "reasoning": "test"})

    monkeypatch.setattr(
        SsosEclssLoopTeam,
        "_build_llm_client",
        staticmethod(lambda _: FakeClient()),
    )
    cfg = _team_config()
    cfg["mode"] = "llm"
    cfg["llm"] = {}
    cfg["team"] = {"count": 4, "id_prefix": "op", "persona": "operator"}
    cfg["max_actions_per_step"] = 2
    team = SsosEclssLoopTeam(cfg)
    obs = EclssLoopObservation(
        step=0,
        telemetry=EclssTelemetrySnapshot(co2_storage_kg=1.7, o2_storage_kg=0.6),
        health={"overall": "warning"},
    )
    outcome = team._run_step_llm(obs)
    assert len(action_prompts) == 2
    assert any("action representative 1 of 2" in p for p in action_prompts)
    assert any("action representative 2 of 2" in p for p in action_prompts)
    assert len(outcome.commands) == 2
    action_msgs = [
        m for m in outcome.messages if m.metadata.get("deliberation_phase") == "action"
    ]
    assert {m.from_role for m in action_msgs} == {"op_1", "op_2"}


def test_llm_step_default_single_action_rep(monkeypatch):
    import json

    action_prompts: list[str] = []

    class FakeClient:
        def generate(self, prompt: str) -> str:
            if "phase: action" in prompt.lower():
                action_prompts.append(prompt)
                return json.dumps(
                    {
                        "message": "dispatch ARS",
                        "reasoning": "test",
                        "commands": [
                            {
                                "kind": "air_revitalisation",
                                "payload": {
                                    "initial_co2_mass": 1.8,
                                    "initial_moisture_content": 25.0,
                                    "initial_contaminants": 5.0,
                                },
                            }
                        ],
                    }
                )
            return json.dumps({"message": "watching", "reasoning": "test"})

    monkeypatch.setattr(
        SsosEclssLoopTeam,
        "_build_llm_client",
        staticmethod(lambda _: FakeClient()),
    )
    cfg = _team_config()
    cfg["mode"] = "llm"
    cfg["llm"] = {}
    team = SsosEclssLoopTeam(cfg)
    obs = EclssLoopObservation(
        step=0,
        telemetry=EclssTelemetrySnapshot(co2_storage_kg=1.7, o2_storage_kg=0.6),
        health={"overall": "warning"},
    )
    outcome = team._run_step_llm(obs)
    assert team.max_actions_per_step == 1
    assert len(action_prompts) == 1
    assert "team representative" in action_prompts[0]
    assert len(outcome.commands) == 1
    assert outcome.commands[0].issued_by == "op_1"

