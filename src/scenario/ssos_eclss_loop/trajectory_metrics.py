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

**CO2 only, and that is a statement rather than an omission.** It is the one
species this plant models as cabin atmosphere. ``o2_storage_kg`` is
``available_o2_kg`` -- a supply tank OGS fills and the crew draws down -- so
there is no cabin O2 to be exposed to, and an "O2 exposure integral" computed
from it would be a number about a tank presented as a number about people.
Water is an inventory, not an exposure. Both are named in :data:`NOT_SCORED`
so a report says what it did not measure.

**A gate failure is not a low score.** Metrics are refused for a run whose
physics do not close, because a trajectory that could not have happened has
nothing to say about how long anyone was exposed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from scenario.ssos_eclss_loop.physics_gate import evaluate_physics_gate, gate_passed
from scenario.ssos_eclss_loop.reference_limits import (
    CO2_BANDS,
    Habitat,
    co2_kg_for_ppco2,
    provenance as limits_provenance,
)

SCHEMA_VERSION = "0.1.0"

#: Named so a report states its own coverage instead of implying完全性.
NOT_SCORED = {
    "o2": (
        "plant_sim models O2 as available_o2_kg, a supply inventory, not cabin "
        "atmosphere; there is no exposure to integrate"
    ),
    "water": "product water is an inventory, not an exposure",
}


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


def _cabin_co2(run_dir: Path) -> List[float]:
    path = Path(run_dir) / "telemetry.jsonl"
    if not path.is_file():
        raise NotScorable(f"{path} does not exist")
    series = [
        json.loads(line)["co2_storage_kg"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not series:
        raise NotScorable(f"{path} carries no samples")
    return [float(v) for v in series]


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
        "samples": len(series),
        "yardstick": yardstick.to_dict(),
        "physics_gate": {"verdict": gate["verdict"], "form": gate["form"]} if gate else None,
        "co2": {
            "peak_kg": round(max(series), 6),
            "terminal_kg": round(terminal, 6),
            "bands": bands,
        },
        "not_scored": dict(NOT_SCORED),
    }


def write_trajectory_metrics(path: Path, metrics: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
