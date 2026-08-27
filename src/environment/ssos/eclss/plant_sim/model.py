"""Deterministic mass-balance plant model for the ECLSS mock.

This module is pure physics/bookkeeping: no ROS, no agent/contract types, no I/O.
The :class:`PlantModel` owns a :class:`PlantState` and mutates it through a small
set of operations (crew metabolism, ARS, OGS+Sabatier, WRS, resource services).

All quantities are kilograms (mass) or liters (water volume). Water uses density
1.0 kg/L (``eclss.units.WATER_DENSITY_KG_PER_L``); conversions still go through
explicit helpers so the intent stays visible.

Design reference: SSOS_MOCK_ECLSS_DESIGN_PLAN.md v2 §6, §7, §8, §12.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Dict, List, Tuple

from environment.ssos.eclss.plant_sim.config import PlantSimConfig
from environment.ssos.eclss.plant_sim.stoichiometry import (
    CH4_PER_H2,
    CO2_PER_H2,
    H2_PER_O2,
    H2O_PER_H2,
    WATER_PER_O2,
)
from environment.ssos.eclss.units import WATER_DENSITY_KG_PER_L


class PlantInvariantError(RuntimeError):
    """Raised when a state invariant (finite / non-negative) is violated."""


def water_l_to_kg(volume_l: float) -> float:
    return float(volume_l) * WATER_DENSITY_KG_PER_L


def water_kg_to_l(mass_kg: float) -> float:
    return float(mass_kg) / WATER_DENSITY_KG_PER_L


def per_interval(rate_per_day: float, seconds: float) -> float:
    """Amount accrued over ``seconds`` given a per-day rate."""
    return float(rate_per_day) * float(seconds) / 86400.0


@dataclass
class PlantState:
    simulation_time_s: float = 0.0

    # inventories (can decrease; guarded by invariants)
    cabin_co2_kg: float = 0.0
    captured_co2_kg: float = 0.0
    available_o2_kg: float = 0.0
    product_water_l: float = 0.0
    urine_buffer_l: float = 0.0
    grey_water_l: float = 0.0

    # cumulative sinks / diagnostics (monotonic non-decreasing)
    total_co2_vented_kg: float = 0.0
    total_h2_vented_kg: float = 0.0
    total_ch4_vented_kg: float = 0.0
    total_wrs_brine_loss_l: float = 0.0
    total_unrecoverable_crew_water_l: float = 0.0
    total_o2_shortfall_kg: float = 0.0
    total_water_shortfall_l: float = 0.0

    # cumulative crew production (for ledger tests)
    total_co2_generated_kg: float = 0.0
    total_o2_consumed_kg: float = 0.0
    total_potable_water_consumed_l: float = 0.0
    total_urine_generated_l: float = 0.0
    total_condensate_generated_l: float = 0.0

    # cumulative subsystem transformations (for ledger tests)
    total_o2_generated_kg: float = 0.0
    total_electrolysis_water_kg: float = 0.0
    total_sabatier_co2_used_kg: float = 0.0
    total_water_regenerated_l: float = 0.0
    total_wrs_recovered_water_l: float = 0.0

    # external service ledger
    total_o2_delivered_kg: float = 0.0
    total_co2_delivered_kg: float = 0.0
    total_product_water_delivered_l: float = 0.0
    total_external_grey_water_submitted_l: float = 0.0

    # Work done inside the current step, reset by advance_step. These exist so the
    # rated-capacity invariant can hold across *several* actions in one step: a
    # per-action bound alone is defeated by issuing the action twice (EXP-012 saw
    # 2 actors x 1.798 goal x the 4x quantum = 14.38x rated).
    co2_removed_this_step_kg: float = 0.0
    o2_generated_this_step_kg: float = 0.0
    wrs_processed_this_step_l: float = 0.0

    # occupant survival (crew_size is the initial roster; this is the live count)
    crew_alive: int = 0
    crew_lost_total: int = 0
    crew_lost_o2: int = 0
    crew_lost_water: int = 0
    crew_lost_co2: int = 0

    def copy(self) -> "PlantState":
        return PlantState(**{f.name: getattr(self, f.name) for f in fields(self)})


# Inventory fields that may legitimately approach zero and must stay >= 0.
_INVENTORY_FIELDS = (
    "cabin_co2_kg",
    "captured_co2_kg",
    "available_o2_kg",
    "product_water_l",
    "urine_buffer_l",
    "grey_water_l",
)


class PlantModel:
    """Owns a PlantState and applies deterministic mass-balance operations."""

    def __init__(self, config: PlantSimConfig | None = None) -> None:
        self.config = config or PlantSimConfig()
        c = self.config
        self.state = PlantState(
            cabin_co2_kg=c.initial_cabin_co2_kg,
            captured_co2_kg=c.initial_captured_co2_kg,
            available_o2_kg=c.initial_o2_kg,
            product_water_l=c.initial_product_water_l,
            urine_buffer_l=c.initial_urine_buffer_l,
            grey_water_l=c.initial_grey_water_l,
            crew_alive=c.crew_size,
        )
        self._last_survival: Dict[str, object] = {
            "lost_this_step": 0,
            "limiting": [],
        }
        self._check_invariants()

    # ------------------------------------------------------------------ #
    # crew metabolism (advance one observation interval)
    # ------------------------------------------------------------------ #
    def metabolic_headcount(self) -> int:
        """People whose metabolism applies on the next ``advance_step``."""
        if self.config.survival_enabled:
            return int(self.state.crew_alive)
        return int(self.config.crew_size)

    def per_person_o2_demand_kg(self) -> float:
        c = self.config
        return per_interval(c.o2_kg_day_person, c.step_seconds) * c.activity_factor

    def per_person_water_demand_l(self) -> float:
        c = self.config
        return water_kg_to_l(
            per_interval(c.potable_water_kg_day_person, c.step_seconds) * c.activity_factor
        )

    def per_person_co2_generated_kg(self) -> float:
        c = self.config
        return per_interval(c.co2_kg_day_person, c.step_seconds) * c.activity_factor

    def apply_capacity_drop(self) -> Dict[str, object]:
        """Physics floor: keep only people the next interval's O2/water can pay.

        Called after band-dwell. Cabin CO2 does not wipe crew here (scenario dwell).
        No-op when survival is disabled. Occupants never return.
        """
        c = self.config
        s = self.state
        if not c.survival_enabled:
            self._last_survival = {"lost_this_step": 0, "limiting": []}
            return dict(self._last_survival)

        current = int(s.crew_alive)
        o2_pp = self.per_person_o2_demand_kg()
        water_pp = self.per_person_water_demand_l()
        if o2_pp > 0.0:
            o2_cap = int(math.floor(s.available_o2_kg / o2_pp + 0.0))
        else:
            o2_cap = current
        if water_pp > 0.0:
            water_cap = int(math.floor(s.product_water_l / water_pp + 0.0))
        else:
            water_cap = current
        co2_cap = current  # CO2 losses are scenario dwell only
        supported = min(current, max(0, o2_cap), max(0, water_cap), max(0, co2_cap))
        lost = current - supported
        limiting: List[str] = []
        if lost > 0:
            if o2_cap < current:
                limiting.append("o2")
            if water_cap < current:
                limiting.append("water")
            # One headcount, one cause: O2 wins when both inventories bind.
            if o2_cap < current:
                s.crew_lost_o2 += lost
            elif water_cap < current:
                s.crew_lost_water += lost
        s.crew_alive = supported
        s.crew_lost_total += lost
        self._last_survival = {"lost_this_step": lost, "limiting": limiting}
        return dict(self._last_survival)

    def advance_step(self) -> Dict[str, float]:
        c = self.config
        s = self.state
        # A new step restores every subsystem's rated allowance. The scenario loop
        # calls this at the top of a step, before that step's actions.
        s.co2_removed_this_step_kg = 0.0
        s.o2_generated_this_step_kg = 0.0
        s.wrs_processed_this_step_l = 0.0
        factor = self.metabolic_headcount() * c.activity_factor

        co2_generated = per_interval(c.co2_kg_day_person, c.step_seconds) * factor
        o2_demand = per_interval(c.o2_kg_day_person, c.step_seconds) * factor
        water_demand_kg = per_interval(c.potable_water_kg_day_person, c.step_seconds) * factor

        # CO2 accumulates in cabin atmosphere
        s.cabin_co2_kg += co2_generated
        s.total_co2_generated_kg += co2_generated

        # O2 consumption (bounded by inventory; deficit recorded)
        o2_consumed = min(s.available_o2_kg, o2_demand)
        s.available_o2_kg -= o2_consumed
        s.total_o2_consumed_kg += o2_consumed
        s.total_o2_shortfall_kg += o2_demand - o2_consumed

        # potable water consumption (bounded by inventory)
        water_available_kg = water_l_to_kg(s.product_water_l)
        water_consumed_kg = min(water_available_kg, water_demand_kg)
        hydration = water_consumed_kg / water_demand_kg if water_demand_kg > 0 else 1.0
        s.product_water_l -= water_kg_to_l(water_consumed_kg)
        s.total_potable_water_consumed_l += water_kg_to_l(water_consumed_kg)
        s.total_water_shortfall_l += water_kg_to_l(water_demand_kg - water_consumed_kg)

        # crew water outputs scale with actual hydration (mass conserved at crew)
        urine_kg = per_interval(c.urine_kg_day_person, c.step_seconds) * factor * hydration
        condensate_kg = per_interval(c.condensate_kg_day_person, c.step_seconds) * factor * hydration
        loss_kg = (
            per_interval(c.unrecoverable_water_kg_day_person, c.step_seconds) * factor * hydration
        )
        s.urine_buffer_l += water_kg_to_l(urine_kg)
        s.grey_water_l += water_kg_to_l(condensate_kg)
        s.total_urine_generated_l += water_kg_to_l(urine_kg)
        s.total_condensate_generated_l += water_kg_to_l(condensate_kg)
        s.total_unrecoverable_crew_water_l += water_kg_to_l(loss_kg)

        s.simulation_time_s += c.step_seconds
        self._check_invariants()
        return {
            "co2_generated_kg": co2_generated,
            "o2_demand_kg": o2_demand,
            "o2_consumed_kg": o2_consumed,
            "water_demand_kg": water_demand_kg,
            "water_consumed_kg": water_consumed_kg,
            "hydration_fraction": hydration,
            "urine_generated_l": water_kg_to_l(urine_kg),
            "condensate_generated_l": water_kg_to_l(condensate_kg),
        }

    # ------------------------------------------------------------------ #
    # rated capacity: nothing processes more in a step than the step is long
    # ------------------------------------------------------------------ #
    def _rated_bounds(self, rate_per_day: float, operation_seconds: float,
                      done_this_step: float) -> Tuple[float, float, float]:
        """Ceiling for one action, given the rating and what the step already used.

        Two clamps, and both are needed. ``elapsed`` stops a single action from
        outrunning the step it happens in -- an operation quantum of 4800 s inside a
        1200 s step used to bill four steps of throughput to one (EXP-004). The
        ``allowance`` stops the step's rating being spent twice, which a per-action
        clamp cannot do: the same step could simply issue the action again, once per
        agent (EXP-012). Returns (ceiling, elapsed_seconds, rated_this_step).
        """
        c = self.config
        elapsed = min(float(operation_seconds), c.step_seconds)
        rated_this_step = per_interval(rate_per_day, c.step_seconds)
        allowance = max(0.0, rated_this_step - done_this_step)
        return min(per_interval(rate_per_day, elapsed), allowance), elapsed, rated_this_step

    # ------------------------------------------------------------------ #
    # ARS: remove cabin CO2, capture a fraction, vent the rest
    # ------------------------------------------------------------------ #
    def run_ars(self, goal_co2_mass_kg: float) -> Dict[str, float]:
        c = self.config
        s = self.state
        scale = goal_co2_mass_kg / c.ars_reference_goal_co2_kg
        rated, elapsed, rated_this_step = self._rated_bounds(
            c.ars_capacity_kg_day, c.ars_operation_seconds, s.co2_removed_this_step_kg
        )
        # The goal still says "more urgently"; it no longer sells extra machine.
        ordered = per_interval(c.ars_capacity_kg_day, elapsed) * scale
        max_removable = min(ordered, rated)
        removed = min(s.cabin_co2_kg, max_removable)
        captured = removed * c.ars_capture_efficiency
        vented = removed - captured

        s.cabin_co2_kg -= removed
        s.captured_co2_kg += captured
        s.total_co2_vented_kg += vented
        s.co2_removed_this_step_kg += removed
        self._check_invariants()

        tol = c.invariant_tolerance
        if removed < max_removable - tol:
            limited_by = "cabin_co2_inventory"
        elif max_removable < ordered - tol:
            limited_by = "rated_step_capacity"
        else:
            limited_by = None
        return {
            "co2_removed_kg": removed,
            "captured_co2_kg": captured,
            "vented_co2_kg": vented,
            "goal_scale": scale,
            "max_removable_kg": max_removable,
            "ordered_kg": ordered,
            "rated_step_kg": rated_this_step,
            "elapsed_seconds": elapsed,
            "fully_satisfied": limited_by is None,
            "limited_by": limited_by,
        }

    # ------------------------------------------------------------------ #
    # OGS electrolysis + internal Sabatier
    # ------------------------------------------------------------------ #
    def run_ogs(self, input_water_mass_kg: float) -> Dict[str, float]:
        c = self.config
        s = self.state

        requested = input_water_mass_kg
        available = water_l_to_kg(s.product_water_l)
        max_o2, elapsed, rated_this_step = self._rated_bounds(
            c.ogs_max_o2_kg_day, c.ogs_operation_seconds, s.o2_generated_this_step_kg
        )
        max_water_by_capacity = max_o2 * WATER_PER_O2
        processed = min(requested, available, max_water_by_capacity)

        o2_generated = processed / WATER_PER_O2
        h2_generated = o2_generated * H2_PER_O2

        s.product_water_l -= water_kg_to_l(processed)
        s.available_o2_kg += o2_generated

        # Sabatier: H2 + captured CO2 -> CH4 + H2O
        h2_eligible = h2_generated * c.sabatier_conversion_efficiency
        h2_limited_by_co2 = s.captured_co2_kg / CO2_PER_H2 if CO2_PER_H2 > 0 else 0.0
        h2_used = min(h2_eligible, h2_limited_by_co2)
        co2_used = h2_used * CO2_PER_H2
        water_regenerated = h2_used * H2O_PER_H2
        ch4_generated = h2_used * CH4_PER_H2
        h2_vented = h2_generated - h2_used

        s.captured_co2_kg -= co2_used
        s.product_water_l += water_kg_to_l(water_regenerated)
        s.total_ch4_vented_kg += ch4_generated
        s.total_h2_vented_kg += h2_vented
        s.total_o2_generated_kg += o2_generated
        s.total_electrolysis_water_kg += processed
        s.total_sabatier_co2_used_kg += co2_used
        s.total_water_regenerated_l += water_kg_to_l(water_regenerated)
        s.o2_generated_this_step_kg += o2_generated
        self._check_invariants()

        tol = c.invariant_tolerance
        limited_by = []
        if processed < requested - tol:
            if abs(processed - max_water_by_capacity) <= tol:
                spent = s.o2_generated_this_step_kg - o2_generated
                limited_by.append(
                    "rated_step_capacity" if spent > tol else "ogs_capacity"
                )
            if abs(processed - available) <= tol:
                limited_by.append("product_water")
        return {
            "requested_water_kg": requested,
            "processed_water_kg": processed,
            "rated_step_o2_kg": rated_this_step,
            "elapsed_seconds": elapsed,
            "o2_generated_kg": o2_generated,
            "h2_generated_kg": h2_generated,
            "sabatier_co2_used_kg": co2_used,
            "sabatier_h2_used_kg": h2_used,
            "water_regenerated_kg": water_regenerated,
            "ch4_generated_kg": ch4_generated,
            "h2_vented_kg": h2_vented,
            "fully_satisfied": not limited_by,
            "limited_by": limited_by or None,
        }

    # ------------------------------------------------------------------ #
    # WRS: process urine + grey water from internal buffers
    # ------------------------------------------------------------------ #
    def run_wrs(self, requested_urine_l: float) -> Dict[str, float]:
        c = self.config
        s = self.state

        # Feed is bounded by the request, the buffers, the batch cap, AND -- new with
        # the rated-capacity invariant -- the throughput the step is long enough for.
        spent_before = s.wrs_processed_this_step_l
        urine_available = s.urine_buffer_l
        rated, elapsed, rated_this_step = self._rated_bounds(
            c.wrs_capacity_l_day, c.wrs_operation_seconds, spent_before
        )
        feed_cap = min(c.wrs_max_feed_l_per_operation, rated)
        urine_feed = min(requested_urine_l, urine_available, feed_cap)
        remaining_capacity = feed_cap - urine_feed
        grey_feed = min(s.grey_water_l, max(0.0, remaining_capacity))

        urine_recovered = urine_feed * c.wrs_urine_recovery
        grey_recovered = grey_feed * c.wrs_grey_recovery
        loss = (urine_feed - urine_recovered) + (grey_feed - grey_recovered)

        s.urine_buffer_l -= urine_feed
        s.grey_water_l -= grey_feed
        s.product_water_l += urine_recovered + grey_recovered
        s.total_wrs_brine_loss_l += loss
        s.total_wrs_recovered_water_l += urine_recovered + grey_recovered
        s.wrs_processed_this_step_l += urine_feed + grey_feed
        self._check_invariants()

        # WRS used to report neither of these, so the scorecard's D axis had nothing
        # to read for water (STATE-2026-08-27). A rating gives it something to say.
        tol = c.invariant_tolerance
        limited_by: List[str] = []
        if urine_feed < requested_urine_l - tol:
            if abs(urine_feed - urine_available) <= tol:
                limited_by.append("urine_buffer")
            if abs(urine_feed - feed_cap) <= tol:
                if rated <= c.wrs_max_feed_l_per_operation - tol:
                    # A step already part-spent is a different diagnosis from a
                    # single action meeting the rating, and the actor can act on it.
                    limited_by.append(
                        "rated_step_capacity" if spent_before > tol else "wrs_capacity"
                    )
                else:
                    limited_by.append("wrs_batch_cap")
        return {
            "urine_feed_l": urine_feed,
            "grey_feed_l": grey_feed,
            "urine_recovered_l": urine_recovered,
            "grey_recovered_l": grey_recovered,
            "recovered_water_l": urine_recovered + grey_recovered,
            "brine_loss_l": loss,
            "processed_feed_l": urine_feed + grey_feed,
            "rated_step_l": rated_this_step,
            "elapsed_seconds": elapsed,
            "has_feed": (urine_feed + grey_feed) > 0.0,
            "fully_satisfied": not limited_by,
            "limited_by": limited_by or None,
        }

    # ------------------------------------------------------------------ #
    # resource services (payout / intake from existing inventory)
    # ------------------------------------------------------------------ #
    def request_o2(self, amount_kg: float) -> float:
        s = self.state
        granted = min(s.available_o2_kg, amount_kg)
        s.available_o2_kg -= granted
        s.total_o2_delivered_kg += granted
        self._check_invariants()
        return granted

    def request_co2(self, amount_kg: float) -> float:
        s = self.state
        granted = min(s.captured_co2_kg, amount_kg)
        s.captured_co2_kg -= granted
        s.total_co2_delivered_kg += granted
        self._check_invariants()
        return granted

    def request_product_water(self, liters: float) -> float:
        s = self.state
        granted = min(s.product_water_l, liters)
        s.product_water_l -= granted
        s.total_product_water_delivered_l += granted
        self._check_invariants()
        return granted

    def submit_grey_water(self, liters: float) -> float:
        s = self.state
        s.grey_water_l += liters
        s.total_external_grey_water_submitted_l += liters
        self._check_invariants()
        return liters

    # ------------------------------------------------------------------ #
    def _check_invariants(self) -> None:
        eps = self.config.clamp_epsilon
        s = self.state
        for f in fields(s):
            value = getattr(s, f.name)
            if not math.isfinite(value):
                raise PlantInvariantError(f"{f.name} is not finite: {value!r}")
        for name in _INVENTORY_FIELDS:
            value = getattr(s, name)
            if value < 0.0:
                if value >= -eps:
                    setattr(s, name, 0.0)
                else:
                    raise PlantInvariantError(f"{name} went negative: {value!r}")


__all__ = [
    "PlantModel",
    "PlantState",
    "PlantInvariantError",
    "per_interval",
    "water_l_to_kg",
    "water_kg_to_l",
]
