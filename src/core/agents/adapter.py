"""The self-modification surface — the adapter layer of design.md §4.

Everything a Meta agent is allowed to change lives here, and nothing else is
reachable from it. The point is structural rather than advisory: a proposal to
loosen a gate cannot be written, because there is no field to write it into.

    frozen (unreachable from this module)
      base agent inference, the deterministic gates (validity, mass
      conservation, command admissibility), and the evaluator

    adapter (this module)
      M  memory     what is kept and recalled
      R  retrieval  which past runs are consulted        -- not implemented
      X  checks     which checks run, in what order      -- not implemented
      S  stopping   when to abandon a proposal           -- not implemented
      C  configuration  team size, archetype allocation, discourse window

    proposable, but not adapters
      P0 physical parameters and P1 operating policy, which already have their
      own allow-listed surface in scenario/ssos_eclss_loop/design_proposals.py

R, X and S are named in the design and have no implementation in the engine
yet. They are **rejected** rather than accepted-and-ignored: a field that is
written, recorded, and then quietly does nothing is a factor that never
reaches the plant, and this experiment has already lost twelve runs to exactly
that. A knob appears here only once it moves something.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.agents.persona import (
    ARCHETYPE_LENSES,
    json_envelope_preamble,
    output_word_limits_clause,
)

# Schema version. Bumped when a field is added or its meaning changes, so a
# stored adapter cannot be silently reinterpreted by later code.
ADAPTER_SCHEMA_VERSION = 1

# The named surfaces of design.md §4 that have no implementation. Listed so the
# error message can say "not implemented yet" rather than "unknown field",
# which are different problems for whoever hits them.
UNIMPLEMENTED_SURFACES: Dict[str, str] = {
    "R": "retrieval policy (which past runs are consulted)",
    "X": "check invocation (which checks run, in what order)",
    "S": "stopping hook (when to abandon a proposal)",
}


@dataclass(frozen=True)
class FieldSpec:
    """One writable field: where it lands, and what it will accept."""

    surface: str                    # M / R / X / S / C
    path: Tuple[str, ...]           # location inside the agents config
    kind: str                       # "int" | "lens_list"
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    note: str = ""


# Bounds are not safety limits — the evaluator is frozen and scores whatever
# comes out. They keep a proposal inside the range the engine is known to run,
# so that a rejected update is a rejection rather than a crash mid-batch.
ADAPTER_FIELDS: Dict[str, FieldSpec] = {
    "team_count": FieldSpec(
        surface="C", path=("team", "count"), kind="int", minimum=1, maximum=20,
        note="operators in the crew",
    ),
    "archetypes": FieldSpec(
        surface="C", path=("team", "archetypes"), kind="lens_list",
        note=("thinking lenses, dealt round-robin over the crew, so repeats set the "
              "proportion; [] is a homogeneous team"),
    ),
    "discourse_window": FieldSpec(
        surface="C", path=("discourse_window",), kind="int", minimum=0, maximum=200,
        note="how much team discourse an operator sees",
    ),
    "memory_limit": FieldSpec(
        surface="M", path=("memory_limit",), kind="int", minimum=0, maximum=200,
        note="how many private entries an operator keeps",
    ),
}

# What F7=absent means, stated as values rather than as an absence. Equal to the
# shipped defaults, so a run with this adapter is the run that would have
# happened without one.
BASELINE_ADAPTER: Dict[str, Any] = {}


def _describe_fields() -> str:
    return ", ".join(f"{name} ({spec.surface})" for name, spec in sorted(ADAPTER_FIELDS.items()))


def validate_adapter(update: Any) -> List[str]:
    """Every reason this update is not writable. Empty list means it is.

    Unknown keys are errors, not warnings. An adapter that accepts a field it
    does not apply reports a change that never happened.
    """
    errors: List[str] = []
    if not isinstance(update, dict):
        return [f"adapter must be an object, got {type(update).__name__}"]

    version = update.get("schema_version", ADAPTER_SCHEMA_VERSION)
    if version != ADAPTER_SCHEMA_VERSION:
        errors.append(
            f"schema_version {version!r} != {ADAPTER_SCHEMA_VERSION}; "
            "a stored adapter from another version is not reinterpreted"
        )

    fields = update.get("fields", {})
    if not isinstance(fields, dict):
        return errors + [f"adapter.fields must be an object, got {type(fields).__name__}"]

    for name, value in fields.items():
        surface = str(name).split(".")[0].upper()
        if surface in UNIMPLEMENTED_SURFACES:
            errors.append(
                f"{name}: surface {surface} — {UNIMPLEMENTED_SURFACES[surface]} — is named in "
                "design.md 4 but has no implementation. It is rejected rather than stored, "
                "because a field that is recorded and does nothing is indistinguishable from "
                "a factor that never reached the plant."
            )
            continue
        spec = ADAPTER_FIELDS.get(name)
        if spec is None:
            errors.append(
                f"{name}: not an adapter field. Writable fields: {_describe_fields()}. "
                "Thresholds, gates, the evaluator and the operating policy are outside the "
                "adapter surface by construction (design.md 4)."
            )
            continue
        errors.extend(_validate_value(name, spec, value))
    return errors


def _validate_value(name: str, spec: FieldSpec, value: Any) -> List[str]:
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{name}: expected an integer, got {type(value).__name__}"]
        if spec.minimum is not None and value < spec.minimum:
            return [f"{name}: {value} is below the minimum {spec.minimum}"]
        if spec.maximum is not None and value > spec.maximum:
            return [f"{name}: {value} is above the maximum {spec.maximum}"]
        return []
    if spec.kind == "lens_list":
        if not isinstance(value, list):
            return [f"{name}: expected a list of lens names, got {type(value).__name__}"]
        unknown = [v for v in value if v not in ARCHETYPE_LENSES]
        if unknown:
            return [
                f"{name}: unknown lens(es) {unknown}. Known: {sorted(ARCHETYPE_LENSES)}"
            ]
        return []
    return [f"{name}: unsupported field kind {spec.kind!r}"]


def apply_adapter(agents_config: Dict[str, Any], update: Any) -> Dict[str, Any]:
    """Return a copy of the agents config with the adapter written into it.

    Raises on an invalid update rather than applying the writable part of it.
    A half-applied adapter is a configuration nobody proposed.
    """
    errors = validate_adapter(update)
    if errors:
        raise ValueError("invalid adapter update:\n  - " + "\n  - ".join(errors))

    merged = json.loads(json.dumps(agents_config))  # deep copy of plain data
    for name, value in (update.get("fields") or {}).items():
        spec = ADAPTER_FIELDS[name]
        target = merged
        for key in spec.path[:-1]:
            nxt = target.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                target[key] = nxt
            target = nxt
        target[spec.path[-1]] = value
    return merged


def adapter_provenance(update: Any) -> Dict[str, Any]:
    """What went into the run, for the summary. Never part of a score."""
    fields = (update or {}).get("fields") or {}
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "fields": dict(sorted(fields.items())),
        "surfaces_touched": sorted({ADAPTER_FIELDS[n].surface for n in fields if n in ADAPTER_FIELDS}),
        "self_modification": bool(fields),
    }


def load_adapter(path: Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_adapter(data)
    if errors:
        raise ValueError(f"invalid adapter at {path}:\n  - " + "\n  - ".join(errors))
    return data


def write_adapter(path: Path, update: Dict[str, Any]) -> None:
    errors = validate_adapter(update)
    if errors:
        raise ValueError("refusing to write an invalid adapter:\n  - " + "\n  - ".join(errors))
    payload = {"schema_version": ADAPTER_SCHEMA_VERSION, "fields": dict(update.get("fields") or {})}
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# The Meta agent's side of the surface
#
# design.md §4 is explicit that the Meta agent "only proposes an adapter
# update; acceptance is decided by the archive and the evaluation" (§5). So
# nothing here applies anything. It turns a reply into a candidate, and says
# what in that reply was not writable.
# ---------------------------------------------------------------------------

META_ADAPTER_PERSONA = (
    "Meta agent. The simulation is over. You do not operate the plant and you do not "
    "propose changes to it — other agents do that.\n"
    "You change how the crew itself is put together for the next run: how many operators "
    "there are, which thinking lenses they are given, how much of the team discourse each "
    "one sees, and how much each one privately remembers.\n"
    "Ground every change in what this run's evidence shows about the crew's behaviour — "
    "who acted, who repeated each other, what went unattended — not in what you expected.\n"
    "Propose nothing when the run gives you no evidence for a change. An unchanged "
    "configuration is a legitimate answer."
)


def meta_adapter_contract() -> str:
    """The reply contract, generated from the schema so it cannot drift from it."""
    lines = []
    for name, spec in sorted(ADAPTER_FIELDS.items()):
        if spec.kind == "int":
            bounds = f" ({spec.minimum}..{spec.maximum})" if spec.minimum is not None else ""
            lines.append(f'"{name}": integer{bounds} — {spec.note}')
        else:
            lines.append(
                f'"{name}": list of {sorted(ARCHETYPE_LENSES)} — {spec.note}. '
                "Repeating a name weights the allocation: with ten operators "
                '["first_principles","first_principles","failure_mode"] gives seven and '
                "three, not a third each. Say the proportion you want by how often you "
                "name each lens."
            )
    return (
        # The envelope preamble is what makes the reply parseable at all. Every
        # other contract opens with it; leaving it off produced a Meta agent
        # whose every reply failed to parse, which reads exactly like an agent
        # with nothing to say.
        f"{json_envelope_preamble()}"
        'Required keys: "message", "reasoning", "fields". '
        '"fields" is an object holding only these keys, and may be empty: '
        + "; ".join(lines)
        + f". {output_word_limits_clause()} "
        + "Nothing else is writable: thresholds, alarm bands, operating policy, the "
        "model, the safety gates and the evaluator are outside this surface and a key "
        "naming any of them is discarded. Propose the next run's crew, not this run's "
        "actions."
    )


def describe_current(state: Dict[str, Any]) -> str:
    """The configuration the Meta agent is being asked to revise.

    Without this the agent proposes absolute values for settings it cannot see.
    The 2026-08-22 pilot ran four generations that way and oscillated —
    150 -> 50 -> 150 -> 50 on the discourse window — with a fluent justification
    written for each direction. That is not adaptation; it is a prior being
    resampled. A self-modifying loop has to be able to observe the variable it
    is modifying.
    """
    lenses = state.get("archetypes") or []
    if lenses:
        counts: Dict[str, int] = {}
        for lens in lenses:
            counts[lens] = counts.get(lens, 0) + 1
        composition = ", ".join(f"{lens} x{n}" for lens, n in sorted(counts.items()))
    else:
        composition = "homogeneous (no lenses)"
    return (
        "### The crew that produced this run\n"
        f"team_count={state.get('team_count')}, "
        f"discourse_window={state.get('discourse_window')}, "
        f"memory_limit={state.get('memory_limit')}\n"
        f"lens composition: {composition}\n"
        "(These are the current values of the fields you may write. Propose absolute "
        "values, and say nothing about a field you would leave as it is.)"
    )


def partition_proposal(raw_fields: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Split a proposed reply into what is writable and what is not.

    Unlike :func:`apply_adapter`, which is all-or-nothing, this keeps the
    writable part and records the rest. The two rules are for two different
    acts: applying a configuration nobody proposed is a corruption, whereas a
    proposal is a candidate that something else decides on — and what a
    self-modifying system *tried* to reach for is evidence worth keeping rather
    than an error to swallow. Attempts on the frozen surface are counted, not
    hidden.
    """
    accepted: Dict[str, Any] = {}
    rejected: List[Dict[str, Any]] = []
    if not isinstance(raw_fields, dict):
        return accepted, [{
            "field": "(fields)",
            "value": raw_fields,
            "reason": f"expected an object, got {type(raw_fields).__name__}",
        }]
    for name, value in raw_fields.items():
        errors = validate_adapter({"fields": {name: value}})
        if errors:
            rejected.append({"field": str(name), "value": value, "reason": errors[0]})
        else:
            accepted[str(name)] = value
    return accepted, rejected


def proposal_provenance(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """The summary's view of a Meta agent proposal. Never part of a score."""
    fields = ((proposal or {}).get("adapter") or {}).get("fields") or {}
    rejected = (proposal or {}).get("rejected") or []
    return {
        "proposed_by": (proposal or {}).get("proposed_by"),
        "decision_source": (proposal or {}).get("decision_source"),
        "accepted_fields": sorted(fields),
        "rejected_fields": [r.get("field") for r in rejected],
        # How often the system reached for something it cannot have. The design
        # claims such a proposal is impossible to express; this counts the
        # attempts, which is the only way that claim becomes measurable.
        "frozen_surface_attempts": len(rejected),
        "proposes_change": bool(fields),
    }
