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

import yaml

from scenario.ssos_eclss_loop.physics_gate import evaluate_physics_gate, gate_passed
from scenario.ssos_eclss_loop.reference_limits import Habitat
from environment.ssos.eclss.plant_sim.stoichiometry import WATER_PER_O2
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

#: Where the point curves come from.
#:
#: The scorecard states one formula -- 50 × remaining ÷ initial -- and names the
#: quantities for A through D without saying what they are worth. The curves
#: below were chosen on this branch on 2026-08-26 and are **not** in the
#: document. Every scored artifact carries this string so a reader never mistakes
#: them for the team's.
POINTS_POLICY = "branch choice 2026-08-26: absolute anchors, no reference run"

#: An absolute scale means the anchors are published limits or the plant's own
#: constants, never another run. A run that scores near full on an axis is then
#: saying the habitat was genuinely not dangerous on it -- which is information,
#: not a broken curve. The first draft normalised CO2 exposure by the limit
#: itself and scored a run where the whole crew died at 17.4 of 20; anchoring on
#: the ladder's next rung instead is what makes the axis mean something.
_UNDEFINED = (
    "the scorecard names the quantities for this axis but no point formula; "
    "converting them here would define the criterion in code rather than in "
    "the document"
)


def _fraction(value: Optional[float], scale: float) -> Optional[float]:
    """value/scale clipped to [0, 1]; None survives as None."""
    if value is None or not scale:
        return None
    return max(0.0, min(1.0, float(value) / float(scale)))


def _points(remaining_fraction: Optional[float], maximum: float) -> Optional[float]:
    if remaining_fraction is None:
        return None
    return round(maximum * max(0.0, min(1.0, remaining_fraction)), 4)

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


def _sum_parts(parts: Dict[str, Optional[float]], maxima: Dict[str, float]) -> Dict[str, Any]:
    """Sum the parts that could be measured, and say what the rest cost the max.

    "配点を自動再配分せず、適用可能点と満点を明示する": a part with nothing to
    measure is dropped from the axis and from the axis's maximum, never scored
    zero and never spread across the others.
    """
    measurable = {name: value for name, value in parts.items() if value is not None}
    if not measurable:
        return {"points": None, "parts": parts, "max_effective": 0,
                "parts_not_measurable": sorted(parts)}
    return {
        "points": round(sum(measurable.values()), 4),
        "parts": parts,
        "max_effective": sum(maxima[name] for name in measurable),
        "parts_not_measurable": sorted(set(parts) - set(measurable)),
    }


def _score_a(axis: Dict[str, Any], yardstick_bands: Dict[str, float]) -> Dict[str, Any]:
    """A 生存環境 — 20点. CO2 8, O2 4, water 4, dwell 4.

    CO2 is anchored on the ladder's own next rung: zero points when the average
    overshoot above the nominal limit equals the gap up to the ISS off-nominal
    level.

    O2 and water are anchored on the plant's own band and empty -- zero points
    when the inventory sat at nothing for the whole run. That anchor was written
    when O2 was an 8 kg supply tank, where empty was reachable. Since R2 it is
    cabin atmosphere: 0 kg means no atmosphere at all, and the crew died about
    13 kg earlier, so the denominator is roughly a thousand times the deficit
    any real run accumulates (no-op integrates 23.58 against a 5212 anchor).
    Left alone deliberately -- EXP-022 measured the repair at under half a point
    and this branch has three retractions from repairing axes that do not move.
    Revisit when R4 gives O2 something to do.
    """
    dwell = axis["dwell"]
    steps = dwell["steps"] or 1
    exposure = axis["co2_exposure_integral"] or {}
    nominal = exposure.get(axis.get("co2_exposure_band")) if axis.get("co2_exposure_band") else None
    nominal_kg = yardstick_bands.get("nominal")
    off_nominal_kg = yardstick_bands.get("off_nominal")
    co2_scale = (
        (off_nominal_kg - nominal_kg) * steps
        if nominal_kg is not None and off_nominal_kg is not None
        else None
    )
    co2_used = _fraction(nominal, co2_scale) if co2_scale else None
    o2_used = _fraction(axis["o2_deficit_integral"], (axis.get("o2_band_low") or 0) * steps)
    water_used = _fraction(axis["water_deficit_integral"], (axis.get("water_band_low") or 0) * steps)
    critical_used = _fraction(dwell["overall"]["critical"], steps)
    parts = {
        "co2": _points(None if co2_used is None else 1 - co2_used, 8),
        "o2": _points(None if o2_used is None else 1 - o2_used, 4),
        "water": _points(None if water_used is None else 1 - water_used, 4),
        "dwell": _points(None if critical_used is None else 1 - critical_used, 4),
    }
    return _sum_parts(parts, {"co2": 8, "o2": 4, "water": 4, "dwell": 4})


