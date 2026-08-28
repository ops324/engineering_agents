"""Post-run ECLSS design agent — separate from in-sim actors."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.agents.memory import TeamMemoryStore
from core.agents.persona import (
    PersonaAgent,
    TeamConfig,
    build_personas,
    eclss_design_proposal_contract,
    load_team,
    message_contract,
    run_parallel,
)
from core.agents.types import AgentMessage, DeliberationPhase
from core.llm.base import LLMClient
from core.llm.factory import build_llm_client
from scenario.ssos_eclss_loop.design_proposals import (
    DESIGN_DOMAIN,
    SSOS_CHANGE_KINDS,
    build_design_proposals_from_run,
    explain_ssos_proposal_change,
    validate_ssos_proposal_change,
)


@dataclass
class ActorTeamSnapshot:
    agent_ids: List[str] = field(default_factory=list)
    mode: str = "none"
    state: Dict[str, Any] = field(default_factory=dict)
    discourse: List[AgentMessage] = field(default_factory=list)
    policy: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DesignReviewBundle:
    summary: Dict[str, Any]
    scenario_config: Dict[str, Any]
    baseline_graph: Dict[str, Any]
    policy: Dict[str, Any]
    actor_snapshot: Optional[ActorTeamSnapshot] = None


def post_run_message_step(summary: Dict[str, Any]) -> int:
    """Last 0-based simulation step (``0 .. steps-1``).

    Designer messages must land on a telemetry step so dashboard replay
    (bounded by telemetry min/max) can show them.
    """
    steps = int(summary.get("steps", 0) or 0)
    return max(steps - 1, 0)


class PostRunDesignAgent:
    """Homogeneous designer team invoked only after the simulation loop."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.mode = config.get("mode", "none")
        self.llm_mode = self.mode == "llm"
        self.llm_client = self._build_llm_client(config.get("llm", {})) if self.llm_mode else None
        team_cfg = dict(config)
        team_raw = dict(team_cfg.get("team") or {})
        team_raw.setdefault("id_prefix", "eclss_designer")
        team_raw.setdefault("count", 4)
        team_cfg["team"] = team_raw
        self.team_cfg: TeamConfig = load_team(team_cfg)
        self.personas = build_personas(self.team_cfg)
        self.memory_store = TeamMemoryStore(
            agent_ids=list(self.personas.keys()),
            memory_limit=int(config.get("memory_limit", 8)),
            discourse_window=int(config.get("discourse_window", 12)),
        )
        self.agents: Dict[str, PersonaAgent] = {
            agent_id: PersonaAgent(
                persona=persona,
                memory=self.memory_store.agent_memories[agent_id],
                llm_client=self.llm_client,
            )
            for agent_id, persona in self.personas.items()
        }

    def propose(self, bundle: DesignReviewBundle) -> Dict[str, Any]:
        baseline_graph = dict(bundle.baseline_graph or {})
        if self.llm_mode:
            return self._llm_propose(bundle, baseline_graph)
        proposed_by = self.team_cfg.agent_ids[0] if self.team_cfg.agent_ids else "eclss_designer_1"
        proposals = build_design_proposals_from_run(
            proposed_by=proposed_by,
            decision_source="rule",
            policy=bundle.policy,
            summary=bundle.summary,
            baseline_graph=baseline_graph or None,
        )
        proposals["deliberation_messages"] = [
            AgentMessage(
                step=post_run_message_step(bundle.summary),
                from_role=proposed_by,
                to_role="team",
                message=str(proposals.get("message") or ""),
                message_type="comment",
                reasoning=str(proposals.get("reasoning") or ""),
                metadata={
                    "decision_source": "rule",
                    "deliberation_phase": DeliberationPhase.POST_RUN,
                },
            ).to_dict()
        ]
        return proposals

    def _rep_id(self, summary: Dict[str, Any]) -> str:
        # Designers are a separate team from actors. Labeled always uses
        # designer[0] as the rule speaker. LLM rotates on the *designer* roster
        # using the final step index — not TeamConfig.action_rep_index, which
        # addresses in-sim actors.
        steps = post_run_message_step(summary)
        index = steps % self.team_cfg.count
        return self.team_cfg.agent_ids[index]

    def _llm_propose(
        self,
        bundle: DesignReviewBundle,
        baseline_graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        situation = build_llm_post_run_situation(bundle)
        contract = message_contract()
        step = post_run_message_step(bundle.summary)
        actor_discourse = list((bundle.actor_snapshot.discourse if bundle.actor_snapshot else [])[-8:])
        turns = run_parallel(
            [
                self._deliberation_turn(
                    agent_id=agent_id,
                    step=step,
                    situation=situation,
                    team_discourse=actor_discourse,
                    contract=contract,
                )
                for agent_id in self.team_cfg.agent_ids
            ]
        )
        step_discourse: List[AgentMessage] = []
        for agent_id, parsed in zip(self.team_cfg.agent_ids, turns):
            if parsed is None:
                continue
            step_discourse.append(
                AgentMessage(
                    step=step,
                    from_role=agent_id,
                    to_role="team",
                    message=str(parsed.data.get("message", "")),
                    message_type="comment",
                    reasoning=str(parsed.data.get("reasoning", "")),
                    metadata={
                        "decision_source": "llm",
                        "deliberation_phase": DeliberationPhase.DELIBERATION,
                    },
                )
            )

        rep = self._rep_id(bundle.summary)
        design_contract = eclss_design_proposal_contract()
        agent = self.agents[rep]
        ctx = agent.build_context(
            step=step,
            phase=DeliberationPhase.POST_RUN,
            situation=situation,
            step_discourse=step_discourse,
            team_discourse=actor_discourse + step_discourse,
        )
        parsed = agent.deliberate(
            ctx,
            design_contract,
            PersonaAgent.phase_hint(DeliberationPhase.POST_RUN),
            ("message", "reasoning", "changes"),
        )
        if parsed is None:
            fallback = build_design_proposals_from_run(
                proposed_by=rep,
                decision_source="llm_parse_fail",
                policy=bundle.policy,
                summary=bundle.summary,
                message="LLM response could not be parsed; fell back to rule proposals.",
                baseline_graph=baseline_graph or None,
            )
            fallback["reasoning"] = "LLM response could not be parsed."
            fallback["deliberation_messages"] = [
                msg.to_dict() for msg in step_discourse
            ] + [
                AgentMessage(
                    step=step,
                    from_role=rep,
                    to_role="team",
                    message=str(fallback.get("message") or ""),
                    message_type="comment",
                    reasoning="LLM response could not be parsed; using rule fallback.",
                    metadata={
                        "decision_source": "llm_parse_fail",
                        "deliberation_phase": DeliberationPhase.POST_RUN,
                    },
                ).to_dict()
            ]
            return fallback

        raw_changes = parsed.data.get("changes", [])
        changes, parse_notes, rejected = parse_llm_design_proposals_detailed(raw_changes)
        return {
            "design_domain": DESIGN_DOMAIN,
            "proposed_by": rep,
            "decision_source": "llm",
            "message": str(parsed.data.get("message", "")),
            "reasoning": str(parsed.data.get("reasoning", "")),
            "changes": changes,
            # What the model emitted, and why each refusal happened. Without
            # these a rejection rate cannot be diagnosed, only quoted (EXP-026).
            "changes_emitted": len(raw_changes) if isinstance(raw_changes, list) else None,
            "changes_rejected": rejected,
            "baseline_graph": baseline_graph,
            "parse_status": parsed.status,
            "parse_error": parsed.error,
            "parse_notes": parse_notes,
            "raw_response_excerpt": parsed.raw_excerpt,
            "deliberation_messages": [msg.to_dict() for msg in step_discourse]
            + [
                AgentMessage(
                    step=step,
                    from_role=rep,
                    to_role="team",
                    message=str(parsed.data.get("message", "")),
                    message_type="comment",
                    reasoning=str(parsed.data.get("reasoning", "")),
                    metadata={
                        "decision_source": "llm",
                        "deliberation_phase": DeliberationPhase.POST_RUN,
                    },
                ).to_dict()
            ],
        }

    async def _deliberation_turn(
        self,
        *,
        agent_id: str,
        step: int,
        situation: str,
        team_discourse: List[AgentMessage],
        contract: str,
    ):
        agent = self.agents[agent_id]
        ctx = agent.build_context(
            step=step,
            phase=DeliberationPhase.DELIBERATION,
            situation=situation,
            step_discourse=[],
            team_discourse=team_discourse,
        )
        return await agent.deliberate_async(
            ctx,
            contract,
            PersonaAgent.phase_hint(DeliberationPhase.DELIBERATION),
            ("message", "reasoning"),
        )

    @staticmethod
    def _build_llm_client(llm_cfg: Dict[str, Any]) -> LLMClient:
        return build_llm_client(llm_cfg)


def parse_llm_design_proposals(raw_changes: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Accept every valid change. One representative may emit any count."""
    accepted, notes, _ = parse_llm_design_proposals_detailed(raw_changes)
    return accepted, notes


def parse_llm_design_proposals_detailed(
    raw_changes: Any,
) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """As :func:`parse_llm_design_proposals`, and keep what was refused.

    A refusal used to leave one note -- ``invalid payload for set_parameter``,
    identical whether the target did not exist or the value was not a number --
    and the payload itself was dropped. An audit (2026-08-29, EXP-026) met a run
    that discarded 11 of 14 changes and could reconstruct only two of them,
    because the raw response had already been cut to 240 characters at each end.

    Measuring a rejection rate while being unable to say what was rejected is
    the designer-layer version of what EXP-008 recorded: 861 runs that can never
    be re-scored because the telemetry is gone.
    """
    if not isinstance(raw_changes, list):
        return [], ["changes is not a list"], []
    accepted: List[Dict[str, Any]] = []
    notes: List[str] = []
    rejected: List[Dict[str, Any]] = []

    def refuse(change_kind: str, payload: Any, reason: str) -> None:
        notes.append(reason)
        rejected.append({"change_kind": change_kind, "payload": payload, "reason": reason})

    for item in raw_changes:
        if not isinstance(item, dict):
            refuse("", item, "change item is not an object")
            continue
        change_kind = str(item.get("change_kind", "")).strip()
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        if change_kind not in SSOS_CHANGE_KINDS:
            refuse(change_kind, payload, f"unsupported change_kind: {change_kind}")
            continue
        why = explain_ssos_proposal_change(change_kind, payload)
        if why is not None:
            # The note keeps its old prefix so existing readers still match, and
            # gains the reason after it.
            refuse(change_kind, payload, f"invalid payload for {change_kind}: {why}")
            continue
        accepted.append({"change_kind": change_kind, "payload": payload})
    return accepted, notes, rejected


def build_llm_post_run_situation(bundle: DesignReviewBundle) -> str:
    summary = bundle.summary
    sim = bundle.scenario_config.get("simulation") or {}
    thresholds = bundle.scenario_config.get("thresholds") or {}
    snapshot = bundle.actor_snapshot
    telemetry_summary = (
        f"steps={summary.get('steps')}, peak_co2_storage_kg={summary.get('peak_co2_storage_kg')}, "
        f"min_o2_storage_kg={summary.get('min_o2_storage_kg')}, "
        f"final_co2_storage_kg={summary.get('final_co2_storage_kg')}, "
        f"final_o2_storage_kg={summary.get('final_o2_storage_kg')}, "
        f"operational_command_count={summary.get('operational_command_count')}, "
        f"ars_invoked_step={summary.get('ars_invoked_step')}, "
        f"ogs_invoked_step={summary.get('ogs_invoked_step')}, "
        f"co2_requested_step={summary.get('co2_requested_step')}"
    )
    initials = (
        f"initial_co2_storage_kg={sim.get('initial_co2_storage_kg')}, "
        f"initial_o2_storage_kg={sim.get('initial_o2_storage_kg')}, "
        f"initial_product_water_l={sim.get('initial_product_water_l')}"
    )
    # The occupants. Absent until 2026-08-29: the designer was shown gas masses,
    # command counts and health strings, and never told that anyone was aboard or
    # that anyone had died -- while the measure it is judged on is whether its
    # proposal saves a crew member (EXP-026). Stating the outcome is telling the
    # model what the run was for; it is not telling it which parameter to move.
    crew = (
        f"crew_initial={summary.get('crew_initial')}, "
        f"crew_remaining={summary.get('crew_remaining')}, "
        f"crew_lost={summary.get('crew_lost')}, "
        f"crew_lost_by_cause={json.dumps(summary.get('crew_lost_by_cause') or {}, ensure_ascii=False)}"
    )
    # Thresholds are supervision stubs for context — not a pass/fail verdict.
    req_stubs = json.dumps(thresholds, ensure_ascii=False)
    final_health = json.dumps(summary.get("final_health") or {}, ensure_ascii=False)
    actor_state = json.dumps(snapshot.state if snapshot else {}, ensure_ascii=False, default=str)
    discourse = snapshot.discourse if snapshot else []
    discourse_lines = (
        "\n".join(f"- {msg.from_role}: {msg.message}" for msg in discourse[-8:]) or "(none)"
    )
    graph = json.dumps(bundle.baseline_graph, ensure_ascii=False)
    return (
        "Post-run SSOS graph design review. Simulation complete. "
        "Do not judge verification pass/fail. "
        "One representative emits changes; include as many proposals as needed "
        "(no count cap).\n\n"
        f"### Initial conditions\n{initials}\n\n"
        f"### Occupants\n{crew}\n\n"
        f"### Verification requirement stubs (context only)\n{req_stubs}\n\n"
        f"### Telemetry\n{telemetry_summary}\n\n"
        f"### World state\n{final_health}\n\n"
        f"### Actor final state\n{actor_state}\n\n"
        f"### Actor discourse (recent)\n{discourse_lines}\n\n"
        f"Baseline ssos_graph at run end: {graph}"
    )


def actor_snapshot_from_team(team: Any) -> ActorTeamSnapshot:
    state = asdict(team.state) if hasattr(team, "state") else {}
    discourse = []
    if getattr(team, "memory_store", None) is not None:
        discourse = list(team.memory_store.discourse.recent())
    return ActorTeamSnapshot(
        agent_ids=list(getattr(team.team_cfg, "agent_ids", [])),
        mode=str(getattr(team, "mode", "none")),
        state=state,
        discourse=discourse,
        policy=dict(getattr(team, "policy", {}) or {}),
    )
