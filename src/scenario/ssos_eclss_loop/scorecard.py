"""The project's scorecard, as far as it is defined.

`20260823_ECLSS_SIM_評価・検証スコアカード案` fixes what "better" means for
this simulator: a physics gate that is a precondition rather than an axis, an
occupant-survival axis worth 50, environment and margin axes worth 20 each, and
judgement and response axes worth 5 each when an actor was operating.

It defines a formula for exactly one of them:

    actor残存 = 50 × actor_remaining ÷ actor_initial

For A through D it names the quantities -- exposure integral, deficit integral,
longest critical streak, terminal margin, response latency, requested/processed
ratio -- and does not say how many points a given exposure costs. **This module
computes the quantities and refuses to invent the conversion.** Points come back
None with the reason attached, and the total is None while any applicable axis
is unscored.

That refusal is the point. Inventing a curve here would define "better" in a
scoring module rather than in the document the team agreed, and every later
comparison would inherit it silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from scenario.ssos_eclss_loop.physics_gate import evaluate_physics_gate, gate_passed
from scenario.ssos_eclss_loop.reference_limits import Habitat
from scenario.ssos_eclss_loop.survival import resolve_survival_bands
from scenario.ssos_eclss_loop.trajectory_metrics import (
    NotScorable,
    Yardstick,
    from_frozen_baseline,
    from_reference_limits,
    inventory_metrics,
    trajectory_metrics,
)

SCHEMA_VERSION = "0.1.0"

#: Axes whose point formula the scorecard does not state.
_UNDEFINED = (
    "the scorecard names the quantities for this axis but no point formula; "
    "converting them here would define the criterion in code rather than in "
    "the document"
)

ACTOR_MODES_WITH_OPERATIONS = frozenset({"labeled_rule_base", "llm"})


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _longest_streak(values: Sequence[str], target: str) -> int:
    longest = current = 0
    for value in values:
        current = current + 1 if value == target else 0
        longest = max(longest, current)
    return longest


def _dwell(health_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Steps spent in each band, one reading per step.

    Health is written twice on steps where operations ran, so rows are collapsed
    by step and the last reading kept -- the one attrition was judged on.
    """
    by_step: Dict[Any, Dict[str, Any]] = {}
    for row in health_rows:
        by_step[row.get("step")] = row
    ordered = [by_step[step] for step in sorted(by_step, key=lambda s: (s is None, s))]
    axes = ("overall", "co2_status", "o2_status", "water_status")
    dwell: Dict[str, Any] = {"steps": len(ordered)}
    for axis in axes:
        series = [str(row.get(axis) or "unknown") for row in ordered]
        dwell[axis] = {
            "safe": series.count("safe"),
            "warning": series.count("warning"),
            "critical": series.count("critical"),
            "longest_critical_streak": _longest_streak(series, "critical"),
        }
    return dwell


