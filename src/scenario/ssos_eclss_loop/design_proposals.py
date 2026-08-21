"""design_proposals.json — post-run SSOS graph design for the next ssos_eclss_loop run.

Unified with scrubber_degradation naming (``design_proposals.json``). SSOS uses
``design_domain: ssos_graph`` and ROS-oriented ``change_kind`` values. Mock
topology kinds (``add_edge``, ``add_node``) belong to scrubber only.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List

DESIGN_DOMAIN = "ssos_graph"

SSOS_CHANGE_KINDS = frozenset(
    {
        "action_profile",
        "service_config",
        "set_parameter",
        "graph_rewire",
    }
)

ACTION_PROFILE_FIELDS_BY_SUBSYSTEM = {
    "ars": frozenset({"initial_co2_mass", "initial_moisture_content", "initial_contaminants"}),
    "ogs": frozenset({"input_water_mass", "iodine_concentration"}),
    "wrs": frozenset({"urine_volume"}),
}

ALLOWED_SET_PARAMETER_TARGETS = frozenset(
    {
        "agents.policy.co2_storage_high_kg",
        "agents.policy.o2_storage_low_kg",
        "agents.policy.product_water_low_l",
        "thresholds.co2_storage_high_kg",
        "thresholds.co2_storage_critical_kg",
        "thresholds.o2_storage_low_kg",
        "thresholds.product_water_low_l",
    }
)

ApplyHandler = Callable[[Dict[str, Any], Dict[str, Any]], None]


def load_design_proposals(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("design_proposals.json must be a JSON object")
    return data


def validate_design_proposals(data: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    domain = data.get("design_domain")
    if domain is not None and domain != DESIGN_DOMAIN:
        errors.append(f"design_domain must be {DESIGN_DOMAIN!r}, got {domain!r}")

    changes = data.get("changes")
    if not isinstance(changes, list):
        return errors + ["changes must be a list"]

    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            errors.append(f"changes[{index}] must be an object")
            continue
        kind = change.get("change_kind")
        if kind not in SSOS_CHANGE_KINDS:
            errors.append(
                f"changes[{index}].change_kind must be one of {sorted(SSOS_CHANGE_KINDS)}"
            )
        payload = change.get("payload")
        if payload is not None and not isinstance(payload, dict):
            errors.append(f"changes[{index}].payload must be an object")
    return errors


def _filter_action_profile_fields(subsystem: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    import math

    allowed = ACTION_PROFILE_FIELDS_BY_SUBSYSTEM.get(subsystem.lower())
    if allowed is None:
        raise ValueError(f"action_profile subsystem must be ars, ogs, or wrs, got {subsystem!r}")
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ValueError(
            f"action_profile.fields contains unsupported keys for {subsystem}: {unknown}"
        )
    filtered: Dict[str, Any] = {}
    for key in fields:
        if key not in allowed:
            continue
        value = fields[key]
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"action_profile.fields.{key} must be numeric") from exc
        if not math.isfinite(number) or number < 0.0:
            raise ValueError(f"action_profile.fields.{key} must be finite and non-negative")
        filtered[key] = number
    if not filtered:
        raise ValueError(f"action_profile.fields must include at least one known field for {subsystem!r}")
    return filtered


def _apply_action_profile(config: Dict[str, Any], payload: Dict[str, Any]) -> None:
    subsystem = str(payload.get("subsystem", "")).lower()
    fields = payload.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("action_profile.fields must be an object")
    filtered = _filter_action_profile_fields(subsystem, fields)
    policy = config.setdefault("agents", {}).setdefault("policy", {})
    if subsystem == "ars":
        policy.setdefault("ars_goal", {}).update(filtered)
    elif subsystem == "ogs":
        policy.setdefault("ogs_goal", {}).update(filtered)
    elif subsystem == "wrs":
        policy.setdefault("wrs_goal", {}).update(filtered)
    else:
        raise ValueError(f"action_profile subsystem must be ars, ogs, or wrs, got {subsystem!r}")


def _apply_service_config(config: Dict[str, Any], payload: Dict[str, Any]) -> None:
    import math

    service = str(payload.get("service", "")).lower()
    policy = config.setdefault("agents", {}).setdefault("policy", {})
    if service == "request_co2":
        if "amount" in payload:
            amount = float(payload["amount"])
            if not math.isfinite(amount) or amount <= 0.0:
                raise ValueError("request_co2 amount must be finite and positive")
            policy["request_co2_amount"] = amount
        if "before_ogs" in payload:
            policy["request_co2_before_ogs"] = bool(payload["before_ogs"])
    elif service == "request_o2":
        if "amount" in payload:
            amount = float(payload["amount"])
            if not math.isfinite(amount) or amount <= 0.0:
                raise ValueError("request_o2 amount must be finite and positive")
            policy["request_o2_amount"] = amount
    else:
        raise ValueError(f"unsupported service_config service: {service!r}")


def _apply_set_parameter(config: Dict[str, Any], payload: Dict[str, Any]) -> None:
    target = str(payload.get("target", ""))
    value = payload.get("value")
    if not target:
        raise ValueError("set_parameter.target is required")
    if target not in ALLOWED_SET_PARAMETER_TARGETS:
        allowed = ", ".join(sorted(ALLOWED_SET_PARAMETER_TARGETS))
        raise ValueError(
            f"set_parameter.target {target!r} is not allowed. Allowed targets: {allowed}"
        )

    parts = target.split(".")
    cursor: Any = config
    for part in parts[:-1]:
        if part not in cursor or not isinstance(cursor[part], dict):
            cursor[part] = {}
        cursor = cursor[part]
    cursor[parts[-1]] = value


def _apply_graph_rewire(config: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Merge launch remapping / gateway manifest for the next run."""
    graph = config.setdefault("ssos_graph", {})
    rewires = graph.setdefault("rewires", [])
    if not isinstance(rewires, list):
        raise ValueError("ssos_graph.rewires must be a list")
    rewires.append(copy.deepcopy(payload))