def _score_b(axis: Dict[str, Any]) -> Dict[str, Any]:
    """B 資源余裕 — 20点. CO2 8, O2 6, water 6, each half worst-point half terminal.

    Margin is measured against the band the run was operated to, so full points
    mean "never approached the edge and ended clear of it".
    """
    co2, o2, water = axis["co2"], axis["o2"], axis["water"]
    limit = axis.get("co2_limit_kg")

    def below(value, edge):
        if value is None or not edge:
            return None
        return max(0.0, min(1.0, (edge - float(value)) / float(edge)))

    def above(value, edge):
        if value is None or not edge:
            return None
        return max(0.0, min(1.0, float(value) / float(edge)))

    parts = {
        "co2_worst": _points(below(co2["peak_kg"], limit), 4),
        "co2_terminal": _points(below(co2["terminal_kg"], limit), 4),
        "o2_worst": _points(above(o2["min_kg"], o2["band_low_kg"]), 3),
        "o2_terminal": _points(above(o2["terminal_kg"], o2["band_low_kg"]), 3),
        "water_worst": _points(above(water["min_l"], water["band_low_l"]), 3),
        "water_terminal": _points(above(water["terminal_l"], water["band_low_l"]), 3),
    }
    return _sum_parts(parts, {"co2_worst": 4, "co2_terminal": 4, "o2_worst": 3,
                              "o2_terminal": 3, "water_worst": 3, "water_terminal": 3})


def _score_c(axis: Dict[str, Any], dwell_steps: int) -> Dict[str, Any]:
    """C actorの操作判断 — 5点. latency 2, request sizing 2, invalid ops 1.

    Latency is anchored on the dwell window that kills: reaching the subsystem
    within it means nobody was lost to that band, and never acting while an
    alarm stood scores zero rather than being skipped.

    Request sizing is here rather than in D because the scorecard puts D after
    "物理的に妥当な要求" -- a command for thirty-six times what the machine can
    take is not a plant failure, it is a judgement about the plant.
    """
    latencies = axis.get("response_latency_steps") or {}
    graded = []
    for detail in latencies.values():
        if detail.get("latency_steps") is not None:
            graded.append(max(0.0, 1.0 - detail["latency_steps"] / max(1, dwell_steps)))
        elif detail.get("reason", "").startswith("no such command"):
            graded.append(0.0)  # an alarm stood and nothing was ever sent
    latency_points = _points(sum(graded) / len(graded), 2) if graded else None

    sizing = (axis.get("request_sizing") or {}).get("sizing_score")
    sizing_points = _points(sizing, 2) if sizing is not None else None

    outcomes = axis.get("command_outcomes") or {}
    applied = int(outcomes.get("applied") or 0)
    rejected = int(outcomes.get("rejected") or 0)
    issued = applied + rejected
    invalid_points = _points(1 - (rejected / issued), 1) if issued else None

    parts = {"latency": latency_points, "request_sizing": sizing_points, "invalid_ops": invalid_points}
    maxima = {"latency": 2, "request_sizing": 2, "invalid_ops": 1}
    return _sum_parts(parts, maxima)


