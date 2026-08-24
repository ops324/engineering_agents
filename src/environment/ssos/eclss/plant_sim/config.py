"""Configuration for the deterministic plant-sim ECLSS mock.

All parameters are kilograms / liters / seconds. Values fall into two classes
(see DESIGN_PLAN v2 §11.1):

- source-derived (crew BVAD rates, ARS/OGS throughput): borrowed from SSOS
  validated references; stoichiometry is exact from molecular weights.
- scenario-tuned (initial inventories, operation seconds): chosen so the mock
  runs plausibly; NOT physical limits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Mapping


class PlantConfigError(ValueError):
    """Raised when a PlantSimConfig is internally inconsistent."""


@dataclass(frozen=True)
class PlantSimConfig:
    # --- time ---
    step_seconds: float = 1200.0            # observation interval (Q6)
    # An operation quantum is how much of a subsystem's daily capacity one
    # action buys. It may not exceed step_seconds: the step is the only time
    # that passes, so a longer quantum lets one decision do more work than the
    # elapsed time allows. ARS was 4800 s against a 1200 s step -- one command
    # bought four steps of scrubbing while the world advanced one, inflating
    # ARS from 1.08x the crew's CO2 output to 4.3x, and making the rule
    # landscape non-monotonic: co2_high 1.2 beat both 1.0 and 1.5, an artifact
    # of when the one-shot latch re-armed relative to the failure window
    # rather than anything about the threshold.
    #
    # The design memo classifies this value as シナリオ調整 and states it does
    # not reproduce an SSOS internal cycle, so nothing physical is lost by
    # matching the step. Modelling a genuinely long batch would mean occupying
    # the steps it takes, not compressing them into one.
    ars_operation_seconds: float = 1200.0   # operation quantum per ARS action
    ogs_operation_seconds: float = 1200.0
    wrs_operation_seconds: float = 1200.0

    # --- crew (BVAD, kg/day/person) ---
    crew_size: int = 4
    activity_factor: float = 1.0            # nominal 1.0 / exercise 4.0 / sleep 0.7
    co2_kg_day_person: float = 1.04
    o2_kg_day_person: float = 0.84
    potable_water_kg_day_person: float = 2.28
    urine_kg_day_person: float = 1.50
    condensate_kg_day_person: float = 0.75
    unrecoverable_water_kg_day_person: float = 0.03

    # --- ARS ---
    ars_capacity_kg_day: float = 4.50
    ars_capture_efficiency: float = 0.83
    ars_reference_goal_co2_kg: float = 1.80

    # --- OGS ---
    ogs_max_o2_kg_day: float = 9.25

    # --- Sabatier ---
    sabatier_conversion_efficiency: float = 1.00  # Q2

    # --- WRS ---
    wrs_urine_recovery: float = 0.98        # Q3 (BPA-inclusive)
    wrs_grey_recovery: float = 0.90
    wrs_max_feed_l_per_operation: float = 10.0

    # --- initial inventories ---
    initial_cabin_co2_kg: float = 1.50
    initial_captured_co2_kg: float = 0.0
    initial_o2_kg: float = 0.48
    initial_product_water_l: float = 100.0
    initial_urine_buffer_l: float = 0.0
    initial_grey_water_l: float = 0.0

    # --- diagnostics ---
    invariant_tolerance: float = 1.0e-9
    clamp_epsilon: float = 1.0e-12

    def __post_init__(self) -> None:
        self._validate()

    # ------------------------------------------------------------------ #
    def _validate(self) -> None:
        def req(cond: bool, msg: str) -> None:
            if not cond:
                raise PlantConfigError(msg)

        # all floats finite
        for name, value in self._numeric_items():
            req(math.isfinite(value), f"{name} must be finite, got {value!r}")

        # positive quantities
        for name in (
            "step_seconds",
            "ars_operation_seconds",
            "ogs_operation_seconds",
            "wrs_operation_seconds",
            "ars_capacity_kg_day",
            "ars_reference_goal_co2_kg",
            "ogs_max_o2_kg_day",
            "wrs_max_feed_l_per_operation",
        ):
            req(getattr(self, name) > 0, f"{name} must be > 0")

        # An action cannot process more time than the step advances. Shorter is
        # fine -- the machine simply ran for part of the interval.
        for name in (
            "ars_operation_seconds",
            "ogs_operation_seconds",
            "wrs_operation_seconds",
        ):
            req(
                getattr(self, name) <= self.step_seconds,
                f"{name} ({getattr(self, name)}) must be <= step_seconds "
                f"({self.step_seconds}): an action cannot do more work than the "
                f"elapsed time allows",
            )

        # crew size / activity
        req(isinstance(self.crew_size, int) and self.crew_size >= 0, "crew_size must be int >= 0")
        req(self.activity_factor >= 0, "activity_factor must be >= 0")

        # per-person rates non-negative
        for name in (
            "co2_kg_day_person",
            "o2_kg_day_person",
            "potable_water_kg_day_person",
            "urine_kg_day_person",
            "condensate_kg_day_person",
            "unrecoverable_water_kg_day_person",
        ):
            req(getattr(self, name) >= 0, f"{name} must be >= 0")

        # fractions in [0, 1]
        for name in (
            "ars_capture_efficiency",
            "sabatier_conversion_efficiency",
            "wrs_urine_recovery",
            "wrs_grey_recovery",
        ):
            v = getattr(self, name)
            req(0.0 <= v <= 1.0, f"{name} must be within 0..1, got {v}")

        # initial inventories non-negative
        for name in (
            "initial_cabin_co2_kg",
            "initial_captured_co2_kg",
            "initial_o2_kg",
            "initial_product_water_l",
            "initial_urine_buffer_l",
            "initial_grey_water_l",
        ):
            req(getattr(self, name) >= 0, f"{name} must be >= 0")

        # crew water balance: outputs must sum to intake (mass conservation at crew)
        outputs = (
            self.urine_kg_day_person
            + self.condensate_kg_day_person
            + self.unrecoverable_water_kg_day_person
        )
        req(
            math.isclose(outputs, self.potable_water_kg_day_person, rel_tol=1e-9, abs_tol=1e-9),
            "crew water outputs (urine + condensate + unrecoverable = "
            f"{outputs}) must equal potable intake ({self.potable_water_kg_day_person})",
        )

    def _numeric_items(self):
        for name, value in self.__dict__.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                yield name, float(value)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_scenario_config(cls, config: Mapping[str, Any]) -> "PlantSimConfig":
        """Build a config from a nested scenario dict (see DESIGN_PLAN v2 §5.2).

        Missing keys fall back to class defaults. Unknown keys are ignored.
        """
        base = cls()
        if not config:
            return base

        sim = dict(config.get("simulation", {}) or {})
        ps = dict(config.get("plant_sim", {}) or {})
        time = dict(ps.get("time", {}) or {})
        crew = dict(ps.get("crew", {}) or {})
        ars = dict(ps.get("ars", {}) or {})
        ogs = dict(ps.get("ogs", {}) or {})
        sab = dict(ps.get("sabatier", {}) or {})
        wrs = dict(ps.get("wrs", {}) or {})
        diag = dict(ps.get("diagnostics", {}) or {})

        def pick(source: Mapping[str, Any], key: str, attr: str) -> Any:
            return source.get(key, getattr(base, attr))

        return replace(
            base,
            step_seconds=pick(time, "step_seconds", "step_seconds"),
            ars_operation_seconds=pick(time, "ars_operation_seconds", "ars_operation_seconds"),
            ogs_operation_seconds=pick(time, "ogs_operation_seconds", "ogs_operation_seconds"),
            wrs_operation_seconds=pick(time, "wrs_operation_seconds", "wrs_operation_seconds"),
            crew_size=int(pick(crew, "size", "crew_size")),
            activity_factor=pick(crew, "activity_factor", "activity_factor"),
            co2_kg_day_person=pick(crew, "co2_kg_day_person", "co2_kg_day_person"),
            o2_kg_day_person=pick(crew, "o2_kg_day_person", "o2_kg_day_person"),
            potable_water_kg_day_person=pick(
                crew, "potable_water_kg_day_person", "potable_water_kg_day_person"
            ),
            urine_kg_day_person=pick(crew, "urine_kg_day_person", "urine_kg_day_person"),
            condensate_kg_day_person=pick(
                crew, "condensate_kg_day_person", "condensate_kg_day_person"
            ),
            unrecoverable_water_kg_day_person=pick(
                crew, "unrecoverable_water_kg_day_person", "unrecoverable_water_kg_day_person"
            ),
            ars_capacity_kg_day=pick(ars, "capacity_kg_day", "ars_capacity_kg_day"),
            ars_capture_efficiency=pick(ars, "capture_efficiency", "ars_capture_efficiency"),
            ars_reference_goal_co2_kg=pick(ars, "reference_goal_co2_kg", "ars_reference_goal_co2_kg"),
            ogs_max_o2_kg_day=pick(ogs, "max_o2_kg_day", "ogs_max_o2_kg_day"),
            sabatier_conversion_efficiency=pick(
                sab, "conversion_efficiency", "sabatier_conversion_efficiency"
            ),
            wrs_urine_recovery=pick(wrs, "urine_recovery", "wrs_urine_recovery"),
            wrs_grey_recovery=pick(wrs, "grey_recovery", "wrs_grey_recovery"),
            wrs_max_feed_l_per_operation=pick(
                wrs, "max_feed_l_per_operation", "wrs_max_feed_l_per_operation"
            ),
            initial_cabin_co2_kg=pick(sim, "initial_co2_storage_kg", "initial_cabin_co2_kg"),
            initial_o2_kg=pick(sim, "initial_o2_storage_kg", "initial_o2_kg"),
            initial_product_water_l=pick(sim, "initial_product_water_l", "initial_product_water_l"),
            invariant_tolerance=pick(diag, "invariant_tolerance", "invariant_tolerance"),
            clamp_epsilon=pick(diag, "clamp_epsilon", "clamp_epsilon"),
        )


__all__ = ["PlantSimConfig", "PlantConfigError"]
