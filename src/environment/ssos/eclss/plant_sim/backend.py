"""PlantSimEclssBackend — EclssBackend adapter over the mass-balance PlantModel.

Directly implements the ``EclssBackend`` protocol (does NOT inherit
``MockEclssBackend``) to keep a single source of truth for inventory and avoid
the parent's hidden ``_telemetry`` / ``_grey_water_buffer`` duplication.

Responsibilities that live here (not in the model):
- input validation (reject negative / NaN / Inf; allow 0 as no-op for goals)
- subsystem-failure gating (no mutation while a subsystem is failed)
- wrapping model result dicts into ActionResult / ServiceResult
- telemetry mapping (cabin CO2 -> co2_storage_kg; extras -> raw_topics.plant_sim)

Design reference: SSOS_MOCK_ECLSS_DESIGN_PLAN.md v2 §3.3, §6, §8, §9.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from environment.ssos.eclss.plant_sim.config import PlantSimConfig
from environment.ssos.eclss.plant_sim.model import PlantModel
from environment.ssos.eclss.types import (
    ActionResult,
    ArsGoal,
    EclssTelemetrySnapshot,
    OgsGoal,
    ServiceResult,
    WrsGoal,
)

_SUBSYSTEMS = ("ars", "ogs", "wrs")
_PHYSICS_LIMITING_LABELS = {
    "o2": "o2_physics",
    "water": "water_physics",
    "co2": "co2_physics",
}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _map_physics_limiting(limiting: list) -> list[str]:
    mapped: list[str] = []
    for item in limiting:
        label = _PHYSICS_LIMITING_LABELS.get(str(item), str(item))
        if label not in mapped:
            mapped.append(label)
    return mapped


class PlantSimEclssBackend:
    """Deterministic mass-balance ECLSS backend for agent simulation."""

    def __init__(self, config: Optional[PlantSimConfig] = None) -> None:
        self.config = config or PlantSimConfig()
        self.model = PlantModel(self.config)
        self._failure_flags: Dict[str, bool] = {sub: False for sub in _SUBSYSTEMS}
        self.last_ars_goal: Optional[ArsGoal] = None
        self.last_ogs_goal: Optional[OgsGoal] = None
        self.last_wrs_goal: Optional[WrsGoal] = None
        self._last_metabolism: Optional[Dict[str, float]] = None
        self._last_survival: Dict[str, Any] = {"lost_this_step": 0, "limiting": []}

    @classmethod
    def from_scenario_config(cls, config: Mapping[str, Any]) -> "PlantSimEclssBackend":
        return cls(PlantSimConfig.from_scenario_config(config))

    # ------------------------------------------------------------------ #
    # step capability (StepAdvanceableBackend)
    # ------------------------------------------------------------------ #
    def advance_step(self) -> None:
        self._last_survival = {"lost_this_step": 0, "limiting": []}
        self._last_metabolism = self.model.advance_step()

    def apply_capacity_drop(self) -> Dict[str, Any]:
        """Physics floor after band-dwell; returns physics-only lost/limiting.

        Telemetry ``survival`` merges this with any dwell losses already
        recorded by ``set_crew_alive`` in the same step.
        """
        result = dict(self.model.apply_capacity_drop())
        result["limiting"] = _map_physics_limiting(list(result.get("limiting") or []))
        self._merge_last_survival(
            int(result.get("lost_this_step") or 0),
            list(result.get("limiting") or []),
        )
        return result

    def set_crew_alive(self, n: int, limiting: Optional[list] = None) -> int:
        """Hard-set live occupants; never increases. Returns additional lost."""
        s = self.model.state
        current = int(s.crew_alive)
        n = max(0, min(int(n), current))
        lost = current - n
        s.crew_alive = n
        s.crew_lost_total += lost
        self._merge_last_survival(lost, list(limiting or []))
        return lost

    def _merge_last_survival(self, lost: int, limiting: list) -> None:
        if lost <= 0 and not limiting:
            return
        prev_lost = int(self._last_survival.get("lost_this_step") or 0)
        prev_lim = list(self._last_survival.get("limiting") or [])
        extra = [item for item in limiting if item not in prev_lim]
        self._last_survival = {
            "lost_this_step": prev_lost + max(0, int(lost)),
            "limiting": prev_lim + extra,
        }

    def poll_telemetry(self) -> EclssTelemetrySnapshot:
        s = self.model.state
        plant_sim_topic: Dict[str, Any] = {
            "simulation_time_s": s.simulation_time_s,
            "captured_co2_kg": s.captured_co2_kg,
            "urine_buffer_l": s.urine_buffer_l,
            "total_co2_vented_kg": s.total_co2_vented_kg,
            "total_h2_vented_kg": s.total_h2_vented_kg,
            "total_ch4_vented_kg": s.total_ch4_vented_kg,
            "total_wrs_brine_loss_l": s.total_wrs_brine_loss_l,
            "total_o2_shortfall_kg": s.total_o2_shortfall_kg,
            "total_water_shortfall_l": s.total_water_shortfall_l,
            "crew_initial": self.config.crew_size,
            "crew_alive": s.crew_alive,
            "crew_lost_total": s.crew_lost_total,
            "survival": {
                "enabled": bool(self.config.survival_enabled),
                "lost_this_step": int(self._last_survival.get("lost_this_step") or 0),
                "limiting": list(self._last_survival.get("limiting") or []),
            },
            # Cumulative generation/consumption, added 2026-08-24 for the
            # mass-balance gate (decision 96). Without these a run's recorded
            # trajectory cannot be closed per species: the crew side can be
            # rebuilt from last_metabolism, but OGS/WRS/Sabatier and the
            # external ledger leave no trace. The 346 v1 runs predate this and
            # can never be closed retroactively -- that is why the gate ships
            # with the weaker retroactive form beside the full one.
            "total_co2_generated_kg": s.total_co2_generated_kg,
            "total_o2_consumed_kg": s.total_o2_consumed_kg,
            "total_o2_generated_kg": s.total_o2_generated_kg,
            "total_electrolysis_water_kg": s.total_electrolysis_water_kg,
            "total_sabatier_co2_used_kg": s.total_sabatier_co2_used_kg,
            "total_wrs_recovered_water_l": s.total_wrs_recovered_water_l,
            "total_water_regenerated_l": s.total_water_regenerated_l,
            "total_potable_water_consumed_l": s.total_potable_water_consumed_l,
            "total_urine_generated_l": s.total_urine_generated_l,
            "total_condensate_generated_l": s.total_condensate_generated_l,
            "total_unrecoverable_crew_water_l": s.total_unrecoverable_crew_water_l,
            "total_o2_delivered_kg": s.total_o2_delivered_kg,
            "total_co2_delivered_kg": s.total_co2_delivered_kg,
            "total_product_water_delivered_l": s.total_product_water_delivered_l,
            "total_external_grey_water_submitted_l": s.total_external_grey_water_submitted_l,
        }
        if self._last_metabolism is not None:
            plant_sim_topic["last_metabolism"] = dict(self._last_metabolism)
            self._last_metabolism = None
        return EclssTelemetrySnapshot(
            co2_storage_kg=s.cabin_co2_kg,
            o2_storage_kg=s.cabin_o2_kg,
            product_water_reserve_l=s.product_water_l,
            grey_water_collected_l=s.grey_water_l,
            ars_failure_enabled=self._failure_flags["ars"],
            ogs_failure_enabled=self._failure_flags["ogs"],
            wrs_failure_enabled=self._failure_flags["wrs"],
            raw_topics={"plant_sim": plant_sim_topic},
        )

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def send_air_revitalisation_goal(self, goal: ArsGoal) -> ActionResult:
        self.last_ars_goal = goal
        mass = goal.initial_co2_mass
        if not _finite(mass) or mass < 0:
            return ActionResult(
                False,
                "invalid ARS goal: initial_co2_mass must be finite and >= 0",
                {"rejected": True, "initial_co2_mass": mass},
            )
        for name, value in (
            ("initial_moisture_content", goal.initial_moisture_content),
            ("initial_contaminants", goal.initial_contaminants),
        ):
            if not _finite(value) or not 0.0 <= value <= 100.0:
                return ActionResult(
                    False, f"invalid ARS goal: {name} must be within 0..100", {"rejected": True}
                )
        if self._failure_flags["ars"]:
            return ActionResult(False, "ARS subsystem failure: no operation", {"failed": True})

        result = self.model.run_ars(mass)
        result["ignored_inputs"] = ["initial_moisture_content", "initial_contaminants"]
        return ActionResult(True, "air_revitalisation complete", result)

    def send_oxygen_generation_goal(self, goal: OgsGoal) -> ActionResult:
        self.last_ogs_goal = goal
        water = goal.input_water_mass
        if not _finite(water) or water < 0:
            return ActionResult(
                False,
                "invalid OGS goal: input_water_mass must be finite and >= 0",
                {"rejected": True, "input_water_mass": water},
            )
        if self._failure_flags["ogs"]:
            return ActionResult(False, "OGS subsystem failure: no operation", {"failed": True})

        result = self.model.run_ogs(water)
        return ActionResult(True, "oxygen_generation complete", result)

    def send_water_recovery_goal(self, goal: WrsGoal) -> ActionResult:
        self.last_wrs_goal = goal
        urine = goal.urine_volume
        if not _finite(urine) or urine < 0:
            return ActionResult(
                False,
                "invalid WRS goal: urine_volume must be finite and >= 0",
                {"rejected": True, "urine_volume": urine},
            )
        if self._failure_flags["wrs"]:
            return ActionResult(False, "WRS subsystem failure: no operation", {"failed": True})

        result = self.model.run_wrs(urine)
        if not result["has_feed"]:
            result["reason"] = "no_feed"
            return ActionResult(False, "water_recovery no-op: no feed available", result)
        return ActionResult(True, "water_recovery complete", result)

    # ------------------------------------------------------------------ #
    # services (payout / intake from existing inventory; independent of failures)
    # ------------------------------------------------------------------ #
    def request_o2(self, amount: float) -> ServiceResult:
        return self._request(amount, self.model.request_o2, "o2")

    def request_co2(self, amount: float) -> ServiceResult:
        return self._request(amount, self.model.request_co2, "co2")

    def request_product_water(self, liters: float) -> ServiceResult:
        return self._request(liters, self.model.request_product_water, "product water")

    def submit_grey_water(self, liters: float) -> ServiceResult:
        if not _finite(liters) or liters <= 0:
            return ServiceResult(False, 0.0, "invalid grey water volume: must be finite and > 0")
        accepted = self.model.submit_grey_water(liters)
        return ServiceResult(True, accepted, "grey water accepted")

    def _request(self, amount: float, payout, label: str) -> ServiceResult:
        if not _finite(amount) or amount <= 0:
            return ServiceResult(False, 0.0, f"invalid {label} request: must be finite and > 0")
        granted = payout(amount)
        success = granted >= amount - self.config.invariant_tolerance
        message = f"{label} delivered" if success else f"partial: insufficient {label}"
        return ServiceResult(success, granted, message)

    # ------------------------------------------------------------------ #
    def set_subsystem_failure(self, subsystem: str, enabled: bool) -> None:
        key = subsystem.lower().removesuffix("_failure")
        if key not in self._failure_flags:
            raise ValueError(f"unknown subsystem: {subsystem!r}")
        self._failure_flags[key] = enabled


__all__ = ["PlantSimEclssBackend"]
