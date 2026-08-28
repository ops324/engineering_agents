"""How deep, and for how long — the scorecard's A and B axes.

The scorecard is explicit that a run is not summarised by its worst instant:
評価は軌道に対して行い、「短い軽微超過」と「深刻な長時間曝露」を区別する.
Measured across 270 deterministic runs, that distinction is real rather than
theoretical -- two runs reaching an identical peak of 2.65 kg against an
identical 2.6 kg limit spent 15 and 3 steps above it. `peak` and the terminal
health call both rate those runs the same. Only the trajectory separates them.

**The yardstick is passed in, never read from the run being scored.** This is
the whole design. ``--apply-proposals`` accepts ``set_parameter`` on four
``thresholds.*`` keys, and those are exactly the keys
``compute_eclss_storage_health`` reads, so a run scored against its own
thresholds is scored against a bar its own proposal may have moved. A real
proposal does it: ``product_water_low_l`` 50.0 -> 40.0 on a run finishing at
44.07 L. Scoring therefore takes a :class:`Yardstick` built either from a
published standard (:func:`from_reference_limits`) or from a baseline that has
been frozen on purpose (:func:`from_frozen_baseline`), and records which.

**CO2 first, and O2 since R2.** ``o2_storage_kg`` was ``available_o2_kg``, a
supply tank OGS filled and the crew drew down, so an "O2 exposure integral"
computed from it would have been a number about a tank presented as a number
about people. R2 (``1d59f49``, 2026-08-28) made it cabin atmosphere with
[V2 6003] bands, and :func:`_o2_against_standard` scores it in PIO2 whenever a
habitat is supplied. Water stays an inventory rather than an exposure, and is
scored in crew-days at the [V2 6109] allocation. :data:`NOT_SCORED` has been
empty since; it is kept as a mechanism, not as a description of these two.

**A gate failure is not a low score.** Metrics are refused for a run whose
physics do not close, because a trajectory that could not have happened has
nothing to say about how long anyone was exposed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from scenario.ssos_eclss_loop.physics_gate import evaluate_physics_gate, gate_passed
from scenario.ssos_eclss_loop.reference_limits import (
    ASSUMED_TOTAL_PRESSURE_MMHG,
    CO2_BANDS,
    O2_BANDS,
    POTABLE_WATER_QUANTITY,
    Habitat,
    co2_kg_for_ppco2,
    o2_kg_for_pio2,
    pio2_mmhg,
    ppo2_mmhg as _ppo2_mmhg,
    provenance as limits_provenance,
)

SCHEMA_VERSION = "0.1.0"

#: Named so a report states its own coverage instead of implying完全性.
#: Empty since 2026-08-28: water gained a sourced [V2 6109] allocation, and O2
#: became cabin atmosphere with [V2 6003] bands to be scored against. Kept as a
#: mechanism -- a report that cannot say what it failed to measure will imply
#: coverage it does not have.
NOT_SCORED: Dict[str, str] = {}


class NotScorable(RuntimeError):
    """The run cannot be scored, as distinct from scoring badly."""


@dataclass(frozen=True)
class Band:
    """One severity level, expressed in the run's own units (kg cabin CO2)."""

    name: str
    threshold_kg: float
    origin: str


@dataclass(frozen=True)
class Yardstick:
    """Severity bands plus a record of where they came from.

    Frozen, and built by a constructor that names its source, so a yardstick
    can never be silently assembled from the run it is about to grade.
    """

    bands: Sequence[Band]
    source: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "bands": [
                {"name": b.name, "threshold_kg": round(b.threshold_kg, 6), "origin": b.origin}
                for b in self.bands
            ],
            **({"detail": self.detail} if self.detail else {}),
        }


def from_reference_limits(habitat: Habitat) -> Yardstick:
    """Bands from NASA-STD-3001, converted into the run's kg through a habitat.

    Nothing in this repository can edit the standard, which is the point. The
    habitat is the soft spot -- ppCO2 scales as 1/volume -- so it is carried in
    ``detail`` on every scored artifact.
    """
    bands = [
        Band(
            name=limit.label,
            threshold_kg=co2_kg_for_ppco2(limit.value, habitat),
            origin=f"{limit.requirement or limit.source} = {limit.value} {limit.unit}",
        )
        for limit in CO2_BANDS
    ]
    return Yardstick(bands=bands, source="nasa-std-3001", detail=limits_provenance(habitat))


def from_frozen_baseline(thresholds: Dict[str, Any], *, baseline_run_id: str) -> Yardstick:
    """Bands from a baseline run's thresholds, frozen at the moment it is named.

    For use before a habitat volume has been chosen, when mmHg cannot be
    expressed. Weaker than a standard -- these numbers came from the project,
    not from a document -- but sound as long as the baseline is fixed and named
    here rather than read out of whichever run is being scored.
    """
    pairs = (
        ("high", "co2_storage_high_kg"),
        ("critical", "co2_storage_critical_kg"),
    )
    bands: List[Band] = []
    for name, key in pairs:
        if key not in thresholds:
            raise NotScorable(f"frozen baseline is missing {key}")
        bands.append(
            Band(
                name=name,
                threshold_kg=float(thresholds[key]),
                origin=f"frozen from {baseline_run_id}: thresholds.{key}",
            )
        )
    return Yardstick(
        bands=bands,
        source="frozen-baseline",
        detail={"baseline_run_id": baseline_run_id, "thresholds": dict(thresholds)},
    )


