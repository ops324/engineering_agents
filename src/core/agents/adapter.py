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

from core.agents.persona import ARCHETYPE_LENSES

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
        note="thinking lenses, dealt round-robin over the crew; [] is a homogeneous team",
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
