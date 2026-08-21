"""Deterministic gate between an agent's operational command and the backend.

Why this is separate from the agent
-----------------------------------
An agent proposes; this decides. Nothing here consults an LLM, and nothing here
reads agent-authored configuration, so no amount of deliberation can talk a
command past it. Keeping it in its own module rather than inside the team class
is the point: it is meant to be the part of the loop that agents cannot reach.

What this gate does NOT do, and why
-----------------------------------
It does not check resource availability. An earlier version did, and it was
wrong on every count: the plant model saturates each request rather than
failing it —

    run_ogs:     processed  = min(requested, available, capacity)
    run_wrs:     urine_feed = min(requested, urine_buffer, capacity)
    request_co2: granted    = min(captured_co2, amount)
    request_o2:  granted    = min(available_o2, amount)

Asking for more than is on hand is how a caller says "give me what you can",
not an error. Refusing those commands blocked 45 legitimate water-recovery
operations in a 50-step run and dropped the safety score from 0.26 to 0.14 —
the gate became the reason the run failed. It also compared urine demand
against grey water, two separate buffers that the model feeds independently.

Nor does it forbid the same command twice in one step: ``max_actions_per_step``
exists precisely so several representatives can act, and the scenario is tuned
for it ("Two OGS reps draw 0.15 L each").

So this is a **structural** gate, not a resource manager. It rejects commands
that are malformed, out of an agent's authority, or numerically corrupting —
things the plant cannot sensibly absorb — and leaves scarcity to the physics,
which already models it correctly.

Rejections carry a rule id and a readable reason so the caller can record *why*
a command was refused; a rejection whose cause is discarded teaches the next
attempt nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

# Commands an operator may issue. Deliberately not a superset of what the
# backend can execute: `set_subsystem_failure` reaches the backend but belongs
# to the scenario, and an agent that could toggle its own fault injection is
# grading its own exam.
OPERATIONAL_KINDS = frozenset({
    "air_revitalisation",
    "oxygen_generation",
    "water_recovery",
    "request_co2",
    "request_o2",
})

# Reachable from the backend but never from an agent.
SCENARIO_CONTROL_KINDS = frozenset({"set_subsystem_failure"})

FIELDS_BY_KIND: Dict[str, frozenset] = {
    "air_revitalisation": frozenset({
        "initial_co2_mass", "initial_moisture_content", "initial_contaminants",
    }),
    "oxygen_generation": frozenset({"input_water_mass", "iodine_concentration"}),
    "water_recovery": frozenset({"urine_volume"}),
    "request_co2": frozenset({"amount"}),
    "request_o2": frozenset({"amount"}),
}

# Upper bounds only where the unit convention fixes one (units.py documents
# moisture and contaminants as percent, 0-100). Everywhere else there is no
# ceiling here on purpose: the plant already limits by capacity, and a number
# invented in this file would override the model with a guess.
_PERCENT_FIELDS = frozenset({"initial_moisture_content", "initial_contaminants"})

# Fields whose value must be strictly positive to mean anything. The rest need
# only be non-negative — the backends assert ">= 0" and treat 0 as a no-op.
_STRICTLY_POSITIVE = frozenset({"input_water_mass", "urine_volume", "amount"})


@dataclass(frozen=True)
class Rejection:
    rule_id: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return {"rule_id": self.rule_id, "reason": self.reason}


@dataclass(frozen=True)
class AdmissibilityVerdict:
    admissible: bool
    rejections: Tuple[Rejection, ...] = ()

    @property
    def rule_ids(self) -> Tuple[str, ...]:
        return tuple(r.rule_id for r in self.rejections)

    @property
    def summary(self) -> str:
        return "; ".join(r.reason for r in self.rejections)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "admissible": self.admissible,
            "rejections": [r.to_dict() for r in self.rejections],
        }


_ADMISSIBLE = AdmissibilityVerdict(admissible=True)


def _reject(*rejections: Rejection) -> AdmissibilityVerdict:
    return AdmissibilityVerdict(admissible=False, rejections=tuple(rejections))


def _check_fields(kind: str, payload: Mapping[str, Any]) -> Tuple[Rejection, ...]:
    allowed = FIELDS_BY_KIND[kind]
    found: list[Rejection] = []

    unknown = sorted(set(payload) - allowed)
    if unknown:
        # Silently ignoring these is worse than refusing: the command would be
        # applied minus the part the agent believed it was asking for.
        found.append(Rejection(
            "FIELD_UNKNOWN",
            f"{kind}: unsupported field(s) {unknown}; allowed: {sorted(allowed)}",
        ))

    for name in sorted(set(payload) & allowed):
        raw = payload[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            found.append(Rejection("FIELD_NOT_NUMERIC", f"{kind}.{name} is not numeric: {raw!r}"))
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            found.append(Rejection("FIELD_NOT_NUMERIC", f"{kind}.{name} is not numeric: {raw!r}"))
            continue
        if not math.isfinite(value):
            # NaN/inf would propagate silently through the mass balance and
            # poison every downstream reading.
            found.append(Rejection("FIELD_NOT_FINITE", f"{kind}.{name} must be finite, got {value}"))
            continue
        if value < 0.0:
            found.append(Rejection(
                "FIELD_NEGATIVE", f"{kind}.{name}={value} must be non-negative",
            ))
            continue
        if name in _STRICTLY_POSITIVE and value == 0.0:
            found.append(Rejection(
                "FIELD_NOT_POSITIVE", f"{kind}.{name} must be greater than 0",
            ))
            continue
        if name in _PERCENT_FIELDS and value > 100.0:
            found.append(Rejection(
                "FIELD_OUT_OF_RANGE",
                f"{kind}.{name}={value} is a percentage and must be within [0, 100]",
            ))
    return tuple(found)


def is_command_admissible(
    kind: str,
    payload: Optional[Mapping[str, Any]] = None,
    **_ignored: Any,
) -> AdmissibilityVerdict:
    """Decide whether an operational command may reach the backend.

    Structural only — see the module docstring for why scarcity is left to the
    physics. Extra keyword arguments are accepted and ignored so a caller that
    still passes telemetry does not break.
    """
    if kind in SCENARIO_CONTROL_KINDS:
        return _reject(Rejection(
            "KIND_NOT_OPERATIONAL",
            f"{kind} controls the scenario, not the plant; an operator may not issue it",
        ))
    if kind not in OPERATIONAL_KINDS:
        return _reject(Rejection(
            "KIND_UNKNOWN",
            f"unsupported operational kind: {kind!r}; allowed: {sorted(OPERATIONAL_KINDS)}",
        ))
    if payload is not None and not isinstance(payload, Mapping):
        return _reject(Rejection("PAYLOAD_NOT_OBJECT", f"{kind}: payload must be an object"))
    if not payload:
        return _reject(Rejection("PAYLOAD_EMPTY", f"{kind}: payload is empty"))

    rejections = _check_fields(kind, payload)
    return _reject(*rejections) if rejections else _ADMISSIBLE
