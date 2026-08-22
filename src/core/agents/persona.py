"""Persona-based prompt building and LLM deliberation turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, List, Optional, Sequence, Tuple, TypeVar

from core.agents.base import DeliberationContext, Persona
from core.agents.memory import AgentMemory
from core.agents.types import AgentMessage, DeliberationPhase
from core.llm.base import LLMClient
from core.llm.parsing import parse_json_response

TEAM_CHARTER = """You are on a closed-habitat ECLSS resilience team.
Your Persona is a professional lens — not a script. You may disagree with teammates, wait, or propose alternatives.
Ground claims in Telemetry (numbers) and World state (descriptive health). Normative safety judgment is yours as an ECLSS engineer — do not assume hidden facility thresholds.
Scenario specifics live only under ## Situation — not in your Persona."""

DEFAULT_TEAM_PERSONA = (
    "Closed-habitat ECLSS colleague engineer. Ground observations in Telemetry and World state; "
    "state hypotheses and recovery options from team discourse.\n"
    "Do not change topology during the simulation — structural changes are post-run recommendations only.\n"
    "Cite teammates by agent_id; agree or disagree explicitly."
)

# Archetype lenses — scenario-independent *ways of thinking*, not role scripts.
# Each lens text is composed on top of the shared team persona (see build_personas);
# it must never encode scenario names, thresholds, or a catalogue of fixed actions.
ARCHETYPE_LENSES: Dict[str, str] = {
    "first_principles": (
        "Thinking lens — First principles: reason from conservation laws and mass/energy "
        "balances. Reconstruct the numbers from the ground up and distrust figures that do "
        "not reconcile."
    ),
    "failure_mode": (
        "Thinking lens — Failure mode: think like FMEA. Hunt for what breaks next, secondary "
        "failures, and worst-case interactions before endorsing any course of action."
    ),
    "improviser": (
        "Thinking lens — Improviser: look for unexpected reuse of resources already on hand "
        "and the smallest intervention that gets meaningful effect."
    ),
    "systems_integrator": (
        "Thinking lens — Systems integrator: watch cross-subsystem coupling (e.g. power vs. "
        "life-support) and the side-effects a local fix imposes on the rest of the station."
    ),
}


DEFAULT_DESIGN_PERSONA = (
    "Post-run ECLSS design engineer. The simulation is over; you are reviewing what happened.\n"
    "Ground every claim in the run summary and the operators' discourse — not in what you "
    "expected to happen.\n"
    "Propose changes to the next run's design, not actions inside the finished run.\n"
    "Say plainly when the run gives you no evidence for a change in your own subsystem."
)

# Subsystem responsibilities for post-run design proposers.
#
# Distinct from ARCHETYPE_LENSES: archetypes are *ways of thinking* used by
# operators during a run; these name *which subsystem a proposer answers for*
# after the run. Only subsystems the EclssBackend can actually act on are
# listed — the scope deck also mentions temperature/humidity control, which has
# no backend counterpart yet and is therefore intentionally absent.
DESIGN_SUBSYSTEM_LENSES: Dict[str, str] = {
    "air_revitalisation": (
        "Subsystem responsibility — Air revitalisation (ARS): CO2 removal capacity, "
        "moisture and contaminant handling. Judge whether the run's CO2 trajectory was "
        "driven by capacity, by timing, or by an outage you cannot design around."
    ),
    "oxygen_generation": (
        "Subsystem responsibility — Oxygen generation (OGS): O2 production and the water "
        "it consumes. Watch the coupling: O2 margin bought with water may be paid for by "
        "the water subsystem."
    ),
    "water_recovery": (
        "Subsystem responsibility — Water recovery (WRS): reclaim throughput and reserve "
        "margin. You are downstream of everyone; say so when another subsystem's proposal "
        "would drain you."
    ),
    "fault_detection": (
        "Subsystem responsibility — Fault detection: how early and how reliably the team "
        "saw trouble. Judge detection latency and missed signals, not the recovery itself."
    ),
    "systems_integration": (
        "Subsystem responsibility — Systems integration: you own no subsystem. Judge "
        "whether the individual proposals are consistent with each other and with the "
        "resource budget, and name the conflicts nobody else will."
    ),
}


