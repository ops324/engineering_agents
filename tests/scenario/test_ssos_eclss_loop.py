"""Tests for ssos_eclss_loop scenario (mock backend, no ROS2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scenario.runner import list_scenarios, run_scenario
from scenario.ssos_eclss_loop.scenario_run import (
    BACKEND_ENV_VAR,
    build_eclss_backend,
    resolve_backend_kind,
)
from scenario.ssos_eclss_loop.loop_mock_backend import LoopMockEclssBackend


def _ssos_agents(mode: str, *, count: int = 4, design_mode: str | None = None) -> dict:
    agents: dict = {
        "mode": mode,
        "actor": {"mode": mode, "team": {"count": count, "id_prefix": "eclss_actor"}},
    }
    if design_mode is not None:
        agents["design"] = {
            "mode": design_mode,
            "team": {"count": 4, "id_prefix": "eclss_designer"},
        }
    return {"agents": agents}


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_ssos_eclss_loop_steps_are_zero_based(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "steps",
        overrides={"simulation": {"steps": 10}},
        recreate_output=True,
    )
    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")
    steps = [row["step"] for row in telemetry]
    assert steps == list(range(10))
    assert "ssos_eclss_loop" in list_scenarios()


def test_ssos_eclss_loop_baseline_runs(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "baseline",
        recreate_output=True,
    )

    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")
    health = _read_jsonl(run_dir / "health_metrics.jsonl")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    assert summary["scenario"] == "ssos_eclss_loop"
    assert summary["backend"] == "mock"
    assert summary["agents_mode"] == "none"
    assert summary["steps"] == 50
    assert len(telemetry) == 50
    assert len(health) == 50
    assert summary["inject_failures"] is False
    assert summary["operational_command_count"] == 0
    assert summary["message_count"] == 0
    assert summary.get("ars_invoked_step") is None
    assert (run_dir / "provenance.jsonl").exists()
    assert (run_dir / "design_state.jsonl").exists()
    assert not (run_dir / "design_proposals.json").exists()

    co2_series = [row["co2_storage_kg"] for row in telemetry]
    assert co2_series[0] == pytest.approx(1.3)
    assert co2_series[-1] > co2_series[0], "CO2 should rise without agent intervention"
    assert all(row["ars_failure_enabled"] is False for row in telemetry)
    assert all(row["ogs_failure_enabled"] is False for row in telemetry)


def test_ssos_eclss_loop_yaml_schedule_applies_when_inject_enabled(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "inject_on",
        overrides={
            "simulation": {"steps": 21},
            "agents": {"mode": "none"},
            "inject_failures": True,
        },
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")
    by_step = {row["step"]: row for row in telemetry if not row.get("post_ops")}

    assert summary["inject_failures"] is True
    assert by_step[9]["ars_failure_enabled"] is False
    assert by_step[10]["ars_failure_enabled"] is True
    assert by_step[19]["ars_failure_enabled"] is True
    assert by_step[20]["ars_failure_enabled"] is False
    assert by_step[19]["ogs_failure_enabled"] is False
    assert by_step[20]["ogs_failure_enabled"] is True


def test_ssos_eclss_loop_labeled_agents_invoke_ars(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "labeled",
        overrides={
            **_ssos_agents("labeled_rule_base"),
            "plant_sim": {"crew": {"size": 4}},
            "simulation": {"initial_o2_storage_kg": 8.0},
        },
        recreate_output=True,
    )

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    messages = _read_jsonl(run_dir / "messages.jsonl")
    events = _read_jsonl(run_dir / "events.jsonl")
    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")

    assert summary["agents_mode"] == "labeled_rule_base"
    assert summary["actor_mode"] == "labeled_rule_base"
    assert summary["design_mode"] == "labeled_rule_base"
    assert "thresholds" in summary
    assert summary["thresholds"]["co2_storage_high_kg"] == pytest.approx(2.0)
    assert summary["thresholds"]["co2_storage_critical_kg"] == pytest.approx(8.0)
    assert "health_inputs" in summary
    assert summary["team_count"] == 4
    assert summary["agent_ids"] == [
        "eclss_actor_1",
        "eclss_actor_2",
        "eclss_actor_3",
        "eclss_actor_4",
    ]
    assert summary["message_count"] > 0
    assert summary["operational_command_count"] >= 1
    assert summary["ars_invoked_step"] == 12

    message_types = {m["message_type"] for m in messages}
    assert "alert" in message_types
    assert "operational_command" in message_types
    assert "design_change" not in message_types

    applied = [e for e in events if e.get("kind") == "/eclss/events/operational_applied"]
    assert any(
        (e.get("command") or {}).get("kind") == "air_revitalisation" for e in applied
    )

    assert telemetry[0]["step"] == 0
    assert telemetry[0]["co2_storage_kg"] == pytest.approx(1.3)
    assert telemetry[13]["co2_storage_kg"] < telemetry[12]["co2_storage_kg"], (
        "ARS should reduce CO2 storage after step 12"
    )
    assert (run_dir / "design_proposals.json").exists()
    assert summary.get("design_proposal_count", 0) >= 1
    proposals = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))
    assert proposals.get("design_domain") == "ssos_graph"


def test_ssos_eclss_loop_labeled_policy_matches_thresholds(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "policy_thresholds",
        overrides={
            **_ssos_agents("labeled_rule_base"),
            "thresholds": {"co2_storage_high_kg": 1.6, "o2_storage_low_kg": 0.43},
            "simulation": {"initial_co2_storage_kg": 1.65},
        },
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["ars_invoked_step"] == 0


def test_ssos_eclss_loop_labeled_reinvokes_ars_when_co2_reexceeds(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "labeled_rearm",
        overrides={
            **_ssos_agents("labeled_rule_base"),
            "simulation": {"initial_o2_storage_kg": 8.0},
        },
        recreate_output=True,
    )

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    events = _read_jsonl(run_dir / "events.jsonl")
    ars_steps = [
        e["step"]
        for e in events
        if e.get("kind") == "/eclss/events/operational_applied"
        and (e.get("command") or {}).get("kind") == "air_revitalisation"
    ]

    assert summary["operational_command_count"] >= 2
    assert 12 in ars_steps
    assert any(step > 12 for step in ars_steps), "ARS should re-fire after CO2 regrows past threshold"


def test_ssos_eclss_loop_provenance_includes_operational_records(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "labeled_prov",
        overrides={
            **_ssos_agents("labeled_rule_base"),
            "simulation": {"initial_o2_storage_kg": 8.0},
        },
        recreate_output=True,
    )

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    provenance = _read_jsonl(run_dir / "provenance.jsonl")
    operational = [p for p in provenance if p.get("record_type") == "operational"]

    assert summary["provenance_record_count"] >= 1
    assert operational, "expected SSOS operational provenance records"
    assert any(p.get("change_kind") == "air_revitalisation" for p in operational)
    assert operational[0]["trace"]["event_kind"] == "/eclss/events/operational_applied"
    assert operational[0]["trace"]["decision_source"] == "rule"


def test_ssos_eclss_loop_apply_proposals(tmp_path: Path):
    first = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "first",
        overrides=_ssos_agents("labeled_rule_base"),
        recreate_output=True,
    )
    proposals_path = first / "design_proposals.json"
    assert proposals_path.exists()

    from scenario.ssos_eclss_loop.scenario_run import SsosEclssLoopScenario

    second = SsosEclssLoopScenario().run(
        output_dir=tmp_path / "second",
        overrides=_ssos_agents("labeled_rule_base"),
        apply_proposals_path=proposals_path,
    )
    summary = json.loads((second / "summary.json").read_text(encoding="utf-8"))
    assert summary["operational_command_count"] >= 1
    assert summary["apply_proposals_path"] == str(proposals_path)
    assert (second / "scenario_config.yaml").exists()
    assert (second / "agents_config.yaml").exists()

    import yaml

    proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
    effective_agents = yaml.safe_load((second / "agents_config.yaml").read_text(encoding="utf-8"))
    effective_scenario = yaml.safe_load((second / "scenario_config.yaml").read_text(encoding="utf-8"))
    assert effective_scenario.get("agents", {}).get("mode") == "labeled_rule_base"
    assert (effective_agents.get("actor") or {}).get("mode") == "labeled_rule_base"

    # At least one applied change must appear in the dumped effective agents policy.
    applied_kinds = {c["change_kind"] for c in proposals.get("changes", [])}
    policy = (effective_agents.get("actor") or {}).get("policy") or effective_agents.get("policy") or {}
    if "action_profile" in applied_kinds:
        assert "ars_goal" in policy or "ogs_goal" in policy or "wrs_goal" in policy
    if "service_config" in applied_kinds:
        assert "request_co2_amount" in policy or "request_o2_amount" in policy
    if "set_parameter" in applied_kinds:
        # set_parameter may land in scenario thresholds and/or agents.policy
        assert "thresholds" in effective_scenario or any(
            k.endswith("_kg") or k.endswith("_l") for k in policy
        )


def test_ssos_eclss_loop_labeled_agents_ogs_when_o2_low(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "ogs",
        overrides={
            **_ssos_agents("labeled_rule_base"),
            "simulation": {"initial_o2_storage_kg": 0.42},
        },
        recreate_output=True,
    )

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    events = _read_jsonl(run_dir / "events.jsonl")

    assert summary["ogs_invoked_step"] == 0
    # Default policy leaves CO₂ feedstock to OGS-internal Sabatier (no explicit request_co2).
    assert summary.get("co2_requested_step") is None
    applied_kinds = {
        (e.get("command") or {}).get("kind")
        for e in events
        if e.get("kind") == "/eclss/events/operational_applied"
    }
    assert "oxygen_generation" in applied_kinds
    assert "request_co2" not in applied_kinds


def test_resolve_backend_kind_from_env(monkeypatch):
    config = {"backend": {"kind": "mock"}}
    monkeypatch.setenv(BACKEND_ENV_VAR, "ros2")
    assert resolve_backend_kind(config) == "ros2"


def test_effective_config_records_env_resolved_backend(tmp_path: Path, monkeypatch):
    """scenario_config.yaml must match the backend actually used (not stale YAML)."""
    import yaml

    monkeypatch.setenv(BACKEND_ENV_VAR, "plant_sim")
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "env_backend",
        overrides={"simulation": {"steps": 2}},
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    effective = yaml.safe_load((run_dir / "scenario_config.yaml").read_text(encoding="utf-8"))
    assert summary["backend"] == "plant_sim"
    assert effective["backend"]["kind"] == "plant_sim"


def test_resolve_backend_kind_override_wins(monkeypatch):
    config = {"backend": {"kind": "mock"}}
    monkeypatch.setenv(BACKEND_ENV_VAR, "ros2")
    assert resolve_backend_kind(config, overrides={"backend": {"kind": "mock"}}) == "mock"


def test_build_eclss_backend_mock():
    backend = build_eclss_backend({"simulation": {}, "mock_dynamics": {}}, kind="mock")
    assert isinstance(backend, LoopMockEclssBackend)


def test_build_eclss_backend_unknown_raises():
    with pytest.raises(ValueError, match="Unknown ECLSS backend"):
        build_eclss_backend({}, kind="invalid")


class _FakeEclssLlm:
    def generate(self, prompt: str) -> str:
        lower = prompt.lower()
        if "phase: deliberation" in lower:
            return json.dumps(
                {
                    "message": "CO2 storage at band edge; ARS may be warranted.",
                    "reasoning": "co2_storage_kg telemetry elevated",
                }
            )
        if "phase: action" in lower:
            return json.dumps(
                {
                    "message": "LLM action rep: start ARS air_revitalisation.",
                    "reasoning": "team consensus on high CO2 storage",
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
        if "phase: post_run_proposal" in lower:
            return json.dumps(
                {
                    "message": "LLM design: raise ARS CO2 mass setpoint for next run.",
                    "reasoning": "operational intervention indicates margin gap",
                    "changes": [
                        {
                            "change_kind": "action_profile",
                            "payload": {
                                "subsystem": "ars",
                                "action": "air_revitalisation",
                                "fields": {"initial_co2_mass": 2.0},
                            },
                        },
                        {
                            "change_kind": "action_profile",
                            "payload": {
                                "subsystem": "ogs",
                                "action": "oxygen_generation",
                                "fields": {"input_water_mass": 0.2},
                            },
                        },
                        {
                            "change_kind": "set_parameter",
                            "payload": {
                                "target": "agents.policy.co2_storage_high_kg",
                                "value": 1.4,
                            },
                        },
                    ],
                }
            )
        return "{}"


def test_ssos_eclss_loop_llm_agents_invoke_ars(tmp_path: Path, monkeypatch):
    from scenario.agents.ssos_eclss_loop_team import SsosEclssLoopTeam
    from scenario.agents.ssos_post_run_design import PostRunDesignAgent

    monkeypatch.setattr(SsosEclssLoopTeam, "_build_llm_client", staticmethod(lambda _: _FakeEclssLlm()))
    monkeypatch.setattr(PostRunDesignAgent, "_build_llm_client", staticmethod(lambda _: _FakeEclssLlm()))

    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "llm",
        overrides={
            "simulation": {"steps": 8},
            **_ssos_agents("llm"),
            "agents": {
                **_ssos_agents("llm")["agents"],
                "actor": {
                    **_ssos_agents("llm")["agents"]["actor"],
                    "max_actions_per_step": 1,
                },
            },
            "plant_sim": {"crew": {"size": 4}},
        },
        recreate_output=True,
    )

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    messages = _read_jsonl(run_dir / "messages.jsonl")
    design_proposals = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))

    assert summary["agents_mode"] == "llm"
    assert summary["design_mode"] == "llm"
    assert summary["team_count"] == 4
    assert summary["max_actions_per_step"] == 1
    assert summary["operational_command_count"] >= 1
    assert summary["ars_invoked_step"] == 0
    assert any(m.get("decision_source") == "llm" for m in messages)
    assert any(m.get("deliberation_phase") == "deliberation" for m in messages)
    assert any(m.get("deliberation_phase") == "action" for m in messages)
    assert design_proposals.get("decision_source") == "llm"
    assert design_proposals.get("proposed_by", "").startswith("eclss_designer_")
    assert design_proposals.get("design_domain") == "ssos_graph"
    assert len(design_proposals.get("changes", [])) == 3
    assert any(c.get("change_kind") == "action_profile" for c in design_proposals.get("changes", []))
    assert any(
        str(m.get("from_role", "")).startswith("eclss_designer_")
        and m.get("deliberation_phase") in {"deliberation", "post_run_proposal"}
        for m in messages
    )
    last_step = summary["steps"] - 1
    assert all(
        m.get("step") == last_step
        for m in messages
        if str(m.get("from_role", "")).startswith("eclss_designer_")
    )
    assert "deliberation_messages" not in design_proposals


def test_ssos_eclss_loop_llm_actor_design_none_skips_proposals(tmp_path: Path, monkeypatch):
    from scenario.agents.ssos_eclss_loop_team import SsosEclssLoopTeam

    monkeypatch.setattr(SsosEclssLoopTeam, "_build_llm_client", staticmethod(lambda _: _FakeEclssLlm()))
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "llm_actor_only",
        overrides={
            "simulation": {"steps": 4},
            **_ssos_agents("llm", design_mode="none"),
        },
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["actor_mode"] == "llm"
    assert summary["design_mode"] == "none"
    assert summary.get("design_proposal_count") in {None, 0}
    assert not (run_dir / "design_proposals.json").exists()


def test_ssos_eclss_loop_labeled_actor_llm_design(tmp_path: Path, monkeypatch):
    from scenario.agents.ssos_post_run_design import PostRunDesignAgent

    monkeypatch.setattr(PostRunDesignAgent, "_build_llm_client", staticmethod(lambda _: _FakeEclssLlm()))
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "labeled_llm_design",
        overrides=_ssos_agents("labeled_rule_base", design_mode="llm"),
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    proposals = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))
    assert summary["actor_mode"] == "labeled_rule_base"
    assert summary["design_mode"] == "llm"
    assert proposals.get("decision_source") == "llm"
    assert len(proposals.get("changes", [])) == 3
    assert proposals.get("proposed_by", "").startswith("eclss_designer_")
    messages = _read_jsonl(run_dir / "messages.jsonl")
    assert any(str(m.get("from_role", "")).startswith("eclss_designer_") for m in messages)
    last_step = summary["steps"] - 1
    assert all(
        m.get("step") == last_step
        for m in messages
        if str(m.get("from_role", "")).startswith("eclss_designer_")
    )


def test_ssos_llm_design_parse_fail_falls_back_to_rule_changes(tmp_path: Path, monkeypatch):
    from scenario.agents.ssos_post_run_design import PostRunDesignAgent

    class _UnparseableLlm:
        def generate(self, prompt: str) -> str:
            return "<<<not json>>>"

    monkeypatch.setattr(
        PostRunDesignAgent, "_build_llm_client", staticmethod(lambda _: _UnparseableLlm())
    )
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "parse_fail",
        overrides={
            **_ssos_agents("none", design_mode="llm"),
            "simulation": {"steps": 2},
        },
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    proposals = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))
    assert summary["design_decision_source"] == "llm_parse_fail"
    assert proposals["decision_source"] == "llm_parse_fail"
    assert proposals["changes"]
    assert "deliberation_messages" not in proposals


def test_ssos_eclss_loop_skips_empty_design_proposals_file(tmp_path: Path, monkeypatch):
    """L8/B: do not write design_proposals.json when changes is empty."""
    from scenario.agents.ssos_post_run_design import PostRunDesignAgent

    monkeypatch.setattr(
        PostRunDesignAgent,
        "propose",
        lambda self, bundle: {
            "design_domain": "ssos_graph",
            "proposed_by": "eclss_designer_1",
            "decision_source": "rule",
            "message": "",
            "changes": [],
        },
    )
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "empty_proposals",
        overrides=_ssos_agents("labeled_rule_base"),
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary.get("design_proposal_count") == 0
    assert "design_proposals_path" not in summary
    assert not (run_dir / "design_proposals.json").exists()


def test_ssos_eclss_loop_subsystem_failures_schedule_mock(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "failures_mock",
        overrides={
            "simulation": {"steps": 5},
            "agents": {"mode": "none"},
            "inject_failures": True,
            "subsystem_failures": [
                {"subsystem": "ars", "start_step": 2, "end_step": 4},
            ],
        },
        recreate_output=True,
    )
    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")
    by_step = {row["step"]: row for row in telemetry if not row.get("post_ops")}
    assert list(by_step) == [0, 1, 2, 3, 4]
    assert by_step[0]["ars_failure_enabled"] is False
    assert by_step[1]["ars_failure_enabled"] is False
    assert by_step[2]["ars_failure_enabled"] is True
    assert by_step[3]["ars_failure_enabled"] is True
    assert by_step[4]["ars_failure_enabled"] is False

    events = _read_jsonl(run_dir / "events.jsonl")
    failure_events = [e for e in events if e.get("kind") == "subsystem_failure_applied"]
    assert failure_events == [
        {
            "step": 2,
            "kind": "subsystem_failure_applied",
            "subsystem": "ars",
            "enabled": True,
            "source": "subsystem_failures",
        },
        {
            "step": 4,
            "kind": "subsystem_failure_applied",
            "subsystem": "ars",
            "enabled": False,
            "source": "subsystem_failures",
        },
    ]


def test_ssos_eclss_loop_subsystem_failures_schedule_plant_sim(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "failures_plant_sim",
        overrides={
            "backend": {"kind": "plant_sim"},
            "simulation": {"steps": 4},
            "agents": {"mode": "none"},
            "inject_failures": True,
            "subsystem_failures": [
                {"subsystem": "ogs", "start_step": 1, "duration_steps": 2},
                {"subsystem": "wrs", "start_step": 2},
            ],
        },
        recreate_output=True,
    )
    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")
    by_step = {row["step"]: row for row in telemetry if not row.get("post_ops")}
    assert list(by_step) == [0, 1, 2, 3]
    assert by_step[0]["ogs_failure_enabled"] is False
    assert by_step[0]["wrs_failure_enabled"] is False
    assert by_step[1]["ogs_failure_enabled"] is True
    assert by_step[1]["wrs_failure_enabled"] is False
    assert by_step[2]["ogs_failure_enabled"] is True
    assert by_step[2]["wrs_failure_enabled"] is True
    assert by_step[3]["ogs_failure_enabled"] is False
    assert by_step[3]["wrs_failure_enabled"] is True


def test_ssos_eclss_loop_clears_scheduled_failures_after_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backend = LoopMockEclssBackend(
        {"simulation": {"initial_co2_storage_kg": 1.0}, "mock_dynamics": {}}
    )

    def raise_after_injection():
        raise RuntimeError("simulated telemetry failure")

    monkeypatch.setattr(backend, "poll_telemetry", raise_after_injection)
    monkeypatch.setattr(
        "scenario.ssos_eclss_loop.scenario_run.build_eclss_backend",
        lambda config, kind=None: backend,
    )

    with pytest.raises(ValueError, match="simulated telemetry failure"):
        run_scenario(
            "ssos_eclss_loop",
            output_dir=tmp_path / "failure_cleanup",
            overrides={
                "simulation": {"steps": 1},
                "inject_failures": True,
                "subsystem_failures": [{"subsystem": "ars", "start_step": 0}],
            },
            recreate_output=True,
        )

    # The backend outlives the failed run, as a persistent ROS2 backend can.
    assert backend._failure_flags["ars"] is False


def test_ssos_eclss_loop_plant_sim_writes_thresholds_and_metabolism(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "plant_sim",
        overrides={
            "backend": {"kind": "plant_sim"},
            **_ssos_agents("labeled_rule_base", count=50),
            # Pin the occupant count too. This test asserts crew_initial == 50
            # and pinned only the actor side, so it was reading the crew from
            # whatever scenario.yaml happened to ship -- and broke the moment
            # the experimental condition moved. Occupants and actors are the
            # same people; a test that fixes one has to fix the other.
            "plant_sim": {"crew": {"size": 50}},
            "simulation": {"steps": 3},
        },
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")

    assert summary["backend"] == "plant_sim"
    assert "thresholds" in summary
    assert summary["thresholds"]["o2_storage_low_kg"] == pytest.approx(6.0)
    assert summary["thresholds"]["o2_storage_critical_kg"] == pytest.approx(1.0)

    metabolism_rows = [
        row
        for row in telemetry
        if isinstance((row.get("raw_topics") or {}).get("plant_sim"), dict)
        and "last_metabolism" in (row["raw_topics"]["plant_sim"])
        and row.get("post_ops") is not True
    ]
    assert len(metabolism_rows) == 2  # steps 1 and 2 (advance before poll)

    by_step = {}
    for row in telemetry:
        by_step.setdefault(row["step"], []).append(row)
    assert all(len(rows) <= 2 for rows in by_step.values())
    health = _read_jsonl(run_dir / "health_metrics.jsonl")
    health_by_step = {}
    for row in health:
        health_by_step.setdefault(row["step"], []).append(row)
    for step, rows in by_step.items():
        tel_post = sum(1 for row in rows if row.get("post_ops") is True)
        health_post = sum(1 for row in health_by_step[step] if row.get("post_ops") is True)
        assert tel_post == health_post <= 1

    assert "crew_remaining" in summary
    assert summary["crew_initial"] == 50
    topic = telemetry[-1]["raw_topics"]["plant_sim"]
    assert topic["crew_alive"] == summary["crew_remaining"]

    proposals_path = run_dir / "design_proposals.json"
    if proposals_path.exists():
        proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
        for change in proposals.get("changes", []):
            assert change.get("why")
            assert change.get("what")
            assert change.get("how")


def test_plant_sim_rejects_mismatched_team_count(tmp_path: Path):
    with pytest.raises(ValueError, match="must match actor.team.count"):
        run_scenario(
            "ssos_eclss_loop",
            output_dir=tmp_path / "mismatch",
            overrides={
                "backend": {"kind": "plant_sim"},
                "agents": {"mode": "labeled_rule_base"},
                "plant_sim": {"crew": {"size": 5}},
                "simulation": {"steps": 2},
            },
            recreate_output=True,
        )


def test_plant_sim_survival_off_skips_duplicate_post_ops(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "survival_off",
        overrides={
            "backend": {"kind": "plant_sim"},
            "agents": {"mode": "none"},
            "simulation": {"steps": 3},
            "plant_sim": {"survival": {"enabled": False}},
        },
        recreate_output=True,
    )
    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")
    health = _read_jsonl(run_dir / "health_metrics.jsonl")
    assert len(telemetry) == 3
    assert len(health) == 3
    assert all(row.get("post_ops") is not True for row in telemetry)
    assert all(row.get("post_ops") is not True for row in health)


def test_plant_sim_survival_drops_crew_when_o2_starved(tmp_path: Path):
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "starve",
        overrides={
            "backend": {"kind": "plant_sim"},
            "agents": {"mode": "none"},
            "simulation": {
                "steps": 6,
                "initial_o2_storage_kg": 0.02,
                "initial_product_water_l": 80.0,
                "initial_co2_storage_kg": 0.5,
            },
            "plant_sim": {"survival": {"enabled": True}},
        },
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["crew_remaining"] < summary["crew_initial"]
    assert summary["crew_lost"] == summary["crew_initial"] - summary["crew_remaining"]
    events = _read_jsonl(run_dir / "events.jsonl")
    assert any(row.get("kind") == "/eclss/events/crew_lost" for row in events)


def test_plant_sim_o2_warning_dwell_before_physics_floor(tmp_path: Path):
    """WARNING dwell drops one person while tanks still cover the next interval."""
    run_dir = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "dwell",
        overrides={
            "backend": {"kind": "plant_sim"},
            "agents": {"mode": "none"},
            "simulation": {
                "steps": 3,
                "initial_o2_storage_kg": 5.0,
                "initial_product_water_l": 80.0,
                "initial_co2_storage_kg": 0.5,
            },
            "plant_sim": {
                "crew": {"size": 4},
                "survival": {"enabled": True},
            },
        },
        recreate_output=True,
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["crew_initial"] == 4
    assert summary["crew_remaining"] == 3
    events = _read_jsonl(run_dir / "events.jsonl")
    lost_events = [row for row in events if row.get("kind") == "/eclss/events/crew_lost"]
    assert lost_events
    assert "o2_warning" in lost_events[0].get("limiting", [])
    assert "o2_physics" not in lost_events[0].get("limiting", [])
    telemetry = _read_jsonl(run_dir / "telemetry.jsonl")
    health = _read_jsonl(run_dir / "health_metrics.jsonl")
    by_step_tel = {}
    for row in telemetry:
        by_step_tel.setdefault(row["step"], []).append(row)
    by_step_health = {}
    for row in health:
        by_step_health.setdefault(row["step"], []).append(row)
    assert all(len(rows) <= 2 for rows in by_step_tel.values())
    assert all(len(rows) <= 2 for rows in by_step_health.values())
    lost_step = lost_events[0]["step"]
    post = next(row for row in by_step_tel[lost_step] if row.get("post_ops") is True)
    survival = (post.get("raw_topics") or {}).get("plant_sim", {}).get("survival") or {}
    assert survival.get("lost_this_step") == 1
    assert "o2_warning" in (survival.get("limiting") or [])
    assert any(row.get("post_ops") is True for row in by_step_health[lost_step])


def test_plant_sim_skips_physics_floor_on_final_step(tmp_path: Path):
    """Look-ahead O2 floor applies only when another advance_step remains."""
    starved = {
        "backend": {"kind": "plant_sim"},
        "agents": {"mode": "none"},
        "simulation": {
            "initial_o2_storage_kg": 0.02,
            "initial_product_water_l": 80.0,
            "initial_co2_storage_kg": 0.5,
        },
        # Keep 0.02 kg O2 out of WARNING/CRITICAL so only the physics floor can cut crew.
        "thresholds": {"o2_storage_low_kg": 0.01, "o2_storage_critical_kg": 0.005},
        "plant_sim": {"crew": {"size": 4}, "survival": {"enabled": True}},
    }
    one = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "final_only",
        overrides={**starved, "simulation": {**starved["simulation"], "steps": 1}},
        recreate_output=True,
    )
    one_summary = json.loads((one / "summary.json").read_text(encoding="utf-8"))
    assert one_summary["crew_initial"] == 4
    assert one_summary["crew_remaining"] == 4
    assert one_summary["crew_lost"] == 0

    two = run_scenario(
        "ssos_eclss_loop",
        output_dir=tmp_path / "has_next",
        overrides={**starved, "simulation": {**starved["simulation"], "steps": 2}},
        recreate_output=True,
    )
    two_summary = json.loads((two / "summary.json").read_text(encoding="utf-8"))
    assert two_summary["crew_remaining"] < 4
    events = _read_jsonl(two / "events.jsonl")
    lost_events = [row for row in events if row.get("kind") == "/eclss/events/crew_lost"]
    assert lost_events
    assert "o2_physics" in lost_events[0].get("limiting", [])
    assert lost_events[0]["step"] == 0

