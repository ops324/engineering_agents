"""Shared types for agent messages, observations, and step outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from environment.protocol import HealthMetrics, RecoveryCommand, TelemetrySnapshot


class DeliberationPhase:
    DELIBERATION = "deliberation"
    # F5. Named critique, between the independent round and the action that
    # integrates it. Its own phase so the artifact can tell a critique from a
    # comment without parsing text.
    CRITIQUE = "critique"
    ACTION = "action"
    POST_RUN = "post_run_proposal"


@dataclass
class AgentMessage:
    step: int
    from_role: str
    to_role: str
    message: str
    message_type: str
    reasoning: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "step": self.step,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "message": self.message,
            "message_type": self.message_type,
            "reasoning": self.reasoning,
        }
        payload.update(self.metadata)
        return payload


@dataclass
class StepAgentOutcome:
    messages: List[AgentMessage] = field(default_factory=list)
    commands: List[RecoveryCommand] = field(default_factory=list)


@dataclass
class AgentObservation:
    step: int
    telemetry: TelemetrySnapshot
    health: HealthMetrics