@dataclass(frozen=True)
class TeamConfig:
    count: int
    id_prefix: str
    shared_persona: str
    agent_ids: Tuple[str, ...]
    # (agent_id, lens_name) pairs. Empty tuple => homogeneous team (backward compatible).
    archetypes: Tuple[Tuple[str, str], ...] = ()

    def action_rep_index(self, step: int) -> int:
        return (step - 1) % self.count

    def action_rep_id(self, step: int) -> str:
        return self.agent_ids[self.action_rep_index(step)]


def _resolve_archetypes(
    raw: Any, agent_ids: Tuple[str, ...]
) -> Tuple[Tuple[str, str], ...]:
    """Map a list of lens names onto agent_ids (round-robin). Empty/missing => ()."""
    if not raw:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"team.archetypes must be a list of lens names, got {type(raw).__name__}"
        )
    lens_names = [str(name).strip() for name in raw if str(name).strip()]
    if not lens_names:
        return ()
    unknown = [name for name in lens_names if name not in ARCHETYPE_LENSES]
    if unknown:
        raise ValueError(
            f"Unknown archetype lens(es): {unknown}. "
            f"Known lenses: {sorted(ARCHETYPE_LENSES)}"
        )
    return tuple(
        (agent_id, lens_names[i % len(lens_names)])
        for i, agent_id in enumerate(agent_ids)
    )


def load_team(config: Dict[str, Any]) -> TeamConfig:
    team_raw = config.get("team") or {}
    count = max(1, int(team_raw.get("count", 4)))
    id_prefix = str(team_raw.get("id_prefix", "engineer"))
    persona_text = str(team_raw.get("persona") or DEFAULT_TEAM_PERSONA).strip()
    agent_ids = tuple(f"{id_prefix}_{i}" for i in range(1, count + 1))
    archetypes = _resolve_archetypes(team_raw.get("archetypes"), agent_ids)
    return TeamConfig(
        count=count,
        id_prefix=id_prefix,
        shared_persona=persona_text,
        agent_ids=agent_ids,
        archetypes=archetypes,
    )


@dataclass(frozen=True)
class DesignTeamConfig:
    """Post-run design proposers, separate from the in-run operator team."""

    id_prefix: str
    shared_persona: str
    agent_ids: Tuple[str, ...]
    # (agent_id, subsystem_name) pairs, one per agent.
    subsystems: Tuple[Tuple[str, str], ...]

    @property
    def count(self) -> int:
        return len(self.agent_ids)


def load_design_team(config: Dict[str, Any]) -> Optional[DesignTeamConfig]:
    """Read the optional ``design_team`` section.

    Returns None when absent or empty, which keeps the pre-separation behaviour
    (an operator issues the post-run proposal) intact.
    """
    raw = config.get("design_team") or {}
    if not raw:
        return None
    subsystems_raw = raw.get("subsystems")
    if not subsystems_raw:
        return None
    if not isinstance(subsystems_raw, (list, tuple)):
        raise ValueError(
            f"design_team.subsystems must be a list, got {type(subsystems_raw).__name__}"
        )
    names = [str(name).strip() for name in subsystems_raw if str(name).strip()]
    if not names:
        return None
    unknown = [name for name in names if name not in DESIGN_SUBSYSTEM_LENSES]
    if unknown:
        raise ValueError(
            f"Unknown design subsystem(s): {unknown}. "
            f"Known subsystems: {sorted(DESIGN_SUBSYSTEM_LENSES)}"
        )
    id_prefix = str(raw.get("id_prefix", "design"))
    persona_text = str(raw.get("persona") or DEFAULT_DESIGN_PERSONA).strip()
    agent_ids = tuple(f"{id_prefix}_{name}" for name in names)
    return DesignTeamConfig(
        id_prefix=id_prefix,
        shared_persona=persona_text,
        agent_ids=agent_ids,
        subsystems=tuple(zip(agent_ids, names)),
    )