def _score_d(axis: Dict[str, Any]) -> Dict[str, Any]:
    """D 設計・装置の物理応答 — 5点.

    The plant's axis, not the actor's. Comparing "requested" to "processed"
    directly does not measure it: the ARS request is a goal state, not a
    quantity of work, and the plant's own design note says asking for more than
    is on hand is how a caller says "give me what you can" -- saturating at the
    inventory is correct behaviour, not underperformance.

    So this asks whether the machine delivered everything it physically could:
    each applied command counts as satisfied when it was fully satisfied or
    limited only by its own capacity or by the inventory available.
    """
    outcomes = axis.get("delivery") or {}
    total = int(outcomes.get("commands") or 0)
    if not total:
        return {"points": None, "parts": {}}
    delivered = int(outcomes.get("delivered_all_it_could") or 0)
    return {
        "points": _points(delivered / total, 5),
        "parts": {"delivered_all_it_could": delivered, "commands": total,
                  "shortfalls": outcomes.get("shortfalls"),
                  "not_reported_by_plant": outcomes.get("not_reported_by_plant")},
    }


def _per_operation_capacity(run_dir: Path) -> Dict[str, Optional[float]]:
    """What one action of each subsystem can physically take, from the run's config.

    A request larger than this is not a plant failure -- the plant saturates --
    it is an actor asking for what the machine cannot do, which is C's business.

    Bounded by the step, because the rated-capacity invariant is: an action cannot
    process more than the step it happens in is long. Reading a subsystem's own
    quantum here instead would re-open, inside the scorecard, the hole the
    invariant closed in the plant -- C would call a request "within capacity"
    that the machine demonstrably cannot take.
    """
    path = Path(run_dir) / "scenario_config.yaml"
    if not path.is_file():
        return {}
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    plant = config.get("plant_sim") or {}
    time_cfg, ars, ogs, wrs = (plant.get(k) or {} for k in ("time", "ars", "ogs", "wrs"))
    out: Dict[str, Optional[float]] = {}
    # ARS takes a goal, not a quantity: the reference goal is the size the
    # machine is rated against, so a larger one is a request for super-rated work.
    out["air_revitalisation"] = ars.get("reference_goal_co2_kg")
    step_seconds = time_cfg.get("step_seconds")

    def _rated(rate_per_day: Any, operation_seconds: Any) -> Optional[float]:
        if rate_per_day is None or step_seconds is None:
            return None
        elapsed = float(step_seconds)
        if operation_seconds is not None:
            elapsed = min(float(operation_seconds), elapsed)
        return float(rate_per_day) * elapsed / 86400.0

    ogs_o2 = _rated(ogs.get("max_o2_kg_day"), time_cfg.get("ogs_operation_seconds"))
    out["oxygen_generation"] = ogs_o2 * WATER_PER_O2 if ogs_o2 is not None else None
    # Two ceilings now: the batch cap per action, and the rated throughput. The
    # smaller is what one action can take; at this step length it is the rating.
    wrs_rated = _rated(wrs.get("capacity_l_day"), time_cfg.get("wrs_operation_seconds"))
    wrs_batch = wrs.get("max_feed_l_per_operation")
    wrs_limits = [float(v) for v in (wrs_rated, wrs_batch) if v is not None]
    out["water_recovery"] = min(wrs_limits) if wrs_limits else None
    return out


_REQUEST_FIELDS = {
    "air_revitalisation": "initial_co2_mass",
    "oxygen_generation": "input_water_mass",
    "water_recovery": "urine_volume",
}


