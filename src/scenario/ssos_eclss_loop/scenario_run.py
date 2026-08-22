"""SsosEclssLoopScenario — EclssBackend poll loop with agent operational commands."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from core.code_version import describe_code_version
from core.event_log import EventLog
from core.llm.factory import describe_llm_target
from core.scenario import Scenario
from environment.ssos.eclss.backend import EclssBackend
from environment.ssos.eclss.types import EclssTelemetrySnapshot
from integrations.one_piece import export_run_provenance
from scenario.agents.eclss_loop_types import EclssLoopObservation
from scenario.agents.ssos_eclss_loop_team import SsosEclssLoopTeam
from scenario.jobs.resolve import resolve_run_directory
from scenario.runner import (
    _deep_merge,
    load_agents_config,
    scenario_config_path,
    write_effective_configs,
)
from scenario.ssos_eclss_loop.health import (
    build_effective_thresholds,
    compute_eclss_storage_health,
    health_inputs_note,
)
from scenario.ssos_eclss_loop.loop_mock_backend import LoopMockEclssBackend
from core.agents.adapter import adapter_provenance, apply_adapter, load_adapter
from scenario.ssos_eclss_loop.design_proposals import (
    apply_design_proposals,
    load_design_proposals,
    write_design_proposals,
)
from environment.ssos.eclss.ros2.graph_rewire import build_topic_remap
from environment.ssos.eclss.ros2.telemetry import reset_rclpy_telemetry_reader

from scenario.ssos_eclss_loop.policy import merge_labeled_policy_from_thresholds
from scenario.ssos_eclss_loop.subsystem_failures import (
    apply_scheduled_subsystem_failures,
    clear_scheduled_subsystem_failures,
    parse_subsystem_failure_schedule,
    resolve_inject_subsystem_failures,
    scheduled_subsystems,
)

logger = logging.getLogger(__name__)

BACKEND_ENV_VAR = "SSOS_ECLSS_BACKEND"


def _omit_nulls(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _storage_telemetry_missing(snap: EclssTelemetrySnapshot) -> bool:
    return (
        snap.co2_storage_kg is None
        and snap.o2_storage_kg is None
        and snap.product_water_reserve_l is None
    )


def _assert_ros2_storage_telemetry(step: int, snap: EclssTelemetrySnapshot) -> None:
    if not _storage_telemetry_missing(snap):
        return
    raise RuntimeError(
        "No ECLSS storage telemetry at step "
        f"{step} (/co2_storage, /o2_storage, /wrs/product_water_reserve all empty). "
        "ECLSS headless may still be starting or has stopped. From the host, re-run: "
        "ea run ssos_eclss_loop … (ea restarts headless automatically). "
        "Manual check inside the container: ros2 topic list | grep storage"
    )


def _wait_for_ros2_storage_telemetry(
    backend: EclssBackend,
    *,
    timeout_s: float,
    poll_interval_s: float = 0.5,
) -> EclssTelemetrySnapshot:
    """Block until storage telemetry arrives or timeout (ECLSS startup grace)."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        snap = backend.poll_telemetry()
        if not _storage_telemetry_missing(snap):
            return snap
        time.sleep(poll_interval_s)
    raise RuntimeError(
        "Timed out waiting for ECLSS storage telemetry after headless start "
        f"({timeout_s:.0f}s). Topics /co2_storage, /o2_storage, "
        "/wrs/product_water_reserve did not publish. "
        "Increase backend.ros2.startup_wait_s or check headless logs."
    )