def build_design_personas(design_team: DesignTeamConfig) -> Dict[str, Persona]:
    return {
        agent_id: Persona(
            agent_id=agent_id,
            persona=f"{DESIGN_SUBSYSTEM_LENSES[subsystem]}\n\n{design_team.shared_persona}",
        )
        for agent_id, subsystem in design_team.subsystems
    }


def build_personas(team: TeamConfig) -> Dict[str, Persona]:
    # Homogeneous fallback: identical to pre-archetype behaviour.
    if not team.archetypes:
        return {
            agent_id: Persona(agent_id=agent_id, persona=team.shared_persona)
            for agent_id in team.agent_ids
        }
    lens_by_agent = dict(team.archetypes)
    personas: Dict[str, Persona] = {}
    for agent_id in team.agent_ids:
        lens_name = lens_by_agent.get(agent_id)
        if lens_name:
            persona_text = f"{ARCHETYPE_LENSES[lens_name]}\n\n{team.shared_persona}"
        else:
            persona_text = team.shared_persona
        personas[agent_id] = Persona(agent_id=agent_id, persona=persona_text)
    return personas


@dataclass
class ParsedTurn:
    data: Dict[str, Any]
    status: str
    error: Optional[str] = None
    raw_excerpt: str = ""


MESSAGE_WORD_LIMIT = 60
REASONING_WORD_LIMIT = 80
MEMORY_WORD_LIMIT = 40


def json_envelope_preamble() -> str:
    return (
        "Return ONLY one valid JSON object (multi-line is allowed). "
        "No markdown. No code fences. No prose outside JSON. "
    )


def output_word_limits_clause() -> str:
    return (
        f'Keep "message" at most {MESSAGE_WORD_LIMIT} words and '
        f'"reasoning" at most {REASONING_WORD_LIMIT} words. '
        f'Optional "memory" at most {MEMORY_WORD_LIMIT} words.'
    )


def message_contract() -> str:
    return (
        f"{json_envelope_preamble()}"
        'Required keys: "message", "reasoning". '
        'Optional key: "memory". '
        f"{output_word_limits_clause()} "
        'Example: {"message":"CO2 rising.","reasoning":"co2_ppm crossed threshold",'
        '"memory":"Fan boost may be next."}'
    )


def operator_action_contract() -> str:
    return (
        f"{json_envelope_preamble()}"
        'Required keys: "message", "reasoning", "commands". '
        'Optional key: "memory". '
        f"{output_word_limits_clause()} "
        'commands must be a list of {"kind": "...", "value": ...} with kind in '
        '["set_fan_speed","enable_bypass","reduce_load","request_eps_boost"]. '
        "Empty commands when you and teammates agree to hold this step."
    )


def design_proposal_contract() -> str:
    return (
        f"{json_envelope_preamble()}"
        'Required keys: "message", "reasoning", "changes". '
        'Optional key: "memory". '
        f"{output_word_limits_clause()} "
        '"changes" is a list of {"change_kind","payload"} objects. '
        'change_kind in ["add_node","add_edge","set_parameter"]. '
        'add_node payload: {"id","name","kind"}. '
        'add_edge payload: {"node_a","node_b","kind"}. '
        'set_parameter payload: {"key","value"}. '
        "Proposals are post-run only — they will NOT be applied to the completed simulation."
    )


def eclss_operational_action_contract() -> str:
    return (
        f"{json_envelope_preamble()}"
        'Required keys: "message", "reasoning", "commands". '
        'Optional key: "memory". '
        f"{output_word_limits_clause()} "
        '"commands" is a list of {"kind","payload"} objects. '
        'kind in ["air_revitalisation","oxygen_generation","request_co2","request_o2"]. '
        'air_revitalisation payload fields: initial_co2_mass, initial_moisture_content, '
        'initial_contaminants (numeric). '
        'oxygen_generation payload fields: input_water_mass, iodine_concentration (numeric). '
        'request_co2 / request_o2 payload: {"amount": <kg>}. '
        "Empty commands when you and teammates agree to hold this step."
    )


