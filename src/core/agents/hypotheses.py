"""Falsifiable hypothesis memory — design.md 6.

The registered requirement is what fixes the shape here: 6.2 says a prediction
is checked against measurement *deterministically*, and that the model is not
the judge. Free text cannot be scored that way, so a hypothesis is a pair of
predicate lists over telemetry the backend actually reports. That is a
consequence of the requirement, not a choice made around it.

What separates this from retrieval (F3's other level, in memory.py): entries
there are ranked by how well they match the situation and nothing scores them.
Here the memory makes a prediction, the measurement grades it, and the grade
changes what surfaces next time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# The closed whitelist of decision 45. A metric outside it cannot be read off
# the observation, so a hypothesis naming one could never be scored — it is
# rejected at intake rather than kept as an entry that quietly never counts.
NUMERIC_METRICS = frozenset(
    {
        "co2_storage_kg",
        "o2_storage_kg",
        "product_water_reserve_l",
        "grey_water_collected_l",
    }
)
BOOLEAN_METRICS = frozenset(
    {"ars_failure_enabled", "ogs_failure_enabled", "wrs_failure_enabled"}
)
STATUS_METRICS = frozenset({"overall", "co2_status", "o2_status", "water_status"})
KNOWN_METRICS = NUMERIC_METRICS | BOOLEAN_METRICS | STATUS_METRICS

NUMERIC_OPS = frozenset({">", ">=", "<", "<=", "==", "!="})
CATEGORICAL_OPS = frozenset({"==", "!="})

MIN_HORIZON = 1
MAX_HORIZON = 5
# Refuted at three, and only while refutations outnumber support. A hypothesis
# that is right more often than it is wrong is not refuted by its exceptions.
REFUTE_THRESHOLD = 3


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    return left <= right


@dataclass(frozen=True)
class Predicate:
    metric: str
    op: str
    value: Any

    def holds(self, reading: Dict[str, Any]) -> Optional[bool]:
        """True/False, or None when the metric is not in this reading.

        None is not False. A predicate that could not be evaluated must not be
        scored as a failed prediction — that would credit refutations to
        measurements that never happened.
        """
        if self.metric not in reading:
            return None
        observed = reading[self.metric]
        if observed is None:
            return None
        if self.metric in NUMERIC_METRICS:
            try:
                return _compare(float(observed), self.op, float(self.value))
            except (TypeError, ValueError):
                return None
        if self.metric in BOOLEAN_METRICS:
            return _compare(bool(observed), self.op, bool(self.value))
        return _compare(str(observed).lower(), self.op, str(self.value).lower())

    def as_dict(self) -> Dict[str, Any]:
        return {"metric": self.metric, "op": self.op, "value": self.value}

    def describe(self) -> str:
        return f"{self.metric} {self.op} {self.value}"


def parse_predicate(raw: Any) -> Tuple[Optional[Predicate], Optional[str]]:
    if not isinstance(raw, dict):
        return None, f"predicate must be an object, got {type(raw).__name__}"
    metric = str(raw.get("metric", "")).strip()
    if metric not in KNOWN_METRICS:
        return None, f"unknown metric {metric!r}"
    op = str(raw.get("op", "")).strip()
    allowed = NUMERIC_OPS if metric in NUMERIC_METRICS else CATEGORICAL_OPS
    if op not in allowed:
        return None, f"operator {op!r} not allowed for {metric}"
    if "value" not in raw:
        return None, f"{metric}: no value"
    value = raw["value"]
    if metric in NUMERIC_METRICS:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None, f"{metric}: value {value!r} is not a number"
    elif metric in BOOLEAN_METRICS:
        if not isinstance(value, bool):
            return None, f"{metric}: value must be true or false"
    else:
        value = str(value).strip().lower()
        if not value:
            return None, f"{metric}: empty value"
    return Predicate(metric=metric, op=op, value=value), None


@dataclass
class Hypothesis:
    id: str
    condition: Tuple[Predicate, ...]
    prediction: Tuple[Predicate, ...]
    horizon: int
    origin_step: int
    origin_agent: str
    support: int = 0
    refute: int = 0
    status: str = "active"
    # Steps at which this hypothesis' condition fired and its prediction is
    # still to be checked. Never scored twice for one firing.
    pending: List[int] = field(default_factory=list)

    def score(self) -> int:
        return self.support - self.refute

    def describe(self) -> str:
        when = " and ".join(p.describe() for p in self.condition)
        then = " and ".join(p.describe() for p in self.prediction)
        mark = "" if self.status == "active" else f" [{self.status.upper()}]"
        return (
            f"{self.id}{mark}: when {when}, then within {self.horizon} step(s) {then} "
            f"(support {self.support}, refuted {self.refute})"
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "condition": [p.as_dict() for p in self.condition],
            "prediction": [p.as_dict() for p in self.prediction],
            "horizon": self.horizon,
            "origin_step": self.origin_step,
            "origin_agent": self.origin_agent,
            "support": self.support,
            "refute": self.refute,
            "status": self.status,
        }


def _parse_side(raw: Any, label: str) -> Tuple[Tuple[Predicate, ...], List[str]]:
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return (), [f"{label} must be a non-empty list of predicates"]
    predicates: List[Predicate] = []
    notes: List[str] = []
    for item in raw:
        predicate, note = parse_predicate(item)
        if predicate is None:
            notes.append(f"{label}: {note}")
        else:
            predicates.append(predicate)
    if notes:
        # All or nothing. A hypothesis missing half its condition is a different
        # claim from the one the agent made, and scoring it would attribute a
        # verdict to a prediction nobody offered.
        return (), notes
    return tuple(predicates), []


@dataclass
class HypothesisStore:
    """The team's ledger. Shared, not per-agent (decision 49)."""

    hypotheses: List[Hypothesis] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    # Every scoring event, so a run can be asked whether the loop actually
    # turned rather than only what it ended up believing.
    scoring_events: List[Dict[str, Any]] = field(default_factory=list)
    _next_id: int = 1

    def _refuse(self, raw: Any, reasons: List[str], *, step: int, agent_id: str) -> None:
        """Record the offer and why it did not get in.

        The raw offer goes in whatever its shape. An earlier version kept it
        only for scalars, so the ninety-three refusals that mattered most were
        written as `null` and the reason had to carry the whole diagnosis.
        """
        self.rejected.append(
            {"step": step, "agent_id": agent_id, "reasons": reasons, "raw": raw}
        )

    def propose(self, raw: Any, *, step: int, agent_id: str) -> Optional[Hypothesis]:
        """Take an agent's hypothesis, or record why it was not taken."""
        # A one-element list is unwrapped. Replies put the hypothesis in a list
        # because the two fields inside it are lists, and reading `[{...}]` as
        # `{...}` changes nothing about the claim — it is the same single
        # hypothesis, which is what one turn offers. Longer lists are refused
        # rather than silently truncated: a turn offering three claims has not
        # made the one claim the contract asks for, and picking one for it would
        # be choosing on the agent's behalf. The raw offer is recorded either
        # way, so the artifact still shows exactly what came back.
        if isinstance(raw, list) and len(raw) == 1:
            raw = raw[0]
        if not isinstance(raw, dict):
            detail = (
                f"got a list of {len(raw)}; offer one hypothesis, not several"
                if isinstance(raw, list)
                else f"got {type(raw).__name__}"
            )
            self._refuse(raw, [f"hypothesis must be an object, {detail}"],
                         step=step, agent_id=agent_id)
            return None
        condition, condition_notes = _parse_side(raw.get("condition"), "condition")
        prediction, prediction_notes = _parse_side(raw.get("prediction"), "prediction")
        notes = condition_notes + prediction_notes
        horizon_raw = raw.get("horizon", 1)
        try:
            horizon = int(horizon_raw)
        except (TypeError, ValueError):
            horizon = -1
        if not MIN_HORIZON <= horizon <= MAX_HORIZON:
            notes.append(
                f"horizon must be an integer {MIN_HORIZON}..{MAX_HORIZON}, got {horizon_raw!r}"
            )
        if notes:
            self._refuse(raw, notes, step=step, agent_id=agent_id)
            return None
        # A hypothesis already in the ledger is not added twice; the ledger is
        # the team's, so two operators claiming the same thing is one claim with
        # one score, not two claims that each half-count.
        for existing in self.hypotheses:
            if (
                existing.condition == condition
                and existing.prediction == prediction
                and existing.horizon == horizon
            ):
                return existing
        hypothesis = Hypothesis(
            id=f"H-{self._next_id}",
            condition=condition,
            prediction=prediction,
            horizon=horizon,
            origin_step=step,
            origin_agent=agent_id,
        )
        self._next_id += 1
        self.hypotheses.append(hypothesis)
        return hypothesis

    @staticmethod
    def _all_hold(predicates: Iterable[Predicate], reading: Dict[str, Any]) -> Optional[bool]:
        verdicts = [p.holds(reading) for p in predicates]
        if any(v is None for v in verdicts):
            return None
        return all(verdicts)

    def observe(self, step: int, reading: Dict[str, Any]) -> None:
        """Score what is due at this step, then arm what fires at it.

        Order matters: scoring first means a horizon-0 re-firing cannot grade
        itself, and a hypothesis whose condition holds every step still gets one
        verdict per firing rather than a verdict per step of a firing.
        """
        for hypothesis in self.hypotheses:
            due = [fired for fired in hypothesis.pending if fired + hypothesis.horizon <= step]
            for fired in due:
                hypothesis.pending.remove(fired)
                verdict = self._all_hold(hypothesis.prediction, reading)
                if verdict is None:
                    # Unreadable measurement. Not a refutation — see Predicate.holds.
                    self.scoring_events.append(
                        {
                            "hypothesis_id": hypothesis.id,
                            "fired_step": fired,
                            "scored_step": step,
                            "verdict": "unscorable",
                        }
                    )
                    continue
                if verdict:
                    hypothesis.support += 1
                else:
                    hypothesis.refute += 1
                self.scoring_events.append(
                    {
                        "hypothesis_id": hypothesis.id,
                        "fired_step": fired,
                        "scored_step": step,
                        "verdict": "support" if verdict else "refute",
                    }
                )
                # Refuted, never deleted (design.md 6.3).
                if (
                    hypothesis.refute >= REFUTE_THRESHOLD
                    and hypothesis.refute > hypothesis.support
                ):
                    hypothesis.status = "refuted"
        for hypothesis in self.hypotheses:
            if self._all_hold(hypothesis.condition, reading):
                hypothesis.pending.append(step)

    def retrieve(self, reading: Dict[str, Any], limit: int = 5) -> List[Hypothesis]:
        """Hypotheses whose condition holds now, best-scoring first.

        Refuted ones are eligible and carry their status into `describe`. Hiding
        them would make design.md 6.4's question — do proposals that consulted a
        refuted hypothesis pass less often — unanswerable, because no proposal
        could ever have consulted one.
        """
        matching = [
            h for h in self.hypotheses if self._all_hold(h.condition, reading) is True
        ]
        matching.sort(key=lambda h: (-h.score(), h.id))
        return matching[:limit]

    def describe(self, reading: Dict[str, Any], limit: int = 5) -> str:
        matching = self.retrieve(reading, limit=limit)
        if not matching:
            return ""
        lines = "\n".join(f"- {h.describe()}" for h in matching)
        return (
            "### Hypotheses that apply to this reading\n"
            f"{lines}\n"
            "(The team's ledger. Support and refutation counts are measured, not "
            "asserted. A hypothesis marked REFUTED is kept on purpose — it records "
            "what did not hold.)"
        )

    def stats(self) -> Dict[str, Any]:
        scored = [e for e in self.scoring_events if e["verdict"] in ("support", "refute")]
        return {
            "count": len(self.hypotheses),
            "active": sum(1 for h in self.hypotheses if h.status == "active"),
            "refuted": sum(1 for h in self.hypotheses if h.status == "refuted"),
            "rejected_at_intake": len(self.rejected),
            "scoring_events": len(scored),
            "support_events": sum(1 for e in scored if e["verdict"] == "support"),
            "refute_events": sum(1 for e in scored if e["verdict"] == "refute"),
            "unscorable_events": len(self.scoring_events) - len(scored),
        }

    def write_jsonl(self, path: Path) -> None:
        """The ledger, and what was refused entry to it.

        Refusals are written for the same reason 6.3 keeps refuted hypotheses:
        the record of what did not hold is worth as much as the record of what
        did. Concretely — the first check run refused every offer, and the cause
        was only findable because a raw excerpt happened to reach far enough to
        show the malformed field. With the count alone, "the contract asked for
        the wrong shape" and "the model cannot form a hypothesis" look the same.
        """
        with Path(path).open("w", encoding="utf-8") as f:
            for hypothesis in self.hypotheses:
                f.write(json.dumps(
                    {"kind": "hypothesis", **hypothesis.as_dict()}, ensure_ascii=False
                ) + "\n")
            for refusal in self.rejected:
                f.write(json.dumps(
                    {"kind": "refused", **refusal}, ensure_ascii=False
                ) + "\n")