def _request_sizing(
    events: Sequence[Dict[str, Any]], capacity: Dict[str, Optional[float]]
) -> Dict[str, Any]:
    """How often the actor asked for something the machine could actually take."""
    within = total = 0
    oversized: Dict[str, int] = {}
    sized: List[float] = []
    for event in events:
        # Applied and rejected both: asking for something the machine cannot
        # take is a judgement, and it does not stop being one because the
        # command was refused for another reason.
        if event.get("kind") not in _COMMAND_OUTCOME_KINDS:
            continue
        command = event.get("command") or {}
        kind = str(command.get("kind") or "")
        limit = capacity.get(kind)
        field = _REQUEST_FIELDS.get(kind)
        if limit is None or field is None:
            continue
        asked = (command.get("payload") or {}).get(field)
        if asked is None:
            continue
        total += 1
        # Graded by how far over, not by whether. The rule arm asks 0.15 kg of
        # an OGS that can take 0.1447 -- 3.7% over, a stale tuning value -- and
        # an LLM run asked 246 kg, seventeen hundred times over. A binary test
        # scores those the same.
        headroom = min(1.0, float(limit) / float(asked)) if float(asked) > 0 else 1.0
        sized.append(headroom)
        if float(asked) <= float(limit) * (1 + 1e-9):
            within += 1
        else:
            oversized[kind] = oversized.get(kind, 0) + 1
    return {
        "within_capacity_fraction": round(within / total, 6) if total else None,
        "sizing_score": round(sum(sized) / len(sized), 6) if sized else None,
        "commands_sized": total,
        "oversized_by_kind": oversized,
        "per_operation_capacity": {k: v for k, v in capacity.items() if v is not None},
    }


_COMMAND_OUTCOME_KINDS = frozenset({
    "/eclss/events/operational_applied",
    "/eclss/events/operational_rejected",
})

#: Limiters that mean "the machine gave everything it physically could", per
#: _score_d's contract: fully satisfied, or limited only by its own capacity or
#: by the inventory available.
#:
#: ``rated_step_capacity`` belongs here and was missing until 2026-08-28. It is
#: the same physical limit as ``ogs_capacity`` / ``wrs_capacity`` -- model.py
#: picks between the two names by whether the step's allowance was already spent
#: (``"rated_step_capacity" if spent > tol else "ogs_capacity"``) -- so leaving
#: one name out scored the same physics two different ways. The whole of D's
#: variance in v4 and v5 came from the omission: adding the word returns every
#: run in all three generations to 5.000, SD 0. That makes D a constant here,
#: which is the true statement about this plant. Asking for more than a step can
#: process is a sizing error and C already scores it; D is the plant's axis.
_SATURATION_REASONS = frozenset({
    "cabin_co2_inventory", "ogs_capacity", "product_water", "urine_buffer",
    "grey_water", "wrs_capacity", "captured_co2", "available_o2",
    "rated_step_capacity",
})