def eclss_design_proposal_contract() -> str:
    return (
        f"{json_envelope_preamble()}"
        'Required keys: "message", "reasoning", "changes". '
        'Optional key: "memory". '
        f"{output_word_limits_clause()} "
        '"changes" is a list of {"change_kind","payload"} objects. '
        'change_kind in ["action_profile","service_config","set_parameter","graph_rewire"]. '
        'action_profile payload: {"subsystem":"ars|ogs|wrs","action":"...","fields":{...}}. '
        'service_config payload: {"service":"request_co2|request_o2", ...}. '
        'set_parameter payload: {"target":"dotted.config.path","value":...}. '
        "graph_rewire payload: ROS remapping manifest for the next launch. "
        "Proposals are post-run only — they will NOT be applied during this simulation."
    )


def critique_contract(target_role: str) -> str:
    """F5's middle round: one named colleague's reasoning, not the situation.

    Named rather than open so the round is a critique and not a second comment.
    An agent asked to "discuss" repeats itself; an agent asked what is wrong
    with a specific claim has to read it.
    """
    return (
        f"{json_envelope_preamble()}"
        'Required keys: "message". '
        f"{output_word_limits_clause()} "
        f"You are reviewing {target_role}'s reasoning for this step, not the telemetry. "
        "Name the specific assumption, omission or risk you disagree with, or say plainly "
        "that it holds and why. Do not restate your own plan and do not issue commands."
    )


def design_action_contract() -> str:
    """Deprecated alias — runtime design actions are no longer applied."""
    return design_proposal_contract()


def format_discourse(messages: List[AgentMessage]) -> str:
    if not messages:
        return "(none yet)"
    lines = []
    for msg in messages:
        phase = msg.metadata.get("deliberation_phase", "")
        prefix = f"[{phase}] " if phase else ""
        lines.append(f"- {prefix}{msg.from_role}: {msg.message}")
    return "\n".join(lines)


def format_memory(entries: List[str]) -> str:
    if not entries:
        return "(empty — first steps of the mission)"
    return "\n".join(f"- {entry}" for entry in entries)


class PersonaPromptBuilder:
    @staticmethod
    def build(
        persona: Persona,
        ctx: DeliberationContext,
        contract: str,
        action_hint: str,
        charter: str = TEAM_CHARTER,
    ) -> str:
        return (
            f"{charter}\n\n"
            f"agent_id: {persona.agent_id}\n"
            f"phase: {ctx.phase}\n\n"
            f"## How you think and act\n"
            f"{persona.persona}\n\n"
            f"## Situation\n"
            f"{ctx.situation}\n\n"
            f"## Team discourse (recent team messages)\n"
            f"{format_discourse(ctx.team_discourse)}\n\n"
            f"## Your memory (what you recall from prior steps)\n"
            f"{format_memory(ctx.agent_memory)}\n\n"
            f"## This step so far\n"
            f"{format_discourse(ctx.step_discourse)}\n\n"
            f"## Your task\n"
            f"{action_hint}\n\n"
            f"## Output contract\n"
            f"{contract}\n"
        )


