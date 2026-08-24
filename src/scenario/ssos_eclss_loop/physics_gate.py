"""Physics gate — decide whether a run is admissible evidence at all.

Scored axes rank runs against each other. This does not: it asks whether the
trajectory a run recorded could have happened, and a run that fails is not a
low-scoring run, it is a void one. Nothing downstream should score it, average
it into a condition, or cite it in a comparison. The gate carries no points of
its own for the same reason a ruler carries no length.

Every check here is a pure function of ``telemetry.jsonl``. It reads no config
it could be tuned against, mutates nothing, and needs neither the simulator nor
a backend to be running -- so it can be applied to runs that finished months
ago, and cannot be made to pass by changing anything except the physics.

**Ledgers.** ``plant_sim`` writes six inventories and a set of cumulative
totals, and between them each species closes exactly (``plant_sim/model.py``):

    carbon    cabin + captured + vented + sabatier_used + delivered
              = initial + generated

    oxygen    available + consumed + delivered = initial + generated

    water     product + urine_buffer + grey
              = initial - potable_consumed + urine_generated + condensate
                - electrolysis_water + water_regenerated
                - brine_loss - product_delivered + external_grey_submitted

``total_unrecoverable_crew_water_l`` is deliberately absent from the water
ledger. It is a diagnostic of where crew water went, not a flow out of any
pool -- the mass it names left through ``potable_consumed`` already, and
subtracting it again double-counts.

``total_wrs_recovered_water_l`` is absent for the same class of reason: WRS
moves water between pools that are both inside the ledger, so only the brine
it loses crosses the boundary.

**Retroactive form.** The cumulative totals landed on 2026-08-24 (decision 96).
Runs older than that -- the 346 v1 runs among them -- carry inventories but no
totals, and their ledgers can never be closed. Rather than fail them, which
would read as "physics violated" when it means "not recorded", the ledger
checks report ``skipped`` with the field that was missing, and the checks that
do not need totals still run. A run's gate result says which form it got.

Capacity bounds (the scorecard's 装置能力上限内) are not implemented here:
per-operation limits need the goal each command carried, which telemetry does
not retain. That check is reported as ``skipped`` with a reason rather than
silently omitted -- a gate that does not say what it did not check reads as
having checked everything.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from environment.ssos.eclss.plant_sim.stoichiometry import (
    CH4_PER_H2,
    CO2_PER_H2,
    H2O_PER_H2,
    H2_PER_O2,
    WATER_PER_O2,
)
from environment.ssos.eclss.units import WATER_DENSITY_KG_PER_L

SCHEMA_VERSION = "0.1.0"

#: Absolute and relative slack for a ledger residual. Fifty steps of double
#: precision accumulate error near 1e-12 relative; this is loose enough not to
#: flag arithmetic and tight enough that a dropped term cannot hide.
ABS_TOL = 1.0e-9
REL_TOL = 1.0e-9

#: An inventory may be clamped to zero from just below it (model.py does the
#: same); anything further negative is a violation.
CLAMP_EPSILON = 1.0e-9

#: Readings every backend emits. Absent means the trajectory is unusable.
REQUIRED_READINGS = (
    "co2_storage_kg",
    "o2_storage_kg",
    "product_water_reserve_l",
)

#: Pools only some backends model. ``mock`` carries no grey-water loop and no
#: ``raw_topics`` at all, so requiring these would fail every mock run for not
#: recording a quantity it never had -- the same "not recorded reads as
#: violated" error the retroactive form exists to avoid. Absent means the
#: ledgers that need them skip; present means they are checked like any other.
LEDGER_READINGS = ("grey_water_collected_l",)

#: Inventories carried under ``raw_topics.plant_sim``.
PLANT_INVENTORIES = ("captured_co2_kg", "urine_buffer_l")

CUMULATIVE_TOTALS = (
    "total_co2_vented_kg",
    "total_h2_vented_kg",
    "total_ch4_vented_kg",
    "total_wrs_brine_loss_l",
    "total_o2_shortfall_kg",
    "total_water_shortfall_l",
    "total_co2_generated_kg",
    "total_o2_consumed_kg",
    "total_o2_generated_kg",
    "total_electrolysis_water_kg",
    "total_sabatier_co2_used_kg",
    "total_wrs_recovered_water_l",
    "total_water_regenerated_l",
    "total_potable_water_consumed_l",
    "total_urine_generated_l",
    "total_condensate_generated_l",
    "total_unrecoverable_crew_water_l",
    "total_o2_delivered_kg",
    "total_co2_delivered_kg",
    "total_product_water_delivered_l",
    "total_external_grey_water_submitted_l",
)

def _finite(value: Any) -> bool:
    """Whether a reading is a finite number.

    A reading that is not a number at all -- a string, a dict, a bool -- is a
    corrupt reading, which is exactly what this gate is for. Letting float()
    raise would turn it into a CLI traceback with exit 1, indistinguishable
    from a broken invocation, which is the confusion GATE_FAILED_EXIT exists
    to prevent.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    worst_residual: Optional[float] = None
    worst_step: Optional[int] = None
    violations: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }
        if self.worst_residual is not None:
            payload["worst_residual"] = self.worst_residual
            payload["worst_step"] = self.worst_step
        if self.violations:
            payload["violations"] = self.violations[:10]
            payload["violation_count"] = len(self.violations)
        return payload