def _pre_ops_series(run_dir: Path, field: str) -> List[float]:
    """One sample per step, taken from the pre-ops row.

    ``scenario_run`` appends a second ``post_ops: true`` telemetry row for a
    step, but *only* when the team issued at least one command. Scoring the
    raw file therefore counts rows rather than steps, and the row count is a
    function of how often the agents acted -- so a proposal that changes agent
    chattiness moves ``steps_above``, ``exposure_integral`` and
    ``longest_streak`` with the physical trajectory untouched. On a 40-step run
    that produced ``longest_streak = 53``, a value the run is not long enough
    to hold.

    The pre-ops row is the one taken for every step whether or not anyone
    acted, so selecting it makes the sample count a property of the run length
    alone. Taking "the last row per step" would also give one row per step, but
    a *different* row on active steps than on quiet ones -- reintroducing the
    same dependence in a form harder to see.

    Both rows sit at the same simulated instant, so the post-ops refresh is
    dropped rather than averaged in: it is a second look at one step, not a
    second step.
    """
    path = Path(run_dir) / "telemetry.jsonl"
    if not path.is_file():
        raise NotScorable(f"{path} does not exist")
    series: List[float] = []
    seen_steps = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("post_ops"):
            continue
        step = record.get("step")
        if step in seen_steps:
            # Not a shape this pipeline emits; refuse rather than double-count.
            raise NotScorable(
                f"{path} has more than one pre-ops row for step {step}"
            )
        seen_steps.add(step)
        series.append(float(record[field]))
    if not series:
        raise NotScorable(f"{path} carries no samples")
    return series


def _cabin_co2(run_dir: Path) -> List[float]:
    return _pre_ops_series(run_dir, "co2_storage_kg")


def _below_band(series: Sequence[float], threshold: float) -> Dict[str, Any]:
    """The same two axes as ``_band_metrics``, for a quantity that runs out.

    CO2 is scored above a ceiling; O2 and water are scored below a floor. The
    deficit integral is the mirror of the exposure integral: a shallow, long
    shortfall and a deep, brief one are different failures.
    """
    steps_below = 0
    deficit = 0.0
    longest = current = 0
    for value in series:
        if value <= threshold:
            steps_below += 1
            deficit += threshold - value
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return {
        "steps_below": steps_below,
        "fraction_below": round(steps_below / len(series), 6),
        "deficit_integral": round(deficit, 6),
        "longest_streak_below": longest,
    }


def _band_metrics(series: Sequence[float], threshold: float) -> Dict[str, Any]:
    """A-axis for one band: how often, how deep, and how long unbroken."""
    steps_above = 0
    exposure = 0.0
    longest = current = 0
    for value in series:
        if value >= threshold:
            steps_above += 1
            exposure += value - threshold
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return {
        "steps_above": steps_above,
        "fraction_above": round(steps_above / len(series), 6),
        # Depth times duration in one number: a shallow, long excursion and a
        # deep, brief one are different failures and should not tie.
        "exposure_integral_kg_steps": round(exposure, 6),
        "longest_streak": longest,
    }


def trajectory_metrics(
    run_dir: Path,
    yardstick: Yardstick,
    *,
    require_gate: bool = True,
) -> Dict[str, Any]:
    """Score one run's CO2 trajectory against a yardstick it did not choose."""
    run_dir = Path(run_dir)

    gate: Optional[Dict[str, Any]] = None
    if require_gate:
        gate = evaluate_physics_gate(run_dir)
        if not gate_passed(gate):
            raise NotScorable(
                f"{run_dir.name} failed the physics gate "
                f"({', '.join(gate['failed_checks'])}); a trajectory that could not "
                f"have happened says nothing about exposure"
            )

    series = _cabin_co2(run_dir)
    terminal = series[-1]
    bands = {
        band.name: {
            "threshold_kg": round(band.threshold_kg, 6),
            "origin": band.origin,
            **_band_metrics(series, band.threshold_kg),
            # B-axis: what was left at the end, signed so positive is headroom.
            "terminal_margin_kg": round(band.threshold_kg - terminal, 6),
        }
        for band in yardstick.bands
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "steps": len(series),
        "yardstick": yardstick.to_dict(),
        "physics_gate": {"verdict": gate["verdict"], "form": gate["form"]} if gate else None,
        "co2": {
            "peak_kg": round(max(series), 6),
            "terminal_kg": round(terminal, 6),
            "bands": bands,
        },
        "not_scored": dict(NOT_SCORED),
    }