def _telemetry_summary_fields(
    last_snap: Optional[EclssTelemetrySnapshot],
    peak_co2: Optional[float],
    min_o2: Optional[float],
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    if peak_co2 is not None:
        fields["peak_co2_storage_kg"] = round(peak_co2, 2)
    if last_snap is not None:
        if last_snap.co2_storage_kg is not None:
            fields["final_co2_storage_kg"] = last_snap.co2_storage_kg
        if last_snap.o2_storage_kg is not None:
            fields["final_o2_storage_kg"] = last_snap.o2_storage_kg
        if last_snap.product_water_reserve_l is not None:
            fields["final_product_water_reserve_l"] = last_snap.product_water_reserve_l
        if last_snap.raw_topics:
            fields["telemetry_topics_read"] = sorted(last_snap.raw_topics.keys())
    if min_o2 is not None:
        fields["min_o2_storage_kg"] = round(min_o2, 2)
    return fields


def resolve_backend_kind(
    config: Dict[str, Any],
    overrides: Optional[Dict[str, Any]] = None,
) -> str:
    if overrides:
        backend_override = overrides.get("backend") or {}
        kind = backend_override.get("kind")
        if kind:
            return str(kind)
    env_kind = os.environ.get(BACKEND_ENV_VAR)
    if env_kind:
        return env_kind
    return str(config.get("backend", {}).get("kind", "mock"))


def build_eclss_backend(config: Dict[str, Any], kind: Optional[str] = None) -> EclssBackend:
    backend_kind = kind or resolve_backend_kind(config)
    if backend_kind == "mock":
        return LoopMockEclssBackend(config)
    if backend_kind == "plant_sim":
        from environment.ssos.eclss.plant_sim import PlantSimEclssBackend

        return PlantSimEclssBackend.from_scenario_config(config)
    if backend_kind == "ros2":
        from environment.ssos.eclss.ros2.bridge import Ros2EclssBridge

        ros2_cfg = config.get("backend", {}).get("ros2", {}) or {}
        rewires = (config.get("ssos_graph") or {}).get("rewires") or []
        topic_timeout_s = float(ros2_cfg.get("topic_timeout_s", 15.0))
        telemetry_max_age_s = float(ros2_cfg.get("telemetry_max_age_s", topic_timeout_s * 2))
        return Ros2EclssBridge(
            action_timeout_s=float(ros2_cfg.get("action_timeout_s", 120.0)),
            topic_timeout_s=topic_timeout_s,
            telemetry_max_age_s=telemetry_max_age_s,
            topic_remap=build_topic_remap(rewires),
        )
    raise ValueError(
        f"Unknown ECLSS backend kind: {backend_kind!r} (expected mock, plant_sim, or ros2)"
    )


class SsosEclssLoopScenario(Scenario):
    @property
    def name(self) -> str:
        return "ssos_eclss_loop"

    def load_config(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with scenario_config_path(self.name).open(encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if overrides:
            config = _deep_merge(config, overrides)
        return config

    def build_simulator(self, config: Dict[str, Any]):
        raise NotImplementedError("ssos_eclss_loop uses EclssBackend, not SimulatorProtocol")

    def build_team(
        self,
        config: Dict[str, Any],
        agents_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[SsosEclssLoopTeam]:
        if agents_config is None:
            agents_config = load_agents_config(self.name, config)
        if not agents_config:
            return None
        mode = agents_config.get("mode")
        if mode not in {"labeled_rule_base", "llm"}:
            return None
        return SsosEclssLoopTeam(agents_config)

    def run(
        self,
        output_dir: Optional[Path] = None,
        overrides: Optional[Dict[str, Any]] = None,
        recreate_output: bool = True,
        apply_proposals_path: Optional[Path] = None,
        adapter_path: Optional[Path] = None,
        run_id: Optional[str] = None,
        results_root: Optional[Path] = None,
    ) -> Path:
        # Load order (before any simulation step):
        # 1) scenario.yaml (+ CLI overrides)
        # 2) --apply-proposals merges into in-memory config (disk YAML unchanged)
        # 3) agents.yaml ⊕ scenario.agents, then labeled policy from thresholds
        # 4) --adapter writes the self-modification surface (design.md 4). It
        #    lands after the agents config exists and can only reach the fields
        #    in core.agents.adapter; thresholds, gates and the evaluator are not
        #    among them, so F7 cannot loosen what judges it.
        config = self.load_config(overrides)
        applied_proposals_path: Optional[Path] = None
        if apply_proposals_path is not None:
            proposals = load_design_proposals(apply_proposals_path)
            config = apply_design_proposals(config, proposals)
            applied_proposals_path = Path(apply_proposals_path)
        thresholds = config.get("thresholds", {}) or {}
        agents_config = load_agents_config(self.name, config)
        if agents_config:
            agents_config = merge_labeled_policy_from_thresholds(agents_config, thresholds)
        adapter_update: Dict[str, Any] = {}
        if adapter_path is not None:
            adapter_update = load_adapter(Path(adapter_path))
            if agents_config:
                agents_config = apply_adapter(agents_config, adapter_update)
        sim_cfg = config.get("simulation", {})
        steps = int(sim_cfg.get("steps", 8))
        # The seed has to reach the sampler, not just the summary. Recording a
        # seed that nothing consumed makes repetitions look controlled when
        # they are independent re-rolls, which is the one thing the seed is
        # supposed to rule out (design.md 10.3).
        if agents_config and sim_cfg.get("seed") is not None:
            seeded_llm = dict(agents_config.get("llm") or {})
            seeded_llm.setdefault("seed", int(sim_cfg["seed"]))
            agents_config = {**agents_config, "llm": seeded_llm}
        output_cfg = config.get("output", {})
        backend_kind = resolve_backend_kind(config, overrides)
        # Persist the resolved kind (CLI / SSOS_ECLSS_BACKEND may differ from YAML).
        backend_section = config.get("backend")
        if not isinstance(backend_section, dict):
            backend_section = {}
            config["backend"] = backend_section
        backend_section["kind"] = backend_kind

        run_dir = resolve_run_directory(
            scenario_name=self.name,
            output_cfg=output_cfg,
            agents_config=agents_config,
            output_dir=output_dir,
            run_id=run_id,
            results_root=results_root,
            recreate_output=recreate_output,
        )
        config_paths = write_effective_configs(
            run_dir,
            scenario_config=config,
            agents_config=agents_config,
        )

        backend = build_eclss_backend(config, kind=backend_kind)
        team = self.build_team(config, agents_config=agents_config)
        log = EventLog(run_dir)
        inject_failures = resolve_inject_subsystem_failures(config)
        failure_schedule = (
            parse_subsystem_failure_schedule(config.get("subsystem_failures"))
            if inject_failures
            else []
        )
        # Seed False so the first inactive step does not emit a clear event.
        failure_flags_last: Dict[str, bool] = {
            sub: False for sub in scheduled_subsystems(failure_schedule)
        }

        ros2_cfg = (config.get("backend", {}) or {}).get("ros2", {}) or {}
        if backend_kind == "ros2":
            reset_rclpy_telemetry_reader()
            startup_wait_s = float(ros2_cfg.get("startup_wait_s", 45.0))
            _wait_for_ros2_storage_telemetry(backend, timeout_s=startup_wait_s)

        message_count = 0
        operational_command_count = 0
        ars_invoked_step: Optional[int] = None
        ogs_invoked_step: Optional[int] = None
        co2_requested_step: Optional[int] = None
        last_snap: Optional[EclssTelemetrySnapshot] = None
        last_health: Optional[Dict[str, Any]] = None
        peak_co2: Optional[float] = None
        min_o2: Optional[float] = None

        try:
            # 0-based steps: step 0 observes configured initial state; advance before 1..steps-1.
            for step in range(steps):
                if step > 0 and hasattr(backend, "advance_step"):
                    backend.advance_step()

                for event in apply_scheduled_subsystem_failures(
                    backend,
                    failure_schedule,
                    step,
                    last_enabled=failure_flags_last,
                ):
                    log.append("events", {"step": step, **event})

                snap = backend.poll_telemetry()
                if backend_kind == "ros2":
                    _assert_ros2_storage_telemetry(step, snap)
                last_snap = snap
                if snap.co2_storage_kg is not None:
                    peak_co2 = (
                        snap.co2_storage_kg
                        if peak_co2 is None
                        else max(peak_co2, snap.co2_storage_kg)
                    )
                if snap.o2_storage_kg is not None:
                    min_o2 = (
                        snap.o2_storage_kg
                        if min_o2 is None
                        else min(min_o2, snap.o2_storage_kg)
                    )

                health = compute_eclss_storage_health(step, snap, thresholds)
                last_health = health
                log.append("telemetry", {"step": step, **snap.to_dict()})
                log.append("health_metrics", health)
                # L10: per-step design/graph state for audit (ssos_graph remaps, etc.).
                log.append(
                    "design_state",
                    {
                        "step": step,
                        "ssos_graph": copy.deepcopy(config.get("ssos_graph") or {}),
                    },
                )

                if team is not None:
                    obs = EclssLoopObservation(step=step, telemetry=snap, health=health)
                    outcome = team.run_step(backend, obs)
                    events = team.apply_outcome(backend, outcome)
                    operational_command_count += len(outcome.commands)
                    for msg in outcome.messages:
                        log.append("messages", msg.to_dict())
                        message_count += 1
                    for event in events:
                        log.append("events", {"step": step, **event})
                        if event.get("kind") != "/eclss/events/operational_applied":
                            continue
                        cmd = (event.get("command") or {})
                        cmd_kind = cmd.get("kind")
                        if cmd_kind == "air_revitalisation" and ars_invoked_step is None:
                            ars_invoked_step = step
                        elif cmd_kind == "oxygen_generation" and ogs_invoked_step is None:
                            ogs_invoked_step = step
                        elif cmd_kind == "request_co2" and co2_requested_step is None:
                            co2_requested_step = step

                    # L5: refresh final telemetry/health after ops so summary reflects last actions.
                    if outcome.commands:
                        snap = backend.poll_telemetry()
                        if backend_kind == "ros2":
                            _assert_ros2_storage_telemetry(step, snap)
                        last_snap = snap
                        if snap.co2_storage_kg is not None:
                            peak_co2 = (
                                snap.co2_storage_kg
                                if peak_co2 is None
                                else max(peak_co2, snap.co2_storage_kg)
                            )
                        if snap.o2_storage_kg is not None:
                            min_o2 = (
                                snap.o2_storage_kg
                                if min_o2 is None
                                else min(min_o2, snap.o2_storage_kg)
                            )
                        health = compute_eclss_storage_health(step, snap, thresholds)
                        last_health = health
                        log.append("telemetry", {"step": step, "post_ops": True, **snap.to_dict()})
                        log.append("health_metrics", {**health, "post_ops": True})

        finally:
            clear_scheduled_subsystem_failures(backend, failure_schedule)

        summary: Dict[str, Any] = {
            "scenario": self.name,
            "backend": backend_kind,
            "agents_mode": (agents_config or {}).get("mode", "none"),
            "steps": steps,
            "inject_failures": inject_failures,
            **_telemetry_summary_fields(last_snap, peak_co2, min_o2),
            "final_health": last_health,
            "message_count": message_count,
            "operational_command_count": operational_command_count,
            "thresholds": build_effective_thresholds(thresholds),
            "health_inputs": health_inputs_note(),
            **config_paths,
        }
        summary.update(
            _omit_nulls(
                {
                    "ars_invoked_step": ars_invoked_step,
                    "ogs_invoked_step": ogs_invoked_step,
                    "co2_requested_step": co2_requested_step,
                    "apply_proposals_path": (
                        str(applied_proposals_path) if applied_proposals_path is not None else None
                    ),
                }
            )
        )

        # A run that cannot name the code and the backend that produced it
        # cannot be pooled with another run, and nothing downstream can tell
        # that it was pooled wrongly.
        summary["code_version"] = describe_code_version()
        if summary["agents_mode"] == "llm":
            llm_cfg = (agents_config or {}).get("llm") or {}
            provider, base_url, model = describe_llm_target(llm_cfg)
            summary["llm"] = _omit_nulls(
                {
                    "provider": provider,
                    "base_url": base_url,
                    "model": model,
                    "temperature": llm_cfg.get("temperature"),
                    "max_tokens": llm_cfg.get("max_tokens"),
                    "seed": llm_cfg.get("seed"),
                }
            )

        # Recorded in every run, self-modifying or not, so "F7 was absent" is a
        # value in the artifact rather than a missing key.
        summary["adapter"] = adapter_provenance(adapter_update)
        if adapter_path is not None:
            summary["adapter"]["source_path"] = str(adapter_path)

        if isinstance(team, SsosEclssLoopTeam) and team.mode in {"labeled_rule_base", "llm"}:
            summary["team_count"] = team.team_cfg.count
            summary["agent_ids"] = list(team.team_cfg.agent_ids)
            # Composition is recorded in every mode so a run artifact says which
            # configuration produced it (parity with scrubber_degradation).
            summary["archetypes"] = {
                aid: lens for aid, lens in team.team_cfg.archetypes
            }
            if team.design_team_cfg is not None:
                summary["design_team"] = {
                    aid: subsystem for aid, subsystem in team.design_team_cfg.subsystems
                }
                summary["design_proposer_kind"] = "subsystem_design_team"
            else:
                summary["design_team"] = {}
                summary["design_proposer_kind"] = "operator_rep"
            if team.mode == "llm":
                summary["max_actions_per_step"] = team.max_actions_per_step
            proposals = team.propose_post_run_design(summary)
            # Who actually proposed, not who was configured to. The design
            # team is a no-op in labeled_rule_base, and a summary claiming it
            # ran reads as "the manipulation was applied and did nothing" —
            # the most expensive possible way to be wrong about a null result.
            actual_proposer_kind = proposals.get("proposer_kind")
            if actual_proposer_kind:
                summary["design_proposer_kind"] = actual_proposer_kind
            # Counted after the proposal round so post-run design is included.
            usage_of = getattr(getattr(team, "llm_client", None), "usage", None)
            if callable(usage_of):
                summary["llm_usage"] = usage_of()
            # L8/B: only persist when there is at least one change so
            # --apply-proposals never no-ops from an empty document.
            change_count = len(proposals.get("changes") or [])
            summary["design_proposal_count"] = change_count
            if change_count > 0:
                proposals_path = run_dir / "design_proposals.json"
                write_design_proposals(proposals_path, proposals)
                summary["design_proposals_path"] = str(proposals_path)

        log.write_summary(summary)

        provenance_path = run_dir / "provenance.jsonl"
        provenance_count = 0
        try:
            provenance_path = export_run_provenance(run_dir)
            with provenance_path.open(encoding="utf-8") as f:
                provenance_count = sum(1 for line in f if line.strip())
        except Exception as exc:
            logger.warning("One Piece provenance export failed: %s", exc)
        summary["provenance_path"] = str(provenance_path)
        summary["provenance_record_count"] = provenance_count
        log.write_summary(summary)
        return run_dir


SCENARIO_REGISTRY: Dict[str, Scenario] = {
    "ssos_eclss_loop": SsosEclssLoopScenario(),
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run ssos_eclss_loop scenario")
    parser.add_argument(
        "--backend",
        choices=("mock", "plant_sim", "ros2"),
        help=f"EclssBackend kind (default: scenario.yaml or {BACKEND_ENV_VAR} env)",
    )
    parser.add_argument("--output-dir", type=Path, help="Run output directory")
    parser.add_argument(
        "--agents-mode",
        choices=("none", "labeled_rule_base", "llm"),
        help="Override agents.mode from scenario.yaml",
    )
    parser.add_argument("--steps", type=int, help="Override simulation.steps")
    parser.add_argument(
        "--apply-proposals",
        type=Path,
        metavar="PATH",
        help="Apply design_proposals.json from a prior run before executing",
    )
    args = parser.parse_args(argv)

    overrides: Dict[str, Any] = {}
    if args.backend:
        overrides["backend"] = {"kind": args.backend}
    if args.agents_mode:
        overrides["agents"] = {"mode": args.agents_mode}
    if args.steps is not None:
        overrides["simulation"] = {"steps": args.steps}

    from scenario.jobs.executor import execute_run
    from scenario.jobs.spec import RunSpec

    result = execute_run(
        RunSpec(
            scenario="ssos_eclss_loop",
            output_dir=args.output_dir,
            overrides=overrides or None,
            apply_proposals_path=args.apply_proposals,
        )
    )
    if result.exit_code != 0:
        print(result.error or "run failed", file=sys.stderr)
        return result.exit_code
    print(json.dumps(result.summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