class PersonaAgent:
    def __init__(
        self,
        persona: Persona,
        memory: AgentMemory,
        llm_client: LLMClient | None = None,
    ):
        self.persona = persona
        self.memory = memory
        self.llm_client = llm_client

    def build_context(
        self,
        *,
        step: int,
        phase: str,
        situation: str,
        step_discourse: List[AgentMessage],
        team_discourse: List[AgentMessage],
    ) -> DeliberationContext:
        return DeliberationContext(
            step=step,
            phase=phase,
            situation=situation,
            step_discourse=step_discourse,
            team_discourse=team_discourse,
            agent_memory=self.memory.recent(),
        )

    def deliberate(
        self,
        ctx: DeliberationContext,
        contract: str,
        action_hint: str,
        required: tuple[str, ...],
    ) -> Optional[ParsedTurn]:
        prompt = self._build_prompt(ctx, contract, action_hint)
        raw = self._generate(prompt)
        return self._parse_turn(raw, required)

    async def deliberate_async(
        self,
        ctx: DeliberationContext,
        contract: str,
        action_hint: str,
        required: tuple[str, ...],
    ) -> Optional[ParsedTurn]:
        prompt = self._build_prompt(ctx, contract, action_hint)
        raw = await self._generate_async(prompt)
        return self._parse_turn(raw, required)

    def _build_prompt(
        self,
        ctx: DeliberationContext,
        contract: str,
        action_hint: str,
    ) -> str:
        return PersonaPromptBuilder.build(
            self.persona,
            ctx,
            contract,
            action_hint,
        )

    def _generate(self, prompt: str) -> str:
        if self.llm_client is None:
            return ""
        return self.llm_client.generate(prompt)

    async def _generate_async(self, prompt: str) -> str:
        if self.llm_client is None:
            return ""
        generate_async = getattr(self.llm_client, "generate_async", None)
        if generate_async is not None:
            return await generate_async(prompt)
        return await asyncio.to_thread(self.llm_client.generate, prompt)

    @staticmethod
    def _parse_turn(raw: str, required: tuple[str, ...]) -> Optional[ParsedTurn]:
        parsed = parse_json_response(raw, required=required)
        if parsed.status in {"fallback", "empty_response"}:
            return None
        return ParsedTurn(
            data=parsed.data,
            status=parsed.status,
            error=parsed.error,
            raw_excerpt=parsed.raw_excerpt,
        )

    @staticmethod
    def phase_hint(phase: str) -> str:
        if phase == DeliberationPhase.DELIBERATION:
            return (
                "Deliberation: share observations and professional judgment. "
                "This round is simultaneous — you do not see other agents' comments "
                "from this step yet. React to Telemetry, World state, and prior-step "
                "teammates by agent_id."
            )
        if phase == DeliberationPhase.POST_RUN:
            return (
                "Post-run design review: simulation is complete. Propose structural changes as "
                "recommendations only — cite team discourse and run outcomes."
            )
        return PersonaAgent.action_round_hint()

    @staticmethod
    def design_round_hint(*, subsystem: str, n_proposers: int = 1) -> str:
        """Post-run hint for a subsystem design proposer (not an operator)."""
        base = (
            f"Post-run design review — you answer for {subsystem}. The simulation is "
            "complete; propose changes for the NEXT run, as recommendations only. "
            "Cite the run summary and named operators."
        )
        if n_proposers <= 1:
            return base
        return (
            f"{base} {n_proposers} subsystem proposers are writing simultaneously — you "
            "do not see their proposals. Stay inside your own subsystem, and say so "
            "explicitly when the evidence supports no change."
        )

    @staticmethod
    def action_round_hint(*, n_reps: int = 1, slot: int = 0) -> str:
        if n_reps <= 1:
            return (
                "Action round (team representative): issue recovery commands when discourse and "
                "Situation warrant intervention; cite named teammates from this step."
            )
        return (
            f"Action round: you are action representative {slot + 1} of {n_reps} this step. "
            "Issue operational commands when discourse and Situation warrant intervention; "
            "cite named teammates from this step. Empty commands if you hold. "
            "This action round is simultaneous — you do not see other representatives' "
            "commands from this step."
        )


_T = TypeVar("_T")


def run_parallel(awaitables: Sequence[Awaitable[_T]]) -> List[_T]:
    """Run awaitables concurrently and return results in the given order."""

    async def _gather() -> List[_T]:
        return list(await asyncio.gather(*awaitables))

    return asyncio.run(_gather())