_APPLY_HANDLERS: Dict[str, ApplyHandler] = {
    "action_profile": _apply_action_profile,
    "service_config": _apply_service_config,
    "set_parameter": _apply_set_parameter,
    "graph_rewire": _apply_graph_rewire,
}


def apply_design_proposals(
    config: Dict[str, Any],
    proposals: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge proposal changes into scenario config for the *next* run."""
    errors = validate_design_proposals(proposals)
    if errors:
        raise ValueError("; ".join(errors))

    merged = copy.deepcopy(config)
    for change in proposals.get("changes", []):
        kind = change["change_kind"]
        payload = change.get("payload") or {}
        handler = _APPLY_HANDLERS.get(kind)
        if handler is None:
            raise ValueError(f"no apply handler for change_kind: {kind!r}")
        handler(merged, payload)
    return merged


def _append_threshold_bump(
    changes: List[Dict[str, Any]],
    *,
    target_policy: str,
    target_thresholds: str,
    value: float,
    why: str,
    what: str,
    how: str,
) -> None:
    for target in (target_policy, target_thresholds):
        changes.append(
            {
                "change_kind": "set_parameter",
                "payload": {"target": target, "value": value},
                "why": why,
                "what": what,
                "how": how,
            }
        )


def _annotate_change(
    change: Dict[str, Any],
    *,
    why: str,
    what: str,
    how: str,
) -> Dict[str, Any]:
    annotated = dict(change)
    annotated["why"] = why
    annotated["what"] = what
    annotated["how"] = how
    return annotated


def _co2_stress_why(
    *,
    final_health: Dict[str, Any],
    final_co2: Any,
    peak_co2: Any,
    co2_high: float,
) -> str:
    parts: List[str] = []
    status = str(final_health.get("co2_status", "")).lower()
    if status in {"warning", "critical"}:
        parts.append(f"final co2_status={status}")
    if final_co2 is not None and float(final_co2) >= co2_high:
        parts.append(f"final_co2_storage_kg={float(final_co2):.3g} >= co2_storage_high_kg={co2_high}")
    if peak_co2 is not None and float(peak_co2) >= co2_high:
        parts.append(f"peak_co2_storage_kg={float(peak_co2):.3g} >= co2_storage_high_kg={co2_high}")
    return "; ".join(parts) if parts else f"co2_storage_high_kg policy={co2_high}"


def _o2_stress_why(
    *,
    final_health: Dict[str, Any],
    min_o2: Any,
    o2_low: float,
) -> str:
    parts: List[str] = []
    status = str(final_health.get("o2_status", "")).lower()
    if status in {"warning", "critical"}:
        parts.append(f"final o2_status={status}")
    if min_o2 is not None and float(min_o2) <= o2_low:
        parts.append(f"min_o2_storage_kg={float(min_o2):.3g} <= o2_storage_low_kg={o2_low}")
    return "; ".join(parts) if parts else f"o2_storage_low_kg policy={o2_low}"


def build_design_proposals_from_run(
    *,
    proposed_by: str,
    decision_source: str,
    policy: Dict[str, Any],
    summary: Dict[str, Any] | None = None,
    message: str = "SSOS ECLSS design profiles proposed from run outcomes.",
    baseline_graph: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Propose next-run design changes from run outcomes (not a no-op policy copy).

    L8 (labeled_rule_base): prefer outcome-driven changes that differ from the
    run-time policy so ``--apply-proposals`` can change the next simulation.

    Fallback order when stress yields nothing: bump ``ars_goal`` only when
    the bump is a positive increase, else ``ogs_goal`` (same rule), else
    CO₂/O₂ thresholds (policy or defaults), else ``request_co2``
    service_config. Callers should skip writing ``design_proposals.json``
    when ``changes`` is still empty (e.g. LLM parse failure); see
    ``scenario_run``.
    """
    summary = summary or {}
    changes: List[Dict[str, Any]] = []

    ars_goal = dict(policy.get("ars_goal") or {})
    ogs_goal = dict(policy.get("ogs_goal") or {})
    final_health = summary.get("final_health") or {}
    final_co2 = summary.get("final_co2_storage_kg")
    peak_co2 = summary.get("peak_co2_storage_kg")
    min_o2 = summary.get("min_o2_storage_kg")
    co2_high = float(policy.get("co2_storage_high_kg", 1.5))
    o2_low = float(policy.get("o2_storage_low_kg", 0.45))

    co2_stressed = (
        str(final_health.get("co2_status", "")).lower() in {"warning", "critical"}
        or (final_co2 is not None and float(final_co2) >= co2_high)
        or (peak_co2 is not None and float(peak_co2) >= co2_high)
    )
    o2_stressed = (
        str(final_health.get("o2_status", "")).lower() in {"warning", "critical"}
        or (min_o2 is not None and float(min_o2) <= o2_low)
    )

    if co2_stressed:
        co2_why = _co2_stress_why(
            final_health=final_health,
            final_co2=final_co2,
            peak_co2=peak_co2,
            co2_high=co2_high,
        )
        base_mass = float(ars_goal.get("initial_co2_mass", 1.8))
        proposed_mass = round(base_mass * 1.25, 6)
        if proposed_mass != base_mass:
            changes.append(
                _annotate_change(
                    {
                        "change_kind": "action_profile",
                        "payload": {
                            "subsystem": "ars",
                            "action": "air_revitalisation",
                            "fields": {"initial_co2_mass": proposed_mass},
                        },
                    },
                    why=co2_why,
                    what="Raise ARS initial_co2_mass to remove more cabin CO2 per cycle.",
                    how=f"initial_co2_mass: {base_mass} → {proposed_mass}",
                )
            )
        proposed_high = round(co2_high * 0.9, 6)
        if proposed_high > 0.0 and proposed_high != co2_high:
            _append_threshold_bump(
                changes,
                target_policy="agents.policy.co2_storage_high_kg",
                target_thresholds="thresholds.co2_storage_high_kg",
                value=proposed_high,
                why=co2_why,
                what="Lower CO2 warning threshold to align policy with observed stress.",
                how=f"co2_storage_high_kg: {co2_high} → {proposed_high}",
            )

    if o2_stressed:
        o2_why = _o2_stress_why(
            final_health=final_health,
            min_o2=min_o2,
            o2_low=o2_low,
        )
        base_water = float(ogs_goal.get("input_water_mass", 0.015))
        proposed_water = round(base_water * 1.25, 6)
        if proposed_water != base_water:
            changes.append(
                _annotate_change(
                    {
                        "change_kind": "action_profile",
                        "payload": {
                            "subsystem": "ogs",
                            "action": "oxygen_generation",
                            "fields": {"input_water_mass": proposed_water},
                        },
                    },
                    why=o2_why,
                    what="Raise OGS input_water_mass to generate more O2 per cycle.",
                    how=f"input_water_mass: {base_water} → {proposed_water}",
                )
            )
        if "request_co2_amount" in policy:
            base_amt = float(policy.get("request_co2_amount", 0.025))
            proposed_amt = round(base_amt * 1.25, 6)
            if proposed_amt != base_amt:
                changes.append(
                    _annotate_change(
                        {
                            "change_kind": "service_config",
                            "payload": {
                                "service": "request_co2",
                                "amount": proposed_amt,
                                "before_ogs": bool(
                                    policy.get("request_co2_before_ogs", False)
                                ),
                            },
                        },
                        why=o2_why,
                        what="Increase request_co2 amount to feed Sabatier / OGS loop.",
                        how=f"request_co2_amount: {base_amt} → {proposed_amt}",
                    )
                )

    # L8 fallback: keep labeled proposals non-empty / non-no-op when possible.
    # Empty ``changes`` is still allowed for callers that skip the write (LLM).
    # Only accept a fallback that yields a positive bump; otherwise fall through.
    if not changes:
        if ars_goal:
            base_mass = float(ars_goal.get("initial_co2_mass", 1.8))
            proposed_mass = round(base_mass * 1.1, 6)
            if proposed_mass > base_mass:
                changes.append(
                    _annotate_change(
                        {
                            "change_kind": "action_profile",
                            "payload": {
                                "subsystem": "ars",
                                "action": "air_revitalisation",
                                "fields": {"initial_co2_mass": proposed_mass},
                            },
                        },
                        why="No stressed branch matched; fallback ARS profile bump.",
                        what="Slightly raise ARS initial_co2_mass for next run.",
                        how=f"initial_co2_mass: {base_mass} → {proposed_mass}",
                    )
                )
        if not changes and ogs_goal:
            base_water = float(ogs_goal.get("input_water_mass", 0.015))
            proposed_water = round(base_water * 1.1, 6)
            if proposed_water > base_water:
                changes.append(
                    _annotate_change(
                        {
                            "change_kind": "action_profile",
                            "payload": {
                                "subsystem": "ogs",
                                "action": "oxygen_generation",
                                "fields": {"input_water_mass": proposed_water},
                            },
                        },
                        why="No stressed branch matched; fallback OGS profile bump.",
                        what="Slightly raise OGS input_water_mass for next run.",
                        how=f"input_water_mass: {base_water} → {proposed_water}",
                    )
                )
        if not changes:
            proposed_high = round(co2_high * 0.9, 6)
            if proposed_high > 0.0 and proposed_high != co2_high:
                _append_threshold_bump(
                    changes,
                    target_policy="agents.policy.co2_storage_high_kg",
                    target_thresholds="thresholds.co2_storage_high_kg",
                    value=proposed_high,
                    why="No stressed branch matched; fallback CO2 threshold adjustment.",
                    what="Lower CO2 warning threshold for next run.",
                    how=f"co2_storage_high_kg: {co2_high} → {proposed_high}",
                )
            else:
                proposed_low = round(o2_low * 1.1, 6)
                if proposed_low > 0.0 and proposed_low != o2_low:
                    _append_threshold_bump(
                        changes,
                        target_policy="agents.policy.o2_storage_low_kg",
                        target_thresholds="thresholds.o2_storage_low_kg",
                        value=proposed_low,
                        why="No stressed branch matched; fallback O2 threshold adjustment.",
                        what="Raise O2 low threshold for next run.",
                        how=f"o2_storage_low_kg: {o2_low} → {proposed_low}",
                    )
                elif "request_co2_amount" in policy:
                    base_amt = float(policy.get("request_co2_amount", 0.025))
                    proposed_amt = round(base_amt * 1.1, 6)
                    if proposed_amt > base_amt:
                        changes.append(
                            _annotate_change(
                                {
                                    "change_kind": "service_config",
                                    "payload": {
                                        "service": "request_co2",
                                        "amount": proposed_amt,
                                        "before_ogs": bool(
                                            policy.get("request_co2_before_ogs", False)
                                        ),
                                    },
                                },
                                why="No stressed branch matched; fallback request_co2 bump.",
                                what="Slightly raise request_co2 amount for next run.",
                                how=f"request_co2_amount: {base_amt} → {proposed_amt}",
                            )
                        )

    doc: Dict[str, Any] = {
        "design_domain": DESIGN_DOMAIN,
        "proposed_by": proposed_by,
        "proposer_kind": "operator_rep",
        "decision_source": decision_source,
        "message": message,
        "changes": changes,
        "parse_notes": [],
    }
    if baseline_graph is not None:
        doc["baseline_graph"] = baseline_graph
    return doc


def write_design_proposals(path: Path, proposals: Dict[str, Any]) -> None:
    errors = validate_design_proposals(proposals)
    if errors:
        raise ValueError("; ".join(errors))
    path.write_text(json.dumps(proposals, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
