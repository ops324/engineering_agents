"""One Piece provenance exporter for scenario run outputs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


DESIGN_CHANGE_EVENT_KIND = "/eclss/events/design_change"
RECOVERY_EVENT_KIND = "/eclss/events/recovery_applied"
OPERATIONAL_EVENT_KIND = "/eclss/events/operational_applied"
OPERATIONAL_REJECTED_EVENT_KIND = "/eclss/events/operational_rejected"
OPERATIONAL_EVENT_KINDS = frozenset(
    {OPERATIONAL_EVENT_KIND, OPERATIONAL_REJECTED_EVENT_KIND}
)
EPS_BOOST_COMMAND_KIND = "request_eps_boost"


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _design_state_before(step: int, states: List[Dict[str, Any]]) -> Dict[str, Any]:
    before = {}
    for row in states:
        row_step = int(row.get("step", -1))
        if row_step < step:
            before = row
            continue
        break
    return before


def _design_state_after(step: int, states: List[Dict[str, Any]]) -> Dict[str, Any]:
    for row in states:
        row_step = int(row.get("step", -1))
        if row_step >= (step + 1):
            return row
    return states[-1] if states else {}


def _find_recovery_message(
    step: int,
    actor: str,
    messages: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Match recovery_command message for the step and issuing engineer (engineer_*)."""
    for row in messages:
        if int(row.get("step", -1)) != step:
            continue
        if row.get("message_type") != "recovery_command":
            continue
        if row.get("from_role") != actor:
            continue
        return row
    return None


def _find_design_message(
    step: int,
    actor: str,
    messages: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for row in messages:
        if int(row.get("step", -1)) != step:
            continue
        if row.get("message_type") != "design_change":
            continue
        if row.get("from_role") != actor:
            continue
        return row
    return None


#: The word an operator's message is expected to contain when it announces one
#: kind of command. Matched on word boundaries, not as a substring: ``"o2" in
#: text`` is true of every message that says ``co2``, and almost every message
#: says co2 -- an audit (2026-08-29, EXP-033) found 5 of 36 ``request_o2``
#: commands attributed to a message that never mentions O2 at all. ``"ars"``
#: has the same shape and matched ``clears`` three times. The keywords are
#: alphanumeric, so ``\b`` would fire between ``c`` and ``o2``; the lookarounds
#: below require a non-alphanumeric neighbour on each side instead.
_COMMAND_KEYWORD = {
    "air_revitalisation": "ars",
    "oxygen_generation": "ogs",
    "request_co2": "co2",
    "request_o2": "o2",
}


def _mentions(text: str, keyword: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _find_operational_message(
    step: int,
    actor: str,
    command_kind: str,
    messages: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for row in messages:
        if int(row.get("step", -1)) != step:
            continue
        if row.get("message_type") != "operational_command":
            continue
        if row.get("from_role") != actor:
            continue
        text = str(row.get("message", "")).lower()
        keyword = _COMMAND_KEYWORD.get(command_kind)
        if keyword is not None and _mentions(text, keyword):
            return row
    return None


def build_provenance_records(run_dir: Path) -> List[Dict[str, Any]]:
    run_dir = Path(run_dir)
    summary = _read_json(run_dir / "summary.json")
    events = _read_jsonl(run_dir / "events.jsonl")
    messages = _read_jsonl(run_dir / "messages.jsonl")
    design_states = sorted(
        _read_jsonl(run_dir / "design_state.jsonl"),
        key=lambda row: int(row.get("step", -1)),
    )

    run_id = run_dir.name
    scenario = summary.get("scenario")
    records: List[Dict[str, Any]] = []

    for idx, event in enumerate(events):
        if event.get("kind") != DESIGN_CHANGE_EVENT_KIND:
            continue

        step = int(event.get("step", -1))
        change = event.get("change", {}) or {}
        actor = change.get("proposed_by", "unknown")
        msg = _find_design_message(step=step, actor=actor, messages=messages)
        before = _design_state_before(step=step, states=design_states)
        after = _design_state_after(step=step, states=design_states)

        record = {
            "record_id": f"{run_id}:design_change:{len(records) + 1}",
            "run_id": run_id,
            "scenario": scenario,
            "step": step,
            "actor": actor,
            "actor_kind": "ai_agent",
            "change_kind": change.get("kind"),
            "payload": change.get("payload", {}),
            "before_topology": before.get("topology"),
            "after_topology": after.get("topology"),
            "before_parameters": before.get("parameters"),
            "after_parameters": after.get("parameters"),
            "trace": {
                "event_kind": event.get("kind"),
                "event_index": idx,
                "message": (msg or {}).get("message"),
                "reasoning": (msg or {}).get("reasoning"),
                "decision_source": (msg or {}).get("decision_source"),
                "parse_status": (msg or {}).get("parse_status"),
            },
        }
        records.append(record)

    for idx, event in enumerate(events):
        if event.get("kind") != RECOVERY_EVENT_KIND:
            continue
        command = event.get("command") or {}
        if command.get("kind") != EPS_BOOST_COMMAND_KIND:
            continue

        step = int(event.get("step", -1))
        actor = command.get("issued_by") or "unknown"
        msg = _find_recovery_message(step=step, actor=actor, messages=messages)
        eps_meta = event.get("eps") or {}

        record = {
            "record_id": f"{run_id}:recovery:{len(records) + 1}",
            "run_id": run_id,
            "scenario": scenario,
            "step": step,
            "record_type": "recovery",
            "actor": actor,
            "actor_kind": "ai_agent",
            "change_kind": EPS_BOOST_COMMAND_KIND,
            "payload": {"support_w": command.get("value"), "eps": eps_meta},
            "trace": {
                "event_kind": event.get("kind"),
                "event_index": idx,
                "message": (msg or {}).get("message"),
                "reasoning": (msg or {}).get("reasoning"),
                "decision_source": (msg or {}).get("decision_source"),
                "parse_status": (msg or {}).get("parse_status"),
                "event_message": event.get("message"),
            },
        }
        records.append(record)

    for idx, event in enumerate(events):
        if event.get("kind") not in OPERATIONAL_EVENT_KINDS:
            continue
        command = event.get("command") or {}
        command_kind = command.get("kind")
        if not command_kind:
            continue

        step = int(event.get("step", -1))
        actor = command.get("issued_by") or "unknown"
        msg = _find_operational_message(
            step=step,
            actor=actor,
            command_kind=str(command_kind),
            messages=messages,
        )
        result = event.get("result") or {}
        event_kind = str(event.get("kind", ""))
        result_success = result.get("success")
        if result_success is None:
            result_success = event_kind == OPERATIONAL_EVENT_KIND

        record = {
            "record_id": f"{run_id}:operational:{len(records) + 1}",
            "run_id": run_id,
            "scenario": scenario,
            "step": step,
            "record_type": "operational",
            "actor": actor,
            "actor_kind": "ai_agent",
            "change_kind": command_kind,
            "payload": command.get("payload", {}),
            "trace": {
                "event_kind": event_kind,
                "event_index": idx,
                "message": (msg or {}).get("message"),
                "reasoning": (msg or {}).get("reasoning"),
                "decision_source": (msg or {}).get("decision_source")
                or ((msg or {}).get("metadata") or {}).get("decision_source"),
                "parse_status": (msg or {}).get("parse_status"),
                "event_message": event.get("message"),
                "result_success": result_success,
            },
        }
        records.append(record)

    return records


def export_run_provenance(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    output_path = run_dir / "provenance.jsonl"
    records = build_provenance_records(run_dir)
    with output_path.open("w", encoding="utf-8") as f:
        for row in records:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return output_path
