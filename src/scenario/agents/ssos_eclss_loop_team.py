"""SSOS ECLSS loop agent team — operates EclssBackend instead of Mock ECLSS simulator."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.agents.base import Team
from core.agents.memory import TeamMemoryStore
from core.agents.persona import (
    DesignTeamConfig,
    Persona,
    PersonaAgent,
    TeamConfig,
    build_design_personas,
    build_personas,
    eclss_design_proposal_contract,
    critique_contract,
    eclss_operational_action_contract,
    load_design_team,
    load_team,
    message_contract,
    run_parallel,
)
from core.agents.types import AgentMessage, DeliberationPhase
from core.llm.base import LLMClient
from core.llm.factory import build_llm_client
from environment.ssos.eclss.backend import EclssBackend
from environment.ssos.eclss.types import ArsGoal, OgsGoal, WrsGoal
from scenario.agents.command_admissibility import is_command_admissible
from scenario.agents.eclss_loop_types import (
    EclssLoopObservation,
    EclssOperationalCommand,
    StepEclssOutcome,
)
from core.agents.adapter import (
    ADAPTER_SCHEMA_VERSION,
    META_ADAPTER_PERSONA,
    describe_current,
    describe_lineage,
    meta_adapter_contract,
    partition_proposal,
)
from scenario.ssos_eclss_loop.design_proposals import (
    DESIGN_DOMAIN,
    SSOS_CHANGE_KINDS,
    ACTION_PROFILE_FIELDS_BY_SUBSYSTEM,
    build_design_proposals_from_run,
)

_ECLSS_OPERATIONAL_KINDS = frozenset(
    {"air_revitalisation", "oxygen_generation", "water_recovery", "request_co2", "request_o2"}
)

_ARS_GOAL_FIELDS = frozenset({"initial_co2_mass", "initial_moisture_content", "initial_contaminants"})
_OGS_GOAL_FIELDS = frozenset({"input_water_mass", "iodine_concentration"})
_WRS_GOAL_FIELDS = frozenset({"urine_volume"})


def _resolve_max_actions_per_step(raw: Any, *, team_count: int) -> int:
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"max_actions_per_step must be an integer >= 1, got {raw!r}")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            raise ValueError(f"max_actions_per_step must be an integer >= 1, got {raw!r}")
        value = int(raw)
    elif isinstance(raw, str):
        try:
            as_float = float(raw.strip())
        except ValueError as exc:
            raise ValueError(
                f"max_actions_per_step must be an integer >= 1, got {raw!r}"
            ) from exc
        if not as_float.is_integer():
            raise ValueError(f"max_actions_per_step must be an integer >= 1, got {raw!r}")
        value = int(as_float)
    else:
        raise ValueError(f"max_actions_per_step must be an integer >= 1, got {raw!r}")
    if value < 1:
        raise ValueError(f"max_actions_per_step must be >= 1, got {value}")
    return min(value, team_count)


@dataclass
class EclssLoopTeamState:
    alert_sent: bool = False
    ars_invoked: bool = False
    ars_critical_escalated: bool = False
    co2_requested: bool = False
    ogs_invoked: bool = False
    wrs_invoked: bool = False
    co2_at_ars_dispatch: Optional[float] = None
    o2_at_ogs_dispatch: Optional[float] = None


class SsosEclssLoopTeam(Team):
    """Crew Simulation replacement — sends ARS/OGS goals and O2/CO2 service calls."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.mode = config.get("mode", "labeled_rule_base")
        self.state = EclssLoopTeamState()
        self.llm_mode = self.mode == "llm"
        self.llm_client = self._build_llm_client(config.get("llm", {})) if self.llm_mode else None
        # F6. The post-run roles (design proposers, Meta agent) may be served by
        # a different model from the crew — that mixed allocation is a level of
        # the registered factor, and until now one client served everyone, so
        # the level could not be built even with both models resident.
        # `design_llm` overlays the crew's llm block, so a config naming only a
        # model keeps the same endpoint and sampling.
        self.design_llm_client = self.llm_client
        design_llm_cfg = config.get("design_llm") or {}
        if self.llm_mode and design_llm_cfg:
            self.design_llm_client = build_llm_client(
                {**(config.get("llm") or {}), **design_llm_cfg}, prefer_config=True
            )

        self.team_cfg: TeamConfig = load_team(config)
        self.personas = build_personas(self.team_cfg)
        self.policy: Dict[str, Any] = (
            config.get("policy", {}) if self.mode == "labeled_rule_base" else {}
        )

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
        # F5. Absent by default, which is the registered baseline level. When on,
        # each operator is named to review exactly one colleague's reasoning
        # (i -> i+1 round robin, so everyone critiques once and is critiqued
        # once) and the action round sees the critiques as well as the comments.
        # Deterministic pairing rather than self-selected: who reviews whom is
        # then a property of the design, not of the run.
        self.critique_enabled = bool((config.get("critique") or {}).get("enabled", False))
        # F4. What the archive handed forward: the parent design and other
        # cells' elites, each with the outcome it got. Empty unless a chain
        # supplies it, so a lone run behaves exactly as before.
        self.lineage: Dict[str, Any] = config.get("lineage") or {}

        self.max_actions_per_step = _resolve_max_actions_per_step(
            config.get("max_actions_per_step", 1),
            team_count=self.team_cfg.count,
        )
        # F2. "central" is the registered baseline: a rotating window of
        # representatives converts the step's discourse into the team's
        # commands. "distributed" removes that job — every operator acts on its
        # own judgement, so the crew's output is the sum of individual decisions
        # rather than one integrated one.
        #
        # The level therefore changes how many operators act (2 of 10 becomes
        # 10 of 10 at the shipped settings), and that is the factor, not a side
        # effect: centralisation is measured in how many minds turn discourse
        # into commands. It does mean the distributed arm issues more commands
        # and costs more calls than the central one at equal run count. Both are
        # recorded in the summary so a result from this arm cannot be read
        # without them.
        integration_raw = (config.get("integration") or {}).get("mode", "central")
        integration_mode = str(integration_raw).strip().lower()
        if integration_mode not in ("central", "distributed"):
            raise ValueError(
                f"integration.mode must be 'central' or 'distributed', got {integration_raw!r}"
            )
        self.integration_mode = integration_mode

        # Post-run design proposers, separate from the operators above. Absent by
        # default, in which case an operator issues the proposal as before.
        self.design_team_cfg: Optional[DesignTeamConfig] = load_design_team(config)
        self.design_agents: Dict[str, PersonaAgent] = {}
        if self.design_team_cfg is not None:
            design_personas = build_design_personas(self.design_team_cfg)
            self.design_memory_store = TeamMemoryStore(
                agent_ids=list(design_personas),
                memory_limit=int(config.get("memory_limit", 8)),
                discourse_window=int(config.get("discourse_window", 12)),
            )
            self.design_agents = {
                agent_id: PersonaAgent(
                    persona=persona,
                    memory=self.design_memory_store.agent_memories[agent_id],
                    llm_client=self.design_llm_client,
                )
                for agent_id, persona in design_personas.items()
            }

        # The Meta agent (design.md 4). Absent by default, which is F7=absent:
        # no adapter proposal is made and the run is what it was before. It is
        # a separate agent rather than a borrowed operator for the same reason
        # the design proposers were separated — "who proposed this" has to be
        # answerable from the artifact.
        # The adapter fields as they actually stand for this run. Recorded here
        # once so the summary and the Meta agent's prompt cannot disagree about
        # what the crew was.
        self.adapter_state: Dict[str, Any] = {
            "team_count": self.team_cfg.count,
            "discourse_window": int(config.get("discourse_window", 12)),
            "memory_limit": int(config.get("memory_limit", 8)),
            "archetypes": [lens for _, lens in self.team_cfg.archetypes],
            "subsystems": [name for _, name in self.team_cfg.subsystems],
        }

        self.meta_agent_id: Optional[str] = None
        self.meta_agent: Optional[PersonaAgent] = None
        meta_raw = config.get("meta_agent") or {}
        if meta_raw and self.mode == "llm":
            self.meta_agent_id = str(meta_raw.get("id") or "meta_agent_1")
            meta_memory = TeamMemoryStore(
                agent_ids=[self.meta_agent_id],
                memory_limit=int(config.get("memory_limit", 8)),
                discourse_window=int(config.get("discourse_window", 12)),
            )
            self.meta_agent = PersonaAgent(
                persona=Persona(agent_id=self.meta_agent_id, persona=META_ADAPTER_PERSONA),
                memory=meta_memory.agent_memories[self.meta_agent_id],
                llm_client=self.design_llm_client,
            )

    def llm_usage(self) -> Dict[str, int]:
        """Spend across every client this team used.

        With one client the caller could read it directly. With two, reading
        one of them under-reports the budget — and decision 23 settles the
        contest in tokens, so an unaccounted client is an unaccounted arm.
        """
        clients = []
        for client in (self.llm_client, self.design_llm_client):
            if client is not None and not any(client is seen for seen in clients):
                clients.append(client)
        total: Dict[str, int] = {}
        for client in clients:
            # A client without accounting (test doubles, and any backend added
            # later) is skipped rather than crashing the run it was measuring.
            usage_of = getattr(client, "usage", None)
            if not callable(usage_of):
                continue
            for key, value in usage_of().items():
                total[key] = total.get(key, 0) + int(value)
        return total

    def llm_usage_by_role(self) -> Dict[str, Any]:
        """Spend per client, so "the big model was configured" and "the big
        model answered" stay distinguishable. A split that routes no call is
        a level that was declared and not built."""
        out: Dict[str, Any] = {}
        for role, client in (("crew", self.llm_client), ("post_run", self.design_llm_client)):
            usage_of = getattr(client, "usage", None)
            if callable(usage_of):
                out[role] = usage_of()
        if self.design_llm_client is self.llm_client:
            out.pop("post_run", None)
        return out

    def llm_roles(self) -> Dict[str, Any]:
        """Which model answered for which role. Never part of a score."""
        def model_of(client):
            return getattr(client, "model", None)
        return {
            "crew": model_of(self.llm_client),
            "post_run": model_of(self.design_llm_client),
            # A difference in what was asked of the backend, not in object
            # identity: two clients resolving to one model is not a split, and
            # reporting it as one is how an unbuilt level looks built.
            "split": (
                model_of(self.llm_client) != model_of(self.design_llm_client)
                or getattr(self.llm_client, "base_url", None)
                != getattr(self.design_llm_client, "base_url", None)
            ),
        }

    def propose_adapter_update(self, summary: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """One typed candidate for the next run's crew, or None when F7 is absent.

        Proposes only. design.md 4 puts acceptance with the archive and the
        evaluation, so nothing here is applied — not to this run, and not to
        the next one without something else choosing it.
        """
        if self.meta_agent is None or self.meta_agent_id is None:
            return None

        situation = (
            build_llm_post_run_situation(summary, self.memory_store.discourse.recent(), {})
            + "\n\n"
            + describe_current(self.adapter_state)
            + ("\n\n" + describe_lineage(self.lineage) if self.lineage else "")
        )
        ctx = self.meta_agent.build_context(
            step=int(summary.get("steps", 0)),
            phase=DeliberationPhase.POST_RUN,
            situation=situation,
            step_discourse=[],
            team_discourse=self.memory_store.discourse.recent(),
        )
        parsed = self.meta_agent.deliberate(
            ctx,
            meta_adapter_contract(),
            PersonaAgent.phase_hint(DeliberationPhase.POST_RUN),
            ("message", "reasoning", "fields"),
        )
        if parsed is None:
            return {
                "schema_version": ADAPTER_SCHEMA_VERSION,
                "proposed_by": self.meta_agent_id,
                "decision_source": "llm_parse_fail",
                "message": "",
                "reasoning": "LLM response could not be parsed.",
                "adapter": {"schema_version": ADAPTER_SCHEMA_VERSION, "fields": {}},
                "rejected": [],
                # Without this a parse failure and an agent with nothing to say
                # look identical in the artifact.
                "raw_response_excerpt": getattr(self.meta_agent, "last_raw_excerpt", None),
            }

        accepted, rejected = partition_proposal(parsed.data.get("fields", {}))
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "proposed_by": self.meta_agent_id,
            "decision_source": "llm",
            "message": str(parsed.data.get("message", "")),
            "reasoning": str(parsed.data.get("reasoning", "")),
            "adapter": {"schema_version": ADAPTER_SCHEMA_VERSION, "fields": accepted},
            "rejected": rejected,
            "parse_status": parsed.status,
            "parse_error": parsed.error,
            "raw_response_excerpt": parsed.raw_excerpt,
        }

    def _action_rep_id(self, step: int) -> str:
        """Round-robin representative for 0-based scenario steps (`step % N`)."""
        return self.team_cfg.agent_ids[step % self.team_cfg.count]

    def _actor_ids(self, step: int) -> List[str]:
        """Who acts this step. F2's two levels differ in exactly this."""
        if self.integration_mode == "distributed":
            # Every operator, in roster order. max_actions_per_step is the size
            # of the representative window and there is no window here, so it
            # does not apply — the summary records both so that is visible.
            return list(self.team_cfg.agent_ids)
        return self._action_rep_ids(step)

    def _action_rep_ids(self, step: int) -> List[str]:
        """Rotating window of action representatives (length ``max_actions_per_step``)."""
        n = self.team_cfg.count
        k = min(self.max_actions_per_step, n)
        start = step % n
        ids = self.team_cfg.agent_ids
        return [ids[(start + offset) % n] for offset in range(k)]

    def run_step(self, backend: EclssBackend, obs: EclssLoopObservation) -> StepEclssOutcome:
        _ = backend
        if self.llm_mode:
            outcome = self._run_step_llm(obs)
            self.memory_store.commit_step(outcome)
            return outcome
        return self._run_step_labeled(obs)

    def apply_outcome(self, backend: EclssBackend, outcome: StepEclssOutcome) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        # Single choke point for every command, whatever produced it. The rule
        # base passes through the same gate as the LLM: a policy can be
        # misconfigured too, and a gate that only one path crosses is not a
        # gate. Structural only — scarcity belongs to the plant, which already
        # saturates requests rather than failing them.
        for cmd in outcome.commands:
            verdict = is_command_admissible(cmd.kind, cmd.payload)
            if not verdict.admissible:
                events.append({
                    "kind": "/eclss/events/operational_inadmissible",
                    "command": cmd.to_dict(),
                    "message": verdict.summary,
                    "admissibility": verdict.to_dict(),
                    "decision_source": "deterministic_gate",
                })
                continue
            event = self._apply_command(backend, cmd)
            if event is not None:
                events.append(event)
        return events

    def propose_post_run_design(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        baseline_graph = dict(self.config.get("ssos_graph") or {})
        steps = int(summary.get("steps", 0))
        rep = self._action_rep_id(steps - 1 if steps > 0 else 0)
        if self.llm_mode:
            if self.design_agents:
                return self._llm_subsystem_design_proposals(summary, baseline_graph)
            return self._llm_post_run_design_proposal(summary, baseline_graph, rep)
        return build_design_proposals_from_run(
            proposed_by=rep,
            decision_source="rule",
            policy=self.policy,
            summary=summary,
            baseline_graph=baseline_graph or None,
        )

    def _llm_subsystem_design_proposals(
        self,
        summary: Dict[str, Any],
        baseline_graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Each subsystem proposer writes simultaneously; their changes are merged.

        Simultaneous by design, for the same reason the deliberation round is:
        a proposer that sees its peers' drafts converges on them. Cross-proposal
        reconciliation is the ``systems_integration`` proposer's job, and it is
        done from the run evidence rather than from the other drafts.
        """
        assert self.design_team_cfg is not None
        subsystem_by_agent = dict(self.design_team_cfg.subsystems)
        agent_ids = list(self.design_team_cfg.agent_ids)
        results = run_parallel(
            [
                self._llm_subsystem_design_turn(
                    summary=summary,
                    baseline_graph=baseline_graph,
                    agent_id=agent_id,
                    subsystem=subsystem_by_agent[agent_id],
                    n_proposers=len(agent_ids),
                )
                for agent_id in agent_ids
            ]
        )

        changes: List[Dict[str, Any]] = []
        contributions: List[Dict[str, Any]] = []
        parse_notes: List[str] = []
        messages: List[str] = []
        for agent_id, result in zip(agent_ids, results):
            contributions.append(result)
            parse_notes.extend(
                f"{agent_id}: {note}" for note in result.get("parse_notes", [])
            )
            if result.get("message"):
                messages.append(f"{agent_id}: {result['message']}")
            for change in result.get("changes", []):
                # proposed_by rides along per change so provenance survives the
                # merge; validate_design_proposals ignores extra keys.
                changes.append({**change, "proposed_by": agent_id})

        sources = {c.get("decision_source") for c in contributions}
        decision_source = "llm" if "llm" in sources else "llm_parse_fail"
        return {
            "design_domain": DESIGN_DOMAIN,
            "proposed_by": ",".join(agent_ids),
            "proposer_kind": "subsystem_design_team",
            "decision_source": decision_source,
            "message": " | ".join(messages),
            "reasoning": "Merged from simultaneous per-subsystem design proposals.",
            "changes": changes,
            "baseline_graph": baseline_graph,
            "parse_notes": parse_notes,
            "contributions": contributions,
        }

    async def _llm_subsystem_design_turn(
        self,
        *,
        summary: Dict[str, Any],
        baseline_graph: Dict[str, Any],
        agent_id: str,
        subsystem: str,
        n_proposers: int,
    ) -> Dict[str, Any]:
        discourse = self.memory_store.discourse.recent()
        situation = build_llm_post_run_situation(summary, discourse, baseline_graph)
        agent = self.design_agents[agent_id]
        ctx = agent.build_context(
            step=int(summary.get("steps", 0)),
            phase=DeliberationPhase.POST_RUN,
            situation=situation,
            step_discourse=[],
            # Operators' discourse is evidence for the proposer, which is why the
            # design agent reads the operator store rather than its own.
            team_discourse=discourse,
        )
        parsed = await agent.deliberate_async(
            ctx,
            eclss_design_proposal_contract(),
            PersonaAgent.design_round_hint(subsystem=subsystem, n_proposers=n_proposers),
            ("message", "reasoning", "changes"),
        )
        if parsed is None:
            return {
                "agent_id": agent_id,
                "subsystem": subsystem,
                "decision_source": "llm_parse_fail",
                "message": "",
                "reasoning": "LLM response could not be parsed.",
                "changes": [],
                "parse_notes": ["response could not be parsed"],
            }
        changes, notes = self._parse_llm_design_proposals(parsed.data.get("changes", []))
        return {
            "agent_id": agent_id,
            "subsystem": subsystem,
            "decision_source": "llm",
            "message": str(parsed.data.get("message", "")),
            "reasoning": str(parsed.data.get("reasoning", "")),
            "changes": changes,
            "parse_status": parsed.status,
            "parse_error": parsed.error,
            "parse_notes": notes,
            "raw_response_excerpt": parsed.raw_excerpt,
        }

    def _run_step_llm(self, obs: EclssLoopObservation) -> StepEclssOutcome:
        outcome = StepEclssOutcome()
        step_discourse: List[AgentMessage] = []
        situation = build_llm_situation(obs)
        # Simultaneous round: all agents see prior-step team discourse only, so
        # vLLM can batch the N in-flight requests instead of walking the roster.
        team_discourse = self.memory_store.discourse.recent()
        turns = run_parallel(
            [
                self._llm_deliberation_turn(
                    obs=obs,
                    agent_id=agent_id,
                    to_role="team",
                    message_type="comment",
                    phase=DeliberationPhase.DELIBERATION,
                    situation=situation,
                    step_discourse=[],
                    team_discourse=team_discourse,
                    contract=message_contract(),
                    required=("message",),
                )
                for agent_id in self.team_cfg.agent_ids
            ]
        )
        for agent_id, msg in zip(self.team_cfg.agent_ids, turns):
            if msg is not None:
                outcome.messages.append(msg)
                step_discourse.append(msg)
            else:
                outcome.messages.append(
                    self._llm_skip(
                        obs=obs,
                        agent_id=agent_id,
                        phase=DeliberationPhase.DELIBERATION,
                        reason="parse_failed_or_empty_message",
                        decision_source="llm_parse_fail",
                    )
                )

        if self.critique_enabled and len(step_discourse) > 1:
            step_discourse.extend(self._llm_critique_round(obs, situation, step_discourse, outcome))

        reps = self._actor_ids(obs.step)
        action_turns = run_parallel(
            [
                self._llm_action_turn(
                    obs,
                    situation,
                    step_discourse,
                    rep,
                    n_reps=len(reps),
                    slot=slot,
                )
                for slot, rep in enumerate(reps)
            ]
        )
        for action_msgs, action_cmds in action_turns:
            outcome.messages.extend(action_msgs)
            outcome.commands.extend(action_cmds)
        return outcome

    def _llm_critique_round(
        self,
        obs: EclssLoopObservation,
        situation: str,
        step_discourse: List[AgentMessage],
        outcome: StepEclssOutcome,
    ) -> List[AgentMessage]:
        """Each commenter reviews the next one's comment. F5's middle stage."""
        commenters = [m.from_role for m in step_discourse]
        pairs = [
            (critic, commenters[(index + 1) % len(commenters)])
            for index, critic in enumerate(commenters)
        ]
        turns = run_parallel(
            [
                self._llm_deliberation_turn(
                    obs=obs,
                    agent_id=critic,
                    to_role=target,
                    message_type="critique",
                    phase=DeliberationPhase.CRITIQUE,
                    situation=situation,
                    # Only the round under review, so a critic reads the claim
                    # rather than the whole step.
                    step_discourse=[m for m in step_discourse if m.from_role == target],
                    team_discourse=[],
                    contract=critique_contract(target),
                    required=("message",),
                )
                for critic, target in pairs
            ]
        )
        written: List[AgentMessage] = []
        for (critic, _target), msg in zip(pairs, turns):
            if msg is None:
                outcome.messages.append(
                    self._llm_skip(
                        obs=obs,
                        agent_id=critic,
                        phase=DeliberationPhase.CRITIQUE,
                        reason="parse_failed_or_empty_message",
                        decision_source="llm_parse_fail",
                    )
                )
                continue
            outcome.messages.append(msg)
            written.append(msg)
        return written

    def _run_step_labeled(self, obs: EclssLoopObservation) -> StepEclssOutcome:
        outcome = StepEclssOutcome()
        rep = self._action_rep_id(obs.step)
        co2_high = float(self.policy.get("co2_storage_high_kg", 1.5))
        co2_critical = float(self.policy.get("co2_storage_critical_kg", 2.2))
        o2_low = float(self.policy.get("o2_storage_low_kg", 0.45))
        co2 = obs.telemetry.co2_storage_kg
        o2 = obs.telemetry.o2_storage_kg

        if co2 is not None and co2 >= co2_high and not self.state.alert_sent:
            commenter = rep
            self.state.alert_sent = True
            band = "critical" if co2 >= co2_critical else "high"
            outcome.messages.append(
                AgentMessage(
                    step=obs.step,
                    from_role=commenter,
                    to_role="team",
                    message=(
                        f"CO2 storage {co2:.1f} kg exceeds {band} band "
                        f"({co2_critical:.1f} kg critical / {co2_high:.1f} kg high)."
                    ),
                    message_type="alert",
                    reasoning="Storage telemetry threshold crossed.",
                    metadata=self._rule_metadata(),
                )
            )

        messages, commands = self._labeled_recovery(
            obs, rep, co2_high, co2_critical, o2_low, co2, o2
        )
        outcome.messages.extend(messages)
        outcome.commands.extend(commands)
        return outcome

    def _rearm_labeled_recovery(
        self,
        co2: Optional[float],
        o2: Optional[float],
        co2_high: float,
        o2_low: float,
    ) -> None:
        """Re-arm one-shot flags when telemetry returns to the safe band."""
        if co2 is not None and co2 < co2_high:
            self.state.ars_invoked = False
            self.state.ars_critical_escalated = False
            self.state.alert_sent = False
            self.state.co2_at_ars_dispatch = None
        elif (
            self.state.ars_invoked
            and co2 is not None
            and self.state.co2_at_ars_dispatch is not None
            and co2 >= self.state.co2_at_ars_dispatch
        ):
            # ARS had no effect — allow retry on the next step.
            self.state.ars_invoked = False
        if o2 is not None and o2 > o2_low:
            self.state.ogs_invoked = False
            self.state.co2_requested = False
            self.state.o2_at_ogs_dispatch = None
        elif (
            self.state.ogs_invoked
            and o2 is not None
            and self.state.o2_at_ogs_dispatch is not None
            and o2 <= self.state.o2_at_ogs_dispatch
        ):
            self.state.ogs_invoked = False

    def _labeled_recovery(
        self,
        obs: EclssLoopObservation,
        rep: str,
        co2_high: float,
        co2_critical: float,
        o2_low: float,
        co2: Optional[float],
        o2: Optional[float],
    ) -> Tuple[List[AgentMessage], List[EclssOperationalCommand]]:
        self._rearm_labeled_recovery(co2, o2, co2_high, o2_low)
        messages: List[AgentMessage] = []
        commands: List[EclssOperationalCommand] = []

        in_critical = co2 is not None and co2 >= co2_critical
        # High/warning band is one-shot (ars_invoked). Critical band keeps
        # recovering until CO₂ leaves critical — otherwise a partial ARS drop
        # that stays >= critical stalls with both latches set.
        need_ars = co2 is not None and co2 >= co2_high and (
            not self.state.ars_invoked or in_critical
        )
        if need_ars:
            ars_payload = dict(self.policy.get("ars_goal", {}))
            if in_critical:
                # Escalate processed mass when verification critical band is breached (T3).
                base_mass = float(ars_payload.get("initial_co2_mass", 1.8))
                ars_payload["initial_co2_mass"] = base_mass * 1.5
            commands.append(
                EclssOperationalCommand(
                    kind="air_revitalisation",
                    payload=ars_payload,
                    issued_by=rep,
                )
            )
            self.state.ars_invoked = True
            self.state.co2_at_ars_dispatch = co2
            if in_critical:
                self.state.ars_critical_escalated = True
            reason = (
                f"CO2 storage {co2:.1f} kg >= critical {co2_critical:.1f} kg; escalated ARS."
                if in_critical
                else f"CO2 storage {co2:.1f} kg >= {co2_high:.1f} kg."
            )
            messages.append(
                AgentMessage(
                    step=obs.step,
                    from_role=rep,
                    to_role="team",
                    message=(
                        "Starting escalated ARS air_revitalisation (critical band)."
                        if in_critical
                        else "Starting ARS air_revitalisation to vent CO2 from storage."
                    ),
                    message_type="operational_command",
                    reasoning=reason,
                    metadata=self._rule_metadata(),
                )
            )

        if o2 is not None and o2 <= o2_low and not self.state.ogs_invoked:
            # Opt-in: explicit request_co2 before OGS. Default is false because real SSOS
            # OGS already calls /ars/request_co2 for Sabatier. With LoopMock (no CO₂ buffer),
            # true also runs OGS Sabatier storage debit in the same step → double CO₂ draw.
            if self.policy.get("request_co2_before_ogs", False) and not self.state.co2_requested:
                amount = float(self.policy.get("request_co2_amount", 0.025))
                commands.append(
                    EclssOperationalCommand(
                        kind="request_co2",
                        payload={"amount": amount},
                        issued_by=rep,
                    )
                )
                self.state.co2_requested = True
                messages.append(
                    AgentMessage(
                        step=obs.step,
                        from_role=rep,
                        to_role="team",
                        message=f"Requesting {amount:.1f} kg CO2 feedstock for Sabatier (OGS).",
                        message_type="operational_command",
                        reasoning=f"O2 storage {o2:.1f} kg <= {o2_low:.1f} kg.",
                        metadata=self._rule_metadata(),
                    )
                )

            ogs_payload = dict(self.policy.get("ogs_goal", {}))
            commands.append(
                EclssOperationalCommand(
                    kind="oxygen_generation",
                    payload=ogs_payload,
                    issued_by=rep,
                )
            )
            self.state.ogs_invoked = True
            self.state.o2_at_ogs_dispatch = o2
            messages.append(
                AgentMessage(
                    step=obs.step,
                    from_role=rep,
                    to_role="team",
                    message="Starting OGS oxygen_generation cycle.",
                    message_type="operational_command",
                    reasoning=f"O2 storage {o2:.1f} kg <= {o2_low:.1f} kg.",
                    metadata=self._rule_metadata(),
                )
            )

        # WRS: reclaim water once urine/grey feed has accumulated (plant_sim closes
        # the water loop). Threshold-gated, so it re-fires as buffers refill.
        raw_topics = obs.telemetry.raw_topics or {}
        plant_sim_topics = raw_topics.get("plant_sim") if isinstance(raw_topics, dict) else {}
        urine_buffer_l = 0.0
        if isinstance(plant_sim_topics, dict):
            urine_buffer_l = float(plant_sim_topics.get("urine_buffer_l") or 0.0)
        waste_feed_l = float(obs.telemetry.grey_water_collected_l or 0.0) + urine_buffer_l
        wrs_trigger_l = float(self.policy.get("wrs_feed_trigger_l", 0.5))
        if waste_feed_l >= wrs_trigger_l:
            wrs_payload = dict(self.policy.get("wrs_goal", {"urine_volume": 2.0}))
            commands.append(
                EclssOperationalCommand(
                    kind="water_recovery",
                    payload=wrs_payload,
                    issued_by=rep,
                )
            )
            messages.append(
                AgentMessage(
                    step=obs.step,
                    from_role=rep,
                    to_role="team",
                    message="Starting WRS water_recovery to reclaim urine/grey water.",
                    message_type="operational_command",
                    reasoning=f"Waste feed {waste_feed_l:.2f} L >= {wrs_trigger_l:.2f} L.",
                    metadata=self._rule_metadata(),
                )
            )

        return messages, commands

    async def _llm_deliberation_turn(
        self,
        *,
        obs: EclssLoopObservation,
        agent_id: str,
        to_role: str,
        message_type: str,
        phase: str,
        situation: str,
        step_discourse: List[AgentMessage],
        team_discourse: List[AgentMessage],
        contract: str,
        required: tuple[str, ...],
    ) -> Optional[AgentMessage]:
        agent = self.agents[agent_id]
        ctx = agent.build_context(
            step=obs.step,
            phase=phase,
            situation=situation,
            step_discourse=step_discourse,
            team_discourse=team_discourse,
        )
        parsed = await agent.deliberate_async(
            ctx,
            contract,
            PersonaAgent.phase_hint(phase),
            required,
        )
        if parsed is None:
            return None
        message = str(parsed.data.get("message", "")).strip()
        if not message:
            return None
        metadata: Dict[str, Any] = {
            "decision_source": "llm",
            "deliberation_phase": phase,
            "parse_status": parsed.status,
            "parse_error": parsed.error,
            "raw_response_excerpt": parsed.raw_excerpt,
        }
        llm_memory = parsed.data.get("memory")
        if llm_memory:
            metadata["llm_memory"] = str(llm_memory)
        return AgentMessage(
            step=obs.step,
            from_role=agent_id,
            to_role=to_role,
            message=message,
            message_type=message_type,
            reasoning=str(parsed.data.get("reasoning", "")),
            metadata=metadata,
        )

    async def _llm_action_turn(
        self,
        obs: EclssLoopObservation,
        situation: str,
        step_discourse: List[AgentMessage],
        rep: str,
        n_reps: int = 1,
        slot: int = 0,
    ) -> Tuple[List[AgentMessage], List[EclssOperationalCommand]]:
        contract = eclss_operational_action_contract()
        agent = self.agents[rep]
        ctx = agent.build_context(
            step=obs.step,
            phase=DeliberationPhase.ACTION,
            situation=situation,
            step_discourse=step_discourse,
            team_discourse=self.memory_store.discourse.recent(),
        )
        parsed = await agent.deliberate_async(
            ctx,
            contract,
            PersonaAgent.action_round_hint(
                n_reps=n_reps, slot=slot, integration=self.integration_mode
            ),
            ("commands",),
        )
        if parsed is None:
            return [
                self._llm_skip(
                    obs=obs,
                    agent_id=rep,
                    phase=DeliberationPhase.ACTION,
                    reason="parse_failed",
                    decision_source="llm_parse_fail",
                )
            ], []

        message = parsed.data.get("message", "Assessed current state.")
        reasoning = parsed.data.get("reasoning", "")
        commands: List[EclssOperationalCommand] = []
        parse_notes: List[str] = []
        raw_commands = parsed.data.get("commands", [])
        if not isinstance(raw_commands, list):
            raw_commands = []

        for item in raw_commands:
            cmd, note = self._parse_llm_operational_command(item, issued_by=rep)
            if note:
                parse_notes.append(note)
            if cmd is not None:
                commands.append(cmd)

        base_meta: Dict[str, Any] = {
            "decision_source": "llm",
            "deliberation_phase": DeliberationPhase.ACTION,
            "parse_status": parsed.status,
            "parse_error": parsed.error,
            "raw_response_excerpt": parsed.raw_excerpt,
            "parse_notes": parse_notes,
        }
        if parsed.data.get("memory"):
            base_meta["llm_memory"] = str(parsed.data["memory"])

        if not commands:
            return [
                self._llm_skip(
                    obs=obs,
                    agent_id=rep,
                    phase=DeliberationPhase.ACTION,
                    reason="empty_commands",
                    decision_source="llm_no_action",
                    parse_status=parsed.status,
                    parse_error=parsed.error,
                )
            ], []

        llm_msg = AgentMessage(
            step=obs.step,
            from_role=rep,
            to_role="team",
            message=str(message),
            message_type="operational_command",
            reasoning=str(reasoning),
            metadata=base_meta,
        )
        return [llm_msg], commands

    def _llm_post_run_design_proposal(
        self,
        summary: Dict[str, Any],
        baseline_graph: Dict[str, Any],
        rep: str,
    ) -> Dict[str, Any]:
        situation = build_llm_post_run_situation(
            summary,
            self.memory_store.discourse.recent(),
            baseline_graph,
        )
        contract = eclss_design_proposal_contract()
        # The proposer role, not the operator, decides which model answers here
        # (F6). With design_proposer_kind=operator_rep the proposal is issued by
        # a crew member, and reusing that agent object would send the whole run
        # through the proposer's model. A shadow over the same persona and the
        # same memory keeps the run on the crew's model and this one call on the
        # proposer's — otherwise the mixed allocation records a split while
        # nothing crosses it.
        agent = self.agents[rep]
        if self.design_llm_client is not self.llm_client:
            agent = PersonaAgent(
                persona=agent.persona,
                memory=agent.memory,
                llm_client=self.design_llm_client,
            )
        ctx = agent.build_context(
            step=int(summary.get("steps", 0)),
            phase=DeliberationPhase.POST_RUN,
            situation=situation,
            step_discourse=[],
            team_discourse=self.memory_store.discourse.recent(),
        )
        parsed = agent.deliberate(
            ctx,
            contract,
            PersonaAgent.phase_hint(DeliberationPhase.POST_RUN),
            ("message", "reasoning", "changes"),
        )
        if parsed is None:
            return {
                "design_domain": DESIGN_DOMAIN,
                "proposed_by": rep,
                "proposer_kind": "operator_rep",
                "decision_source": "llm_parse_fail",
                "message": "",
                "reasoning": "LLM response could not be parsed.",
                "changes": [],
                "baseline_graph": baseline_graph,
                "parse_notes": [],
            }

        changes, parse_notes = self._parse_llm_design_proposals(parsed.data.get("changes", []))
        return {
            "design_domain": DESIGN_DOMAIN,
            "proposed_by": rep,
            "proposer_kind": "operator_rep",
            "decision_source": "llm",
            "message": str(parsed.data.get("message", "")),
            "reasoning": str(parsed.data.get("reasoning", "")),
            "changes": changes,
            "baseline_graph": baseline_graph,
            "parse_status": parsed.status,
            "parse_error": parsed.error,
            "parse_notes": parse_notes,
            "raw_response_excerpt": parsed.raw_excerpt,
        }

    def _parse_llm_design_proposals(
        self,
        raw_changes: Any,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        if not isinstance(raw_changes, list):
            return [], ["changes is not a list"]
        accepted: List[Dict[str, Any]] = []
        notes: List[str] = []
        for item in raw_changes:
            if not isinstance(item, dict):
                notes.append("change item is not an object")
                continue
            change_kind = str(item.get("change_kind", "")).strip()
            payload = item.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            if change_kind not in SSOS_CHANGE_KINDS:
                notes.append(f"unsupported change_kind: {change_kind}")
                continue
            if self._validate_ssos_proposal_change(change_kind, payload) is None:
                notes.append(f"invalid payload for {change_kind}")
                continue
            accepted.append({"change_kind": change_kind, "payload": payload})
        return accepted, notes

    def _validate_ssos_proposal_change(
        self,
        change_kind: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if change_kind == "action_profile":
            subsystem = str(payload.get("subsystem", "")).lower()
            fields = payload.get("fields")
            if subsystem not in ACTION_PROFILE_FIELDS_BY_SUBSYSTEM:
                return None
            if not isinstance(fields, dict) or not fields:
                return None
            allowed = ACTION_PROFILE_FIELDS_BY_SUBSYSTEM[subsystem]
            if any(key not in allowed for key in fields):
                return None
            return payload
        if change_kind == "service_config":
            service = str(payload.get("service", "")).lower()
            if service not in {"request_co2", "request_o2"}:
                return None
            return payload
        if change_kind == "set_parameter":
            if not str(payload.get("target", "")).strip():
                return None
            return payload
        if change_kind == "graph_rewire":
            return payload if payload else None
        return None

    def _parse_llm_operational_command(
        self,
        item: Any,
        *,
        issued_by: str,
    ) -> Tuple[Optional[EclssOperationalCommand], Optional[str]]:
        if not isinstance(item, dict):
            return None, "operational command is not an object"
        kind = str(item.get("kind", "")).strip()
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if kind not in _ECLSS_OPERATIONAL_KINDS:
            return None, f"unsupported operational kind: {kind}"

        if kind == "air_revitalisation":
            normalized = self._normalize_numeric_fields(payload, _ARS_GOAL_FIELDS)
            if normalized is None:
                return None, "air_revitalisation payload needs numeric ARS goal fields"
            return EclssOperationalCommand(kind=kind, payload=normalized, issued_by=issued_by), None

        if kind == "oxygen_generation":
            normalized = self._normalize_numeric_fields(payload, _OGS_GOAL_FIELDS)
            if normalized is None:
                return None, "oxygen_generation payload needs numeric OGS goal fields"
            return EclssOperationalCommand(kind=kind, payload=normalized, issued_by=issued_by), None

        if kind == "water_recovery":
            normalized = self._normalize_numeric_fields(payload, _WRS_GOAL_FIELDS)
            if normalized is None:
                return None, "water_recovery payload needs numeric WRS goal fields"
            return EclssOperationalCommand(kind=kind, payload=normalized, issued_by=issued_by), None

        if kind in {"request_co2", "request_o2"}:
            try:
                amount = float(payload.get("amount"))
            except (TypeError, ValueError):
                return None, f"{kind} payload.amount must be numeric"
            if not math.isfinite(amount) or amount <= 0.0:
                return None, f"{kind} payload.amount must be finite and positive"
            return (
                EclssOperationalCommand(kind=kind, payload={"amount": amount}, issued_by=issued_by),
                None,
            )

        return None, f"unsupported operational kind: {kind}"

    @staticmethod
    def _normalize_numeric_fields(
        payload: Dict[str, Any],
        allowed: frozenset[str],
    ) -> Optional[Dict[str, float]]:
        if not payload:
            return None
        normalized: Dict[str, float] = {}
        for key, value in payload.items():
            if key not in allowed:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            # L7/D5: reject NaN/Inf/negative quantities from LLM payloads.
            if not math.isfinite(number) or number < 0.0:
                return None
            normalized[key] = number
        return normalized or None

    def _llm_skip(
        self,
        *,
        obs: EclssLoopObservation,
        agent_id: str,
        phase: str,
        reason: str,
        decision_source: str,
        **extra: Any,
    ) -> AgentMessage:
        metadata: Dict[str, Any] = {
            "decision_source": decision_source,
            "deliberation_phase": phase,
            "skip_reason": reason,
        }
        metadata.update(extra)
        return AgentMessage(
            step=obs.step,
            from_role=agent_id,
            to_role="team",
            message="",
            message_type="skip",
            reasoning=reason,
            metadata=metadata,
        )

    def _apply_command(
        self,
        backend: EclssBackend,
        cmd: EclssOperationalCommand,
    ) -> Optional[Dict[str, Any]]:
        kind = cmd.kind
        payload = cmd.payload
        if kind == "air_revitalisation":
            result = backend.send_air_revitalisation_goal(ArsGoal(**payload))
        elif kind == "oxygen_generation":
            result = backend.send_oxygen_generation_goal(OgsGoal(**payload))
        elif kind == "water_recovery":
            result = backend.send_water_recovery_goal(WrsGoal(**payload))
        elif kind == "request_co2":
            result = backend.request_co2(float(payload["amount"]))
        elif kind == "request_o2":
            result = backend.request_o2(float(payload["amount"]))
        elif kind == "set_subsystem_failure":
            backend.set_subsystem_failure(str(payload["subsystem"]), bool(payload["enabled"]))
            return {
                "kind": "/eclss/events/operational_applied",
                "command": cmd.to_dict(),
                "message": f"failure flag {payload['subsystem']}={payload['enabled']}",
            }
        else:
            return {
                "kind": "/eclss/events/operational_rejected",
                "command": cmd.to_dict(),
                "message": f"unsupported command kind: {kind}",
            }

        success = bool(getattr(result, "success", False))
        event_kind = (
            "/eclss/events/operational_applied"
            if success
            else "/eclss/events/operational_rejected"
        )
        return {
            "kind": event_kind,
            "command": cmd.to_dict(),
            "result": result.to_dict(),
            "message": getattr(result, "summary_message", None) or getattr(result, "message", ""),
        }

    @staticmethod
    def _rule_metadata() -> Dict[str, Any]:
        return {"decision_source": "rule"}

    @staticmethod
    def _build_llm_client(llm_cfg: Dict[str, Any]) -> LLMClient:
        return build_llm_client(llm_cfg)


_ECLSS_OPERATIONAL_LEVERS = """\
### Operational levers (facility reference)
- air_revitalisation: ARS action — payload fields initial_co2_mass (kg),
  initial_moisture_content (percent 0–100), initial_contaminants (percent 0–100).
- oxygen_generation: OGS action — payload fields input_water_mass (kg),
  iodine_concentration (mg/L).
- request_co2: Service call — payload {"amount": <kg>} optional Sabatier feedstock;
  default policy leaves this to OGS-internal /ars/request_co2 (use only when
  request_co2_before_ogs is explicitly enabled or discourse justifies it).
- request_o2: Service call — payload {"amount": <kg>} withdraw O2 from plant /o2_storage reserve.
Actions are asynchronous; issue only commands justified by Telemetry and team discourse."""


def build_llm_situation(obs: EclssLoopObservation) -> str:
    t = obs.telemetry
    telemetry = (
        f"step={obs.step}, co2_storage_kg={t.co2_storage_kg}, o2_storage_kg={t.o2_storage_kg}, "
        f"product_water_reserve_l={t.product_water_reserve_l}, "
        f"grey_water_collected_l={t.grey_water_collected_l}, "
        f"ars_failure_enabled={t.ars_failure_enabled}, "
        f"ogs_failure_enabled={t.ogs_failure_enabled}, wrs_failure_enabled={t.wrs_failure_enabled}"
    )
    health = obs.health if isinstance(obs.health, dict) else {}
    world_state = (
        f"overall={health.get('overall', 'unknown')}, "
        f"co2_status={health.get('co2_status', 'unknown')}, "
        f"o2_status={health.get('o2_status', 'unknown')}, "
        f"water_status={health.get('water_status', 'unknown')}\n"
        "(Descriptive assessment from the facility monitoring layer — not a command.)"
    )
    return (
        "Scenario: ssos_eclss_loop. SSOS ECLSS storage and subsystem ops.\n\n"
        f"### Telemetry\n{telemetry}\n\n"
        f"### World state\n{world_state}\n\n"
        f"{_ECLSS_OPERATIONAL_LEVERS}"
    )


def build_llm_post_run_situation(
    summary: Dict[str, Any],
    discourse: List[AgentMessage],
    baseline_graph: Dict[str, Any],
) -> str:
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
    final_health = summary.get("final_health") or {}
    world_state = json.dumps(final_health, ensure_ascii=False)
    discourse_lines = "\n".join(
        f"- {msg.from_role}: {msg.message}" for msg in discourse[-8:]
    ) or "(none)"
    graph = json.dumps(baseline_graph, ensure_ascii=False)
    return (
        "Post-run SSOS graph design review. Simulation complete.\n\n"
        f"### Telemetry\n{telemetry_summary}\n\n"
        f"### World state\n{world_state}\n\n"
        f"### Team discourse (recent)\n{discourse_lines}\n\n"
        f"Baseline ssos_graph at run end: {graph}"
    )
