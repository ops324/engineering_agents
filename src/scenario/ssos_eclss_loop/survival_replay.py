"""Count occupant attrition on a recorded run, under bands it did not set.

Attrition is not scoring. It happens inside the run, driven by the health bands
in ``thresholds.*`` -- the same keys an operational alarm reads and the same
keys a ``set_parameter`` proposal may move. ``occupant_survival.md`` states the
sharing outright: "運用トリガーとサバイバル帯は同じ YAML キー".

So a proposal that raises an alarm above the trajectory makes the run report
``safe`` for every reading, the dwell counters never advance, and the deaths it
caused stop existing. EXP-010 measured it: raising ``co2_storage_high_kg`` from
2.0 to 4.76 kg took cabin peak CO2 from 2.380 to 2.980 kg -- worse air -- and
turned three survivors into four.

Grading on the baseline's frozen bar closes this for the CO2 figures, because
those are computed after the fact from the recorded trajectory. Attrition gets
no such second chance from the run itself, so this module manufactures one:
hold the trajectory fixed, apply the frozen bands to it, and step the same
dwell machine ``scenario_run`` steps.

**This is a counterfactual, not a prediction.** Attrition feeds back -- fewer
occupants generate less CO2 and draw less O2 -- and the recorded trajectory
carries the feedback of the deaths that actually happened, not of these. It
answers "how many would these bands have taken from this trajectory", which is
what comparing two arms on one bar requires, and nothing more. The physics
floor (``PlantModel.apply_capacity_drop``) is not replayed at all: it reads a
look-ahead on inventories that telemetry does not carry per step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from scenario.ssos_eclss_loop.health import compute_eclss_storage_health
from scenario.ssos_eclss_loop.survival import SurvivalDwellPolicy, SurvivalStreaks
from scenario.ssos_eclss_loop.trajectory_metrics import NotScorable

SCHEMA_VERSION = "0.1.0"


@dataclass(frozen=True)
class _Reading:
    """The three inventories ``compute_eclss_storage_health`` reads."""

    co2_storage_kg: float
    o2_storage_kg: float
    product_water_reserve_l: float


def _post_ops_states(run_dir: Path) -> List[Tuple[int, _Reading]]:
    """One reading per step: the last row, which is the state survival saw.

    ``trajectory_metrics._cabin_co2`` deliberately takes the *pre-ops* row, so
    that its sample count is the run length rather than a function of how often
    the agents acted. This takes the last row instead, and for the opposite
    reason: ``scenario_run`` applies dwell to the inventories *after* the step's
    operations, so the post-ops row is the state that actually drove attrition.
    On a step where nobody acted there is only one row and the two readings are
    the same. Either way this yields exactly one reading per step, so nothing
    here counts rows.
    """
    path = Path(run_dir) / "telemetry.jsonl"
    if not path.is_file():
        raise NotScorable(f"{path} does not exist")
    latest: Dict[int, _Reading] = {}
    order: List[int] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        step = int(record.get("step", -1))
        if step not in latest:
            order.append(step)
        try:
            latest[step] = _Reading(
                co2_storage_kg=float(record["co2_storage_kg"]),
                o2_storage_kg=float(record["o2_storage_kg"]),
                product_water_reserve_l=float(record["product_water_reserve_l"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NotScorable(f"{path} step {step} carries no usable readings") from exc
    if not latest:
        raise NotScorable(f"{path} carries no samples")
    return [(step, latest[step]) for step in sorted(order)]


def replay_survival(
    run_dir: Path,
    thresholds: Mapping[str, Any],
    survival: Mapping[str, Any],
    *,
    crew_initial: int,
    bands_from: str,
) -> Dict[str, Any]:
    """Dwell attrition this trajectory would have taken under ``thresholds``.

    ``bands_from`` names where the bands came from, and is carried into the
    result: a count is only meaningful beside the bar it was counted against.
    """
    policy = SurvivalDwellPolicy.from_config({"survival": dict(survival)})
    if not policy.enabled:
        raise NotScorable("survival is disabled in the config these bands came from")
    streaks = SurvivalStreaks()
    alive = max(0, int(crew_initial))
    losses: List[Dict[str, Any]] = []
    for step, reading in _post_ops_states(run_dir):
        health = compute_eclss_storage_health(step, reading, dict(thresholds))
        alive, lost, limiting, streaks, by_cause = policy.apply_dwell(alive, health, streaks)
        if lost:
            losses.append(
                {"step": step, "lost": lost, "limiting": limiting, "by_cause": dict(by_cause)}
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(run_dir),
        "bands_from": bands_from,
        "counterfactual": True,
        "crew_initial": int(crew_initial),
        "crew_remaining": alive,
        "crew_lost": int(crew_initial) - alive,
        "crew_lost_by_cause": dict(policy.lost_by_cause),
        "losses": losses,
        "physics_floor_replayed": False,
    }


__all__ = ["SCHEMA_VERSION", "replay_survival"]