def _delivery(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Did the plant give everything it physically could, per applied command."""
    commands = delivered = 0
    shortfalls: Dict[str, int] = {}
    unjudgeable: Dict[str, int] = {}
    for event in events:
        if event.get("kind") != "/eclss/events/operational_applied":
            continue
        details = (event.get("result") or {}).get("details") or {}
        if not details:
            continue
        limited = details.get("limited_by")
        if "fully_satisfied" not in details and limited is None:
            # water_recovery reports feeds and recoveries but never says whether
            # it was satisfied or what limited it. The plant not saying is not
            # the plant failing, so these are set aside rather than counted
            # against it -- and the count is reported so the silence is visible.
            unjudgeable[str((event.get("command") or {}).get("kind") or "unknown")] = (
                unjudgeable.get(str((event.get("command") or {}).get("kind") or "unknown"), 0) + 1
            )
            continue
        commands += 1
        limited_list = [limited] if isinstance(limited, str) else list(limited or [])
        satisfied = bool(details.get("fully_satisfied")) or (
            bool(limited_list) and all(reason in _SATURATION_REASONS for reason in limited_list)
        )
        if satisfied:
            delivered += 1
        else:
            key = ",".join(limited_list) or "unexplained"
            shortfalls[key] = shortfalls.get(key, 0) + 1
    return {"commands": commands, "delivered_all_it_could": delivered,
            "shortfalls": shortfalls, "not_reported_by_plant": unjudgeable}


def _command_outcomes(events: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Applied and rejected counts straight from the log, so old runs score too."""
    applied = sum(1 for e in events if e.get("kind") == "/eclss/events/operational_applied")
    rejected = sum(1 for e in events if e.get("kind") == "/eclss/events/operational_rejected")
    return {"applied": applied, "rejected": rejected}


def _bands_from_config(run_dir: Path) -> Optional[Dict[str, Any]]:
    """The bands the run's own effective config resolves to, or None if absent.

    ``scenario_config.yaml`` is the effective config written beside the run.
    Reading it costs nothing here -- this module already opens the same file for
    C's rated capacities (:func:`_rated_capacities`) and trajectory_metrics
    opens it for the crew size.
    """
    path = Path(run_dir) / "scenario_config.yaml"
    if not path.is_file():
        return None
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return resolve_survival_bands(config.get("plant_sim"), config.get("thresholds") or {})


def score_run(run_dir: Path, *, habitat: Optional[Habitat] = None) -> Dict[str, Any]:
    """Scorecard outputs for one run. Points only where a formula exists."""
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    gate = evaluate_physics_gate(run_dir)
    passed = gate_passed(gate)

    bands = summary.get("survival_bands") or resolve_survival_bands(
        None, summary.get("thresholds") or {}
    )
    # The bands decide attrition, which is the 50-point axis, and A's o2/water
    # anchors. Taking them from summary.json alone makes one hand-editable file
    # the whole of the bar: an audit (2026-08-29, EXP-022) rewrote that field on
    # a copy and the scorecard believed it. The effective config is written
    # beside it by the same run, so disagreement means one of the two is not a
    # record of what ran -- and there is no way to tell which. Refuse rather
    # than pick. A run that carries no config to check against keeps the old
    # behaviour and says so in ``bands_verified``.
    from_config = _bands_from_config(run_dir)
    bands_verified: Optional[bool] = None
    if from_config is not None and summary.get("survival_bands") is not None:
        overlap = {key: from_config.get(key) for key in bands}
        bands_verified = all(
            other is not None and float(value) == float(other)
            for key, value in bands.items()
            for other in (overlap.get(key),)
        )
        if not bands_verified:
            raise NotScorable(
                f"{run_dir.name}: summary.json survival_bands {bands} disagree with "
                f"scenario_config.yaml {overlap}. One of them is not what ran, and "
                "the bands decide attrition -- refusing rather than choosing."
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

    sizing = (
        _request_sizing(events, _per_operation_capacity(run_dir)) if operations_apply else {}
    )
    co2_limit_kg = (
        min(band.threshold_kg for band in yardstick.bands) if yardstick.bands else None
    )
    # The exposure scored is the one above the most stringent band, and the
    # scale is the distance up to the next rung of whatever ladder is in use:
    # nominal -> ISS off-nominal on the standard, high -> critical on a frozen
    # baseline. A single-band yardstick has no next rung and scores nothing
    # rather than dividing by zero.
    yardstick_bands: Dict[str, float] = {}
    exposure_band_name: Optional[str] = None
    if trajectory:
        ordered = sorted(
            trajectory["co2"]["bands"].items(), key=lambda kv: kv[1]["threshold_kg"]
        )
        exposure_band_name = ordered[0][0]
        yardstick_bands["nominal"] = ordered[0][1]["threshold_kg"]
        if len(ordered) > 1:
            yardstick_bands["off_nominal"] = ordered[1][1]["threshold_kg"]

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
            "points_policy": POINTS_POLICY,
            # CO2 is anchored on a published ladder, and since 2026-08-28 O2
            # and water have sourced limits too ([V2 6003], [V2 6109]). These
            # two are still anchored on the survival bands, which for O2 is one
            # rung below the operational alarm -- so CO2's margin is measured to
            # the alarm and O2's to the lethal floor. One axis, two kinds of
            # ruler; open, and recorded in EXP-022.
            "co2_exposure_band": exposure_band_name,
            "o2_band_low": bands.get("o2_storage_low_kg"),
            "water_band_low": bands.get("product_water_low_l"),
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
            "points_policy": POINTS_POLICY,
            "co2_limit_kg": co2_limit_kg,
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
            "points_policy": POINTS_POLICY,
            "request_within_capacity": sizing.get("within_capacity_fraction"),
            "request_sizing": sizing,
            "command_outcomes": _command_outcomes(events) if operations_apply else None,
            "response_latency_steps": _response_latency(health_rows, events) if operations_apply else None,
            "commands": summary.get("commands"),
        },
        "D_response": {
            "max": 5,
            "points": None,
            "applicable": operations_apply,
            "points_policy": POINTS_POLICY,
            "requested_processed_ratio": _requested_processed(events) if operations_apply else None,
            "delivery": _delivery(events) if operations_apply else None,
        },
    }

    # "配点を自動再配分せず、適用可能点と満点を明示する": an axis that applies
    # but has nothing to measure -- D on a run where every command was refused,
    # so the plant was never asked to respond -- leaves the total and takes its
    # points out of the maximum, rather than scoring zero for a failure that is
    # not there.
    if passed:
        a = _score_a(axes["A_environment"], yardstick_bands)
        axes["A_environment"].update(
            points=a["points"], parts=a["parts"],
            max_effective=a.get("max_effective"),
            parts_not_measurable=a.get("parts_not_measurable"),
        )
        b = _score_b(axes["B_margin"])
        axes["B_margin"].update(
            points=b["points"], parts=b["parts"],
            max_effective=b.get("max_effective"),
            parts_not_measurable=b.get("parts_not_measurable"),
        )
        if operations_apply:
            dwell_steps = int(
                ((summary.get("plant_sim") or {}).get("survival") or {}).get("co2", {}).get(
                    "warning_steps", 2
                )
                or 2
            )
            c = _score_c(axes["C_judgement"], dwell_steps)
            axes["C_judgement"].update(
            points=c["points"], parts=c["parts"],
            max_effective=c.get("max_effective"),
            parts_not_measurable=c.get("parts_not_measurable"),
        )
            d = _score_d(axes["D_response"])
            axes["D_response"].update(
            points=d["points"], parts=d["parts"],
            max_effective=d.get("max_effective"),
            parts_not_measurable=d.get("parts_not_measurable"),
        )

    for name, axis in axes.items():
        if axis.get("applicable", True) and axis["points"] is None and passed:
            axis["measurable"] = False
            axis.setdefault(
                "unmeasurable_reason",
                "nothing in the record to score this axis on",
            )

    counted = [
        axis for axis in axes.values()
        if axis.get("applicable", True) and axis.get("measurable", True)
    ]
    applicable_max = sum(
        axis["max_effective"] if axis.get("max_effective") is not None else axis["max"]
        for axis in counted
    )
    unscored = [
        name for name, axis in axes.items()
        if axis["points"] is None and axis.get("applicable", True) and axis.get("measurable", True)
    ]
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
        # True  = summary.json's bands agree with the effective config beside it
        # None  = the run carries nothing to check against (pre-2026-08 generations)
        # False never reaches here: a disagreement raises NotScorable above.
        "bands_verified": bands_verified,
        "axes": axes,
        "total": {
            "points": (
                round(sum(axis["points"] for axis in counted), 4)
                if passed and not unscored else None
            ),
            "axes_not_measurable": [
                name for name, axis in axes.items() if axis.get("measurable") is False
            ],
            "applicable_max": applicable_max,
            "unscored_axes": unscored,
            "note": (
                "physics gate failed; this run is not evidence"
                if not passed
                else f"points from {POINTS_POLICY}; the scorecard states only the 50-point formula"
            ),
        },
    }


__all__ = ["SCHEMA_VERSION", "score_run"]
