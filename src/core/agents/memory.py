"""Team discourse buffer and per-agent private memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List

from core.agents.types import AgentMessage, StepAgentOutcome


# Words carried by every entry regardless of what happened — they would score
# each other's overlap without saying anything about relevance. Kept small and
# explicit rather than learned: a stop list derived from the corpus would make
# retrieval depend on run history, and then two runs with the same memory policy
# would not be running the same policy.
# F3's implemented levels. "hypothesis" (design.md 6) is registered and not
# built yet; it is absent here rather than silently aliased to retrieval.
MEMORY_POLICIES = frozenset({"none", "naive_retrieval"})

_RETRIEVAL_STOPWORDS = frozenset(
    """a an and are as at be by for from had has have in is it its of on or
    step that the their them then there these this to was were will with action
    payload value operator team""".split()
)


def _terms(text: str) -> set:
    """Content words, lowercased. Deterministic — no embeddings, no model."""
    out = set()
    for raw in re.split(r"[^0-9a-zA-Z_]+", text.lower()):
        if len(raw) > 2 and raw not in _RETRIEVAL_STOPWORDS:
            out.add(raw)
    return out


@dataclass
class AgentMemory:
    """One operator's private recall.

    F3's factor lives here. ``limit`` means different things at the two levels
    and that difference *is* the factor: with no retrieval policy it is what the
    agent keeps, so anything older falls out and recency decides everything;
    with one it is what the agent is *shown*, the entries stay, and relevance
    decides which of them surface. The registered baseline is the former.
    """

    agent_id: str
    limit: int = 8
    entries: List[str] = field(default_factory=list)
    # F3 level: "none" (recency window) or "naive_retrieval" (keep all, surface
    # by lexical overlap with the situation).
    policy: str = "none"

    def append(self, entry: str) -> None:
        text = entry.strip()
        if not text:
            return
        self.entries.append(text)
        # Retrieval cannot rank what was thrown away. Under a retrieval policy
        # the history is kept and `limit` bounds what is surfaced instead.
        if self.policy == "none" and len(self.entries) > self.limit:
            self.entries = self.entries[-self.limit :]

    def recent(self, n: int | None = None) -> List[str]:
        if n is None:
            return list(self.entries)
        return self.entries[-n:]

    def retrieve(self, query: str, n: int | None = None) -> List[str]:
        """The entries this agent is shown for `query`.

        Naive on purpose: overlap of content words, ties broken by recency. It
        is what "naive retrieval" names, it adds no model call, and it is
        reproducible — an embedding index would make the level depend on a
        second model whose version is not part of the design.
        """
        limit = self.limit if n is None else n
        if self.policy != "naive_retrieval":
            return self.recent(limit)
        if not self.entries:
            return []
        query_terms = _terms(query)
        if not query_terms:
            return self.recent(limit)
        scored = [
            (len(query_terms & _terms(entry)), index, entry)
            for index, entry in enumerate(self.entries)
        ]
        # Highest overlap first, then most recent. Entries that overlap nothing
        # are still eligible: dropping them would make an agent with no matching
        # memory see nothing at all, which is a third policy, not this one.
        scored.sort(key=lambda item: (-item[0], -item[1]))
        chosen = sorted(scored[:limit], key=lambda item: item[1])
        return [entry for _, _, entry in chosen]


@dataclass
class DiscourseBuffer:
    window: int = 12
    messages: List[AgentMessage] = field(default_factory=list)

    def extend(self, new_messages: Iterable[AgentMessage]) -> None:
        self.messages.extend(new_messages)
        if len(self.messages) > self.window:
            self.messages = self.messages[-self.window :]

    def recent(self) -> List[AgentMessage]:
        return list(self.messages)


@dataclass
class TeamMemoryStore:
    agent_ids: List[str]
    memory_limit: int = 8
    discourse_window: int = 12
    # F3. "none" is the registered baseline: a recency window, which is what a
    # memory with no policy is.
    memory_policy: str = "none"
    discourse: DiscourseBuffer = field(init=False)
    agent_memories: Dict[str, AgentMemory] = field(init=False)

    def __post_init__(self) -> None:
        if self.memory_policy not in MEMORY_POLICIES:
            raise ValueError(
                f"memory_policy must be one of {sorted(MEMORY_POLICIES)}, "
                f"got {self.memory_policy!r}"
            )
        self.discourse = DiscourseBuffer(window=self.discourse_window)
        self.agent_memories = {
            agent_id: AgentMemory(
                agent_id=agent_id, limit=self.memory_limit, policy=self.memory_policy
            )
            for agent_id in self.agent_ids
        }

    def commit_step(self, outcome: StepAgentOutcome) -> None:
        self.discourse.extend(outcome.messages)
        for msg in outcome.messages:
            memory = self.agent_memories.get(msg.from_role)
            if memory is None:
                continue
            llm_memory = msg.metadata.get("llm_memory")
            if llm_memory:
                memory.append(str(llm_memory))
            summary = f"step {msg.step} [{msg.metadata.get('deliberation_phase', '?')}]: {msg.message}"
            if msg.reasoning:
                summary += f" ({msg.reasoning})"
            memory.append(summary)
        for cmd in outcome.commands:
            issuer = getattr(cmd, "issued_by", None) or "operator"
            memory = self.agent_memories.get(issuer)
            if memory is not None:
                kind = getattr(cmd.kind, "value", cmd.kind)
                payload = getattr(cmd, "payload", None)
                if isinstance(payload, dict) and payload:
                    memory.append(f"step action: {kind} payload={payload}")
                elif hasattr(cmd, "value"):
                    memory.append(f"step action: {kind} value={cmd.value}")
                else:
                    memory.append(f"step action: {kind}")