def _response_latency(
    health_rows: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Steps from first leaving safe on an axis to the first command for it.

    None when the axis never left safe (nothing to respond to) or when no
    command for it was ever applied (no response at all, which is not latency
    zero and must not be scored as though it were).
    """
    subsystem_for = {
        "co2_status": "air_revitalisation",
        "o2_status": "oxygen_generation",
        "water_status": "water_recovery",
    }
    by_step: Dict[Any, Dict[str, Any]] = {}
    for row in health_rows:
        by_step.setdefault(row.get("step"), row)
    first_alarm: Dict[str, Optional[int]] = {}
    for axis in subsystem_for:
        steps = [s for s in sorted(by_step, key=lambda s: (s is None, s))
                 if str(by_step[s].get(axis)) in {"warning", "critical"}]
        first_alarm[axis] = steps[0] if steps else None
    first_command: Dict[str, Optional[int]] = {}
    for event in events:
        if event.get("kind") != "/eclss/events/operational_applied":
            continue
        kind = str((event.get("command") or {}).get("kind") or "")
        if kind not in first_command or first_command[kind] is None:
            first_command.setdefault(kind, event.get("step"))
    out: Dict[str, Any] = {}
    for axis, subsystem in subsystem_for.items():
        alarm, acted = first_alarm[axis], first_command.get(subsystem)
        if alarm is None:
            out[subsystem] = {"latency_steps": None, "reason": "axis never left safe"}
        elif acted is None:
            out[subsystem] = {"latency_steps": None, "reason": "no such command was ever applied",
                              "first_alarm_step": alarm}
        else:
            out[subsystem] = {"latency_steps": max(0, int(acted) - int(alarm)),
                              "first_alarm_step": alarm, "first_command_step": acted}
    return out


_PROCESSED_FIELDS = {
    "air_revitalisation": ("initial_co2_mass", "co2_removed_kg"),
    "oxygen_generation": ("input_water_mass", "processed_water_kg"),
    "water_recovery": ("urine_volume", "urine_feed_l"),
}


def _requested_processed(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How much of what was asked for the plant actually turned into work."""
    totals: Dict[str, Dict[str, float]] = {}
    for event in events:
        if event.get("kind") != "/eclss/events/operational_applied":
            continue
        command = event.get("command") or {}
        kind = str(command.get("kind") or "")
        if kind not in _PROCESSED_FIELDS:
            continue
        asked_field, done_field = _PROCESSED_FIELDS[kind]
        asked = (command.get("payload") or {}).get(asked_field)
        done = ((event.get("result") or {}).get("details") or {}).get(done_field)
        if asked is None or done is None:
            continue
        bucket = totals.setdefault(kind, {"requested": 0.0, "processed": 0.0, "n": 0})
        bucket["requested"] += float(asked)
        bucket["processed"] += float(done)
        bucket["n"] += 1
    for bucket in totals.values():
        bucket["ratio"] = (
            round(bucket["processed"] / bucket["requested"], 6) if bucket["requested"] else None
        )
        bucket["requested"] = round(bucket["requested"], 6)
        bucket["processed"] = round(bucket["processed"], 6)
    return totals


def score_run(run_dir: Path, *, habitat: Optional[Habitat] = None) -> Dict[str, Any]:
    """Scorecard outputs for one run. Points only where a formula exists."""
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    gate = evaluate_physics_gate(run_dir)
    passed = gate_passed(gate)

    bands = summary.get("survival_bands") or resolve_survival_bands(
        None, summary.get("thresholds") or {}
    )
    yardstick: Yardstick = (
        from_reference_limits(habitat)
        if habitat is not None
        else from_frozen_baseline(summary.get("thresholds") or {}, baseline_run_id=run_dir.name)
    )

    trajectory: Optional[Dict[str, Any]] = None
    inventory: Optional[Dict[str, Any]] = None
    if passed:
        try:
            trajectory = trajectory_metrics(run_dir, yardstick, require_gate=False)
            inventory = inventory_metrics(run_dir, bands)
        except NotScorable:
            trajectory = inventory = None

    health_rows = _read_jsonl(run_dir / "health_metrics.jsonl")
    events = _read_jsonl(run_dir / "events.jsonl")
    actor_mode = str(summary.get("agents_mode") or "none")
    operations_apply = actor_mode in ACTOR_MODES_WITH_OPERATIONS

    initial = summary.get("crew_initial")
    remaining = summary.get("crew_remaining")
    survival_points = (
        round(50.0 * float(remaining) / float(initial), 4)
        if passed and initial not in (None, 0) and remaining is not None
        else None
    )

    axes: Dict[str, Any] = {
        "actor_remaining": {
            "max": 50,
            "points": survival_points,
            "formula": "50 × actor_remaining ÷ actor_initial",
            "actor_initial": initial,
            "actor_remaining": remaining,
            "actor_lost_by_cause": summary.get("crew_lost_by_cause"),
        },
        "A_environment": {
            "max": 20,
            "points": None,
            "undefined_reason": _UNDEFINED,
            "co2_exposure_integral": (
                {name: stats["exposure_integral_kg_steps"]
                 for name, stats in trajectory["co2"]["bands"].items()} if trajectory else None
            ),
            "o2_deficit_integral": inventory["o2"]["deficit_integral"] if inventory else None,
            "water_deficit_integral": inventory["water"]["deficit_integral"] if inventory else None,
            "dwell": _dwell(health_rows),
        },
        "B_margin": {
            "max": 20,
            "points": None,
            "undefined_reason": _UNDEFINED,
            "co2": {"peak_kg": trajectory["co2"]["peak_kg"] if trajectory else None,
                    "terminal_kg": trajectory["co2"]["terminal_kg"] if trajectory else None,
                    "terminal_margin_kg": (
                        {name: stats["terminal_margin_kg"]
                         for name, stats in trajectory["co2"]["bands"].items()} if trajectory else None)},
            "o2": {"min_kg": inventory["o2"]["min_kg"] if inventory else None,
                   "terminal_kg": inventory["o2"]["terminal_kg"] if inventory else None,
                   "band_low_kg": inventory["o2"]["band_low_kg"] if inventory else None},
            "water": {"min_l": inventory["water"]["min_l"] if inventory else None,
                      "terminal_l": inventory["water"]["terminal_l"] if inventory else None,
                      "band_low_l": inventory["water"]["band_low_l"] if inventory else None},
            "terminal_health": summary.get("final_health"),
        },
        "C_judgement": {
            "max": 5,
            "points": None,
            "applicable": operations_apply,
            "undefined_reason": _UNDEFINED,
            "response_latency_steps": _response_latency(health_rows, events) if operations_apply else None,
            "commands": summary.get("commands"),
        },
        "D_response": {
            "max": 5,
            "points": None,
            "applicable": operations_apply,
            "undefined_reason": _UNDEFINED,
            "requested_processed_ratio": _requested_processed(events) if operations_apply else None,
        },
    }

    applicable_max = 50 + 20 + 20 + (10 if operations_apply else 0)
    unscored = [name for name, axis in axes.items()
                if axis["points"] is None and axis.get("applicable", True)]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "actor_mode": actor_mode,
        "physics_gate": {
            "verdict": gate["verdict"],
            "form": gate["form"],
            "failed_checks": gate["failed_checks"],
        },
        # "物理ゲート不合格のランは採点せず、検証無効とする"
        "scorable": passed,
        "axes": axes,
        "total": {
            "points": None,
            "applicable_max": applicable_max,
            "unscored_axes": unscored,
            "note": (
                "physics gate failed; this run is not evidence"
                if not passed
                else "A–D carry no point formula in the scorecard, so no total is computed"
            ),
        },
    }


__all__ = ["SCHEMA_VERSION", "score_run"]