def _o2_against_standard(
    o2: Sequence[float], habitat: Optional[Habitat]
) -> Dict[str, Any]:
    """Depth and duration below each [V2 6003] band, in PIO2.

    Needs a habitat for the same reason CO2 does: mass becomes a partial
    pressure only inside a volume. Without one the axis stays a house measure
    rather than silently borrowing a default.
    """
    if habitat is None:
        return {"pio2": None, "pio2_reason": "no habitat chosen; kg cannot become PIO2"}
    bands: Dict[str, Any] = {}
    for limit in O2_BANDS:
        floor_kg = o2_kg_for_pio2(float(limit.value), habitat)
        bands[limit.label] = {
            "floor_pio2_mmhg": limit.value,
            "floor_kg": round(floor_kg, 6),
            "terminal_margin_kg": round(o2[-1] - floor_kg, 6),
            "origin": f"{limit.source} = {limit.value:g} {limit.unit}",
            **_below_band(o2, floor_kg),
        }
    return {
        "pio2": {
            "min_mmhg": round(pio2_mmhg(_ppo2_mmhg(min(o2), habitat)), 6),
            "terminal_mmhg": round(pio2_mmhg(_ppo2_mmhg(o2[-1], habitat)), 6),
            "assumed_total_pressure_mmhg": ASSUMED_TOTAL_PRESSURE_MMHG,
            "bands": bands,
        }
    }


def _water_against_standard(run_dir: Path, water: Sequence[float]) -> Dict[str, Any]:
    """The reserve in crew-days at the [V2 6109] allocation.

    [V2 6109] is a provision rate -- what the system must be able to supply --
    and it sets no reserve horizon, saying only "for the expected needs of each
    mission". So the reserve is converted to crew-days at that rate rather than
    compared to a floor this repository would have had to invent. More days is
    better; the anchor is sourced and nothing here can edit it.

    Deliberately not divided by plant_sim's own 2.28 L/crew-day. That figure is
    what passes through the crew, held there by a mass balance the config
    enforces, and dividing by it would score the run against itself.
    """
    limit = POTABLE_WATER_QUANTITY
    crew = _crew_size(run_dir)
    if not limit.is_sourced or crew is None or crew <= 0:
        return {}
    per_day = float(limit.value) * crew
    return {
        "allocation_l_per_day": round(per_day, 6),
        "min_crew_days": round(min(water) / per_day, 6),
        "terminal_crew_days": round(water[-1] / per_day, 6),
        "allocation_origin": f"{limit.requirement} = {limit.value:g} {limit.unit} x {crew} crew",
    }


def _crew_size(run_dir: Path) -> Optional[int]:
    """Crew the run was configured for, from its own recorded config."""
    path = Path(run_dir) / "scenario_config.yaml"
    if not path.is_file():
        return None
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    crew = ((config.get("plant_sim") or {}).get("crew") or {}).get("size")
    return int(crew) if crew is not None else None


def inventory_metrics(
    run_dir: Path, bands: Dict[str, Any], habitat: Optional[Habitat] = None
) -> Dict[str, Any]:
    """Depth and duration on the two axes no standard can score here.

    Both axes are sourced now -- O2 against [V2 6003] since R2 made it cabin
    atmosphere, water against the [V2 6109] allocation -- and this function is
    still the one that measures *depth below a band* for them. The bands it is
    given are the survival bands, which for O2 sit one rung below the
    operational alarm ([V2 6003] calls 145-127 mmHg "indefinite with
    monitoring", so leaving the band is a breach and not a way to die). Whether
    B's margin should be measured from the alarm rung instead is open, and
    recorded in EXP-022 -- it is not settled by this docstring.

    Keeping the axes in the comparison matters either way: EXP-011 produced a
    run with the best cabin CO2 of ten and the fewest survivors, three of whom
    O2 dwell took.

    Scored against ``bands``, which is where those deaths come from, and which
    the caller is expected to freeze from the baseline for the same reason the
    CO2 yardstick is frozen.
    """
    run_dir = Path(run_dir)
    o2 = _pre_ops_series(run_dir, "o2_storage_kg")
    water = _pre_ops_series(run_dir, "product_water_reserve_l")
    o2_low = float(bands["o2_storage_low_kg"])
    water_low = float(bands["product_water_low_l"])
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "steps": len(o2),
        "scored_against": "survival bands, frozen by the caller",
        "not_a_standard": dict(NOT_SCORED),
        "o2": {
            "band_low_kg": round(o2_low, 6),
            "min_kg": round(min(o2), 6),
            "terminal_kg": round(o2[-1], 6),
            **_below_band(o2, o2_low),
            **_o2_against_standard(o2, habitat),
        },
        "water": {
            "band_low_l": round(water_low, 6),
            "min_l": round(min(water), 6),
            "terminal_l": round(water[-1], 6),
            **_below_band(water, water_low),
            **_water_against_standard(run_dir, water),
        },
    }


def write_trajectory_metrics(path: Path, metrics: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