class TelemetryUnreadable(RuntimeError):
    """The run has no trajectory to check."""


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def load_telemetry(run_dir: Path) -> List[Dict[str, Any]]:
    path = Path(run_dir) / "telemetry.jsonl"
    if not path.is_file():
        raise TelemetryUnreadable(f"{path} does not exist")
    records: List[Dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise TelemetryUnreadable(f"{path}:{lineno} is not JSON: {exc}") from exc
    if not records:
        raise TelemetryUnreadable(f"{path} is empty")
    return records


def _plant(record: Dict[str, Any]) -> Dict[str, Any]:
    return ((record.get("raw_topics") or {}).get("plant_sim") or {})


def _step(record: Dict[str, Any]) -> int:
    return int(record.get("step", -1))


def _close_enough(residual: float, scale: float) -> bool:
    return abs(residual) <= ABS_TOL + REL_TOL * abs(scale)


def missing_totals(records: Sequence[Dict[str, Any]]) -> List[str]:
    """Cumulative totals absent from the trajectory, in declaration order."""
    first = _plant(records[0])
    return [name for name in CUMULATIVE_TOTALS if name not in first]


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_readings_present_and_finite(records: Sequence[Dict[str, Any]]) -> CheckResult:
    violations: List[Dict[str, Any]] = []
    for record in records:
        plant = _plant(record)
        for name in REQUIRED_READINGS:
            value = record.get(name)
            if value is None:
                violations.append({"step": _step(record), "field": name, "reason": "missing"})
            elif not _finite(value):
                violations.append(
                    {"step": _step(record), "field": name, "reason": "not finite", "value": value}
                )
        # Optional pools are checked for sanity when present and ignored when
        # the backend does not model them.
        for name in LEDGER_READINGS:
            if name in record and not _finite(record[name]):
                violations.append(
                    {"step": _step(record), "field": name, "reason": "not finite"}
                )
        for name in PLANT_INVENTORIES:
            if name in plant and not _finite(plant[name]):
                violations.append(
                    {"step": _step(record), "field": name, "reason": "not finite"}
                )
    if violations:
        return CheckResult(
            "readings_present_and_finite",
            FAIL,
            f"{len(violations)} missing or non-finite reading(s)",
            violations=violations,
        )
    return CheckResult(
        "readings_present_and_finite",
        PASS,
        f"{len(records)} steps carry every required reading, all finite",
    )


def check_inventories_non_negative(records: Sequence[Dict[str, Any]]) -> CheckResult:
    violations: List[Dict[str, Any]] = []
    worst = 0.0
    worst_step: Optional[int] = None
    for record in records:
        plant = _plant(record)
        readings = [(n, record.get(n)) for n in REQUIRED_READINGS]
        readings += [(n, record.get(n)) for n in LEDGER_READINGS if n in record]
        readings += [(n, plant.get(n)) for n in PLANT_INVENTORIES if n in plant]
        for name, value in readings:
            if not _finite(value):
                # Already condemned by readings_present_and_finite; comparing a
                # string here would crash the gate instead of failing the run.
                continue
            value = float(value)
            if value < -CLAMP_EPSILON:
                violations.append({"step": _step(record), "field": name, "value": value})
                if value < worst:
                    worst, worst_step = value, _step(record)
    if violations:
        return CheckResult(
            "inventories_non_negative",
            FAIL,
            f"{len(violations)} negative inventory reading(s)",
            worst_residual=worst,
            worst_step=worst_step,
            violations=violations,
        )
    return CheckResult("inventories_non_negative", PASS, "no inventory went negative")


def check_totals_monotonic(records: Sequence[Dict[str, Any]]) -> CheckResult:
    absent = missing_totals(records)
    present = [name for name in CUMULATIVE_TOTALS if name not in absent]
    if not present:
        return CheckResult(
            "totals_monotonic", SKIPPED, "no cumulative totals in this trajectory"
        )
    violations: List[Dict[str, Any]] = []
    worst = 0.0
    worst_step: Optional[int] = None
    for previous, current in zip(records, records[1:]):
        prev_plant, curr_plant = _plant(previous), _plant(current)
        for name in present:
            before, after = prev_plant.get(name), curr_plant.get(name)
            if not _finite(before) or not _finite(after):
                continue
            delta = float(after) - float(before)
            if delta < -(ABS_TOL + REL_TOL * abs(float(before))):
                violations.append({"step": _step(current), "field": name, "delta": delta})
                if delta < worst:
                    worst, worst_step = delta, _step(current)
    if violations:
        return CheckResult(
            "totals_monotonic",
            FAIL,
            f"{len(violations)} cumulative total(s) decreased",
            worst_residual=worst,
            worst_step=worst_step,
            violations=violations,
        )
    detail = f"{len(present)} cumulative totals non-decreasing"
    if absent:
        detail += f"; {len(absent)} not recorded by this run"
    return CheckResult("totals_monotonic", PASS, detail)


def _ledger_check(
    name: str,
    records: Sequence[Dict[str, Any]],
    needed: Sequence[str],
    residual_at,
    *,
    needed_readings: Sequence[str] = (),
) -> CheckResult:
    """Close one species ledger against step 0 at every step.

    ``needed`` names plant totals, ``needed_readings`` names snapshot fields.
    Both are declared rather than discovered so a backend that models fewer
    pools skips the ledger instead of raising on the first missing key.
    """
    base_record = records[0]
    absent = [field_name for field_name in needed if field_name not in _plant(base_record)]
    absent += [
        field_name for field_name in needed_readings if field_name not in base_record
    ]
    if absent:
        return CheckResult(
            name,
            SKIPPED,
            "trajectory predates the cumulative totals; missing " + ", ".join(absent),
        )
    base = records[0]
    worst = 0.0
    worst_step: Optional[int] = None
    violations: List[Dict[str, Any]] = []
    for record in records:
        try:
            residual, scale = residual_at(base, record)
        except (TypeError, ValueError, KeyError):
            # A reading that is not a number is condemned by
            # readings_present_and_finite already. A ledger is a statement
            # about arithmetic and has nothing to say about a corrupt row --
            # raising here would turn a failed run into a crashed CLI.
            continue
        if abs(residual) > abs(worst):
            worst, worst_step = residual, _step(record)
        if not _close_enough(residual, scale):
            violations.append({"step": _step(record), "residual": residual, "scale": scale})
    if violations:
        return CheckResult(
            name,
            FAIL,
            f"ledger does not close at {len(violations)} step(s)",
            worst_residual=worst,
            worst_step=worst_step,
            violations=violations,
        )
    return CheckResult(
        name,
        PASS,
        f"closes at every step (worst residual {worst:.3e})",
        worst_residual=worst,
        worst_step=worst_step,
    )


def _delta(base_plant: Dict[str, Any], plant: Dict[str, Any], name: str) -> float:
    return float(plant.get(name, 0.0)) - float(base_plant.get(name, 0.0))


CARBON_TOTALS = (
    "captured_co2_kg",
    "total_co2_generated_kg",
    "total_co2_vented_kg",
    "total_sabatier_co2_used_kg",
    "total_co2_delivered_kg",
)


def check_carbon_ledger(records: Sequence[Dict[str, Any]]) -> CheckResult:
    def residual(base, record):
        b, p = _plant(base), _plant(record)
        stored_now = float(record["co2_storage_kg"]) + float(p["captured_co2_kg"])
        stored_before = float(base["co2_storage_kg"]) + float(b["captured_co2_kg"])
        produced = _delta(b, p, "total_co2_generated_kg")
        removed = (
            _delta(b, p, "total_co2_vented_kg")
            + _delta(b, p, "total_sabatier_co2_used_kg")
            + _delta(b, p, "total_co2_delivered_kg")
        )
        return (stored_now - stored_before) - (produced - removed), max(stored_now, produced, 1.0)

    return _ledger_check(
        "carbon_ledger",
        records,
        CARBON_TOTALS,
        residual,
        needed_readings=("co2_storage_kg",),
    )


OXYGEN_TOTALS = ("total_o2_generated_kg", "total_o2_consumed_kg", "total_o2_delivered_kg")


def check_oxygen_ledger(records: Sequence[Dict[str, Any]]) -> CheckResult:
    def residual(base, record):
        b, p = _plant(base), _plant(record)
        stored_now = float(record["o2_storage_kg"])
        stored_before = float(base["o2_storage_kg"])
        gained = _delta(b, p, "total_o2_generated_kg")
        lost = _delta(b, p, "total_o2_consumed_kg") + _delta(b, p, "total_o2_delivered_kg")
        return (stored_now - stored_before) - (gained - lost), max(stored_now, gained, 1.0)

    return _ledger_check(
        "oxygen_ledger",
        records,
        OXYGEN_TOTALS,
        residual,
        needed_readings=("o2_storage_kg",),
    )


WATER_TOTALS = (
    "urine_buffer_l",
    "total_potable_water_consumed_l",
    "total_urine_generated_l",
    "total_condensate_generated_l",
    "total_electrolysis_water_kg",
    "total_water_regenerated_l",
    "total_wrs_brine_loss_l",
    "total_product_water_delivered_l",
    "total_external_grey_water_submitted_l",
)


def _water_pool_l(record: Dict[str, Any]) -> float:
    plant = _plant(record)
    return (
        float(record["product_water_reserve_l"])
        + float(record["grey_water_collected_l"])
        + float(plant["urine_buffer_l"])
    )


def check_water_ledger(records: Sequence[Dict[str, Any]]) -> CheckResult:
    def residual(base, record):
        b, p = _plant(base), _plant(record)
        pool_now, pool_before = _water_pool_l(record), _water_pool_l(base)
        gained = (
            _delta(b, p, "total_urine_generated_l")
            + _delta(b, p, "total_condensate_generated_l")
            + _delta(b, p, "total_water_regenerated_l")
            + _delta(b, p, "total_external_grey_water_submitted_l")
        )
        lost = (
            _delta(b, p, "total_potable_water_consumed_l")
            + _delta(b, p, "total_electrolysis_water_kg") / WATER_DENSITY_KG_PER_L
            + _delta(b, p, "total_wrs_brine_loss_l")
            + _delta(b, p, "total_product_water_delivered_l")
        )
        return (pool_now - pool_before) - (gained - lost), max(pool_now, gained, 1.0)

    return _ledger_check(
        "water_ledger",
        records,
        WATER_TOTALS,
        residual,
        needed_readings=("product_water_reserve_l", "grey_water_collected_l"),
    )


STOICHIOMETRY_TOTALS = (
    "total_h2_vented_kg",
    "total_electrolysis_water_kg",
    "total_o2_generated_kg",
    "total_sabatier_co2_used_kg",
    "total_water_regenerated_l",
    "total_ch4_vented_kg",
)


def check_stoichiometric_residual(records: Sequence[Dict[str, Any]]) -> CheckResult:
    """Ratios fixed by molecular weight, checked on the cumulative totals.

    Mass can be conserved while the chemistry is wrong -- a run that turns
    water into oxygen at the wrong ratio still balances if the error is carried
    somewhere else. These three identities pin the reactions themselves.
    """
    absent = [name for name in STOICHIOMETRY_TOTALS if name not in _plant(records[0])]
    if absent:
        return CheckResult(
            "stoichiometric_residual",
            SKIPPED,
            "trajectory predates the cumulative totals; missing " + ", ".join(absent),
        )
    last = _plant(records[-1])
    o2_generated = float(last["total_o2_generated_kg"])
    co2_used = float(last["total_sabatier_co2_used_kg"])
    identities: Sequence[Tuple[str, float, float]] = (
        (
            "electrolysis water per O2",
            float(last["total_electrolysis_water_kg"]),
            o2_generated * WATER_PER_O2,
        ),
        (
            "sabatier water per CO2",
            float(last["total_water_regenerated_l"]) * WATER_DENSITY_KG_PER_L,
            co2_used * (H2O_PER_H2 / CO2_PER_H2),
        ),
        (
            "sabatier CH4 per CO2",
            float(last["total_ch4_vented_kg"]),
            co2_used * (CH4_PER_H2 / CO2_PER_H2),
        ),
        # Hydrogen closes the set. Without it the other three can all hold
        # while H2 is invented or destroyed: electrolysis makes
        # o2_generated * H2_PER_O2 of it, Sabatier consumes
        # sabatier_co2_used / CO2_PER_H2, and whatever is left is vented.
        # A run claiming 1000 kg of H2 vented from 0.2 kg of O2 used to pass
        # while the check reported "3 identities hold".
        (
            "hydrogen balance",
            float(last["total_h2_vented_kg"]) + co2_used / CO2_PER_H2,
            o2_generated * H2_PER_O2,
        ),
    )
    violations: List[Dict[str, Any]] = []
    worst = 0.0
    for label, observed, expected in identities:
        residual = observed - expected
        if abs(residual) > abs(worst):
            worst = residual
        if not _close_enough(residual, max(abs(expected), 1.0)):
            violations.append(
                {"identity": label, "observed": observed, "expected": expected, "residual": residual}
            )
    if violations:
        return CheckResult(
            "stoichiometric_residual",
            FAIL,
            f"{len(violations)} stoichiometric identity(ies) violated",
            worst_residual=worst,
            worst_step=_step(records[-1]),
            violations=violations,
        )
    return CheckResult(
        "stoichiometric_residual",
        PASS,
        f"{len(identities)} identities hold (worst residual {worst:.3e})",
        worst_residual=worst,
    )


FAILURE_QUIESCENCE_TOTALS = (
    "total_co2_generated_kg",
    "total_o2_generated_kg",
    "total_electrolysis_water_kg",
    "total_wrs_recovered_water_l",
    "total_wrs_brine_loss_l",
)


def check_failure_quiescence(records: Sequence[Dict[str, Any]]) -> CheckResult:
    """A failed subsystem must not have processed anything.

    ``plant_sim/backend.py`` returns before touching the model while a
    subsystem is failed, so the trace of any activity is a violation.

    ARS has no total of its own that only it writes, so it is caught through
    conservation instead: cabin CO2 gains only from metabolism and loses only
    to ARS, so across an interval where ARS was down the cabin must have risen
    by exactly what the crew produced.

    The flag is required down at *both* ends of an interval. A subsystem that
    failed and recovered between two polls is not evidence of anything, and
    calling it one would fail runs for being sampled coarsely.
    """
    absent = [name for name in FAILURE_QUIESCENCE_TOTALS if name not in _plant(records[0])]
    if absent:
        return CheckResult(
            "failure_quiescence",
            SKIPPED,
            "trajectory predates the cumulative totals; missing " + ", ".join(absent),
        )
    violations: List[Dict[str, Any]] = []
    intervals = 0
    for previous, current in zip(records, records[1:]):
        b, p = _plant(previous), _plant(current)
        step = _step(current)
        if not _finite(previous.get("co2_storage_kg")) or not _finite(
            current.get("co2_storage_kg")
        ):
            continue  # corrupt rows belong to readings_present_and_finite
        if previous.get("ars_failure_enabled") and current.get("ars_failure_enabled"):
            intervals += 1
            produced = _delta(b, p, "total_co2_generated_kg")
            cabin_rise = float(current["co2_storage_kg"]) - float(previous["co2_storage_kg"])
            residual = cabin_rise - produced
            if not _close_enough(residual, max(abs(produced), 1.0)):
                violations.append(
                    {"step": step, "subsystem": "ars", "co2_removed_kg": -residual}
                )
        if previous.get("ogs_failure_enabled") and current.get("ogs_failure_enabled"):
            intervals += 1
            moved = abs(_delta(b, p, "total_o2_generated_kg")) + abs(
                _delta(b, p, "total_electrolysis_water_kg")
            )
            if moved > ABS_TOL:
                violations.append({"step": step, "subsystem": "ogs", "activity": moved})
        if previous.get("wrs_failure_enabled") and current.get("wrs_failure_enabled"):
            intervals += 1
            moved = abs(_delta(b, p, "total_wrs_recovered_water_l")) + abs(
                _delta(b, p, "total_wrs_brine_loss_l")
            )
            if moved > ABS_TOL:
                violations.append({"step": step, "subsystem": "wrs", "activity": moved})
    if violations:
        return CheckResult(
            "failure_quiescence",
            FAIL,
            f"{len(violations)} interval(s) show a failed subsystem processing",
            violations=violations,
        )
    if intervals == 0:
        return CheckResult(
            "failure_quiescence", PASS, "no interval had a subsystem down at both ends"
        )
    return CheckResult(
        "failure_quiescence", PASS, f"{intervals} down-interval(s) show no processing"
    )


def check_capacity_bounds(records: Sequence[Dict[str, Any]]) -> CheckResult:
    """Declared, not implemented -- named so the gate's coverage is legible."""
    return CheckResult(
        "capacity_bounds",
        SKIPPED,
        "per-operation limits need the goal each command carried; telemetry "
        "retains totals, not goals",
    )


CHECKS = (
    check_readings_present_and_finite,
    check_inventories_non_negative,
    check_totals_monotonic,
    check_carbon_ledger,
    check_oxygen_ledger,
    check_water_ledger,
    check_stoichiometric_residual,
    check_failure_quiescence,
    check_capacity_bounds,
)


def evaluate_physics_gate(run_dir: Path) -> Dict[str, Any]:
    """Run every check over one run's trajectory and report the verdict."""
    run_dir = Path(run_dir)
    records = load_telemetry(run_dir)
    results = [check(records) for check in CHECKS]
    statuses = [r.status for r in results]
    verdict = FAIL if FAIL in statuses else PASS
    absent = missing_totals(records)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "verdict": verdict,
        "steps": len(records),
        "form": "retroactive" if absent else "full",
        "totals_not_recorded": absent,
        "tolerance": {"abs": ABS_TOL, "rel": REL_TOL, "clamp_epsilon": CLAMP_EPSILON},
        "checks": [r.to_dict() for r in results],
        "failed_checks": [r.name for r in results if r.status == FAIL],
        "skipped_checks": [r.name for r in results if r.status == SKIPPED],
    }


def gate_passed(result: Dict[str, Any]) -> bool:
    """Whether the run may be scored at all. A FAIL is void, not low-scoring."""
    return result.get("verdict") == PASS


def write_physics_gate(path: Path, result: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
