"""Band-dwell occupant survival (scenario policy, not plant physics)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Tuple

from environment.protocol import HealthStatus

CAUSE_PRIORITY: Tuple[str, ...] = (
    "co2_critical",
    "co2_warning",
    "o2_critical",
    "water_critical",
    "o2_warning",
    "water_warning",
)


@dataclass
class ResourceDwellTable:
    warning_steps: int = 2
    warning_loss: int = 1
    critical_steps: int = 1
    critical_loss: int = 1


@dataclass
class Co2DwellTable:
    warning_steps: int = 2
    warning_divisor: int = 4
    critical_steps: int = 2
    critical_divisor: int = 2


@dataclass
class SurvivalStreaks:
    o2_warning: int = 0
    o2_critical: int = 0
    water_warning: int = 0
    water_critical: int = 0
    co2_warning: int = 0
    co2_critical: int = 0
    co2_warning_fired: bool = False
    co2_critical_fired: bool = False

    def copy(self) -> "SurvivalStreaks":
        return replace(self)


def resolve_survival_bands(
    plant_sim: Mapping[str, Any] | None,
    thresholds: Mapping[str, Any],
) -> Dict[str, Any]:
    """Band edges attrition reads, which need not be the operational alarms.

    They were the same keys, deliberately: an operator's alarm and the edge of
    the band that kills are the same physical situation, and one number said
    both. But ``thresholds.*`` is reachable by a ``set_parameter`` proposal, so
    a proposal that raised an alarm above the trajectory stopped the run from
    ever entering a band -- and deleted the deaths it had just caused (EXP-010).

    ``plant_sim.survival.bands`` is outside ALLOWED_SET_PARAMETER_TARGETS, so a
    band written there cannot be moved by a proposal. Anything omitted falls
    back to the operational threshold, which keeps every existing config
    behaving exactly as before.
    """
    bands = dict(((plant_sim or {}).get("survival") or {}).get("bands") or {})
    resolved = dict(thresholds)
    resolved.update({key: value for key, value in bands.items() if value is not None})
    return resolved


@dataclass
class SurvivalDwellPolicy:
    enabled: bool = False
    o2: ResourceDwellTable = field(default_factory=lambda: ResourceDwellTable(critical_loss=2))
    water: ResourceDwellTable = field(default_factory=ResourceDwellTable)
    co2: Co2DwellTable = field(default_factory=Co2DwellTable)
    lost_by_cause: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, plant_sim: Mapping[str, Any] | None) -> "SurvivalDwellPolicy":
        survival = dict((plant_sim or {}).get("survival") or {})
        o2 = dict(survival.get("o2") or {})
        water = dict(survival.get("water") or {})
        co2 = dict(survival.get("co2") or {})
        return cls(
            enabled=bool(survival.get("enabled", False)),
            o2=ResourceDwellTable(
                warning_steps=int(o2.get("warning_steps", 2)),
                warning_loss=int(o2.get("warning_loss", 1)),
                critical_steps=int(o2.get("critical_steps", 1)),
                critical_loss=int(o2.get("critical_loss", 2)),
            ),
            water=ResourceDwellTable(
                warning_steps=int(water.get("warning_steps", 2)),
                warning_loss=int(water.get("warning_loss", 1)),
                critical_steps=int(water.get("critical_steps", 1)),
                critical_loss=int(water.get("critical_loss", 1)),
            ),
            co2=Co2DwellTable(
                warning_steps=int(co2.get("warning_steps", 2)),
                warning_divisor=int(co2.get("warning_divisor", 4)),
                critical_steps=int(co2.get("critical_steps", 2)),
                critical_divisor=int(co2.get("critical_divisor", 2)),
            ),
        )

    def apply_dwell(
        self,
        alive: int,
        health: Mapping[str, Any],
        streaks: SurvivalStreaks,
    ) -> Tuple[int, int, list[str], SurvivalStreaks, Dict[str, int]]:
        """Apply band-dwell losses. Returns new_alive, lost, limiting, streaks, by_cause."""
        alive = max(0, int(alive))
        streaks = streaks.copy()
        requests: Dict[str, int] = {}

        o2_status = str(health.get("o2_status") or "")
        water_status = str(health.get("water_status") or "")
        co2_status = str(health.get("co2_status") or "")

        _accumulate_o2_water(
            status=o2_status,
            table=self.o2,
            warning_attr="o2_warning",
            critical_attr="o2_critical",
            warning_cause="o2_warning",
            critical_cause="o2_critical",
            streaks=streaks,
            requests=requests,
        )
        _accumulate_o2_water(
            status=water_status,
            table=self.water,
            warning_attr="water_warning",
            critical_attr="water_critical",
            warning_cause="water_warning",
            critical_cause="water_critical",
            streaks=streaks,
            requests=requests,
        )
        _accumulate_co2(
            status=co2_status,
            table=self.co2,
            alive=alive,
            streaks=streaks,
            requests=requests,
        )

        lost = min(alive, sum(requests.values()))
        remaining = lost
        by_cause: Dict[str, int] = {}
        limiting = [cause for cause in CAUSE_PRIORITY if cause in requests]
        for cause in CAUSE_PRIORITY:
            want = int(requests.get(cause) or 0)
            if want <= 0:
                continue
            take = min(want, remaining)
            if take > 0:
                by_cause[cause] = take
                remaining -= take
                self.lost_by_cause[cause] = int(self.lost_by_cause.get(cause, 0)) + take
        return alive - lost, lost, limiting, streaks, by_cause


def _accumulate_o2_water(
    *,
    status: str,
    table: ResourceDwellTable,
    warning_attr: str,
    critical_attr: str,
    warning_cause: str,
    critical_cause: str,
    streaks: SurvivalStreaks,
    requests: Dict[str, int],
) -> None:
    if status == HealthStatus.CRITICAL.value:
        setattr(streaks, warning_attr, 0)
        setattr(streaks, critical_attr, int(getattr(streaks, critical_attr)) + 1)
        if int(getattr(streaks, critical_attr)) >= table.critical_steps:
            if table.critical_loss > 0:
                requests[critical_cause] = table.critical_loss
            setattr(streaks, critical_attr, 0)
        return
    setattr(streaks, critical_attr, 0)
    if status == HealthStatus.WARNING.value:
        setattr(streaks, warning_attr, int(getattr(streaks, warning_attr)) + 1)
        if int(getattr(streaks, warning_attr)) >= table.warning_steps:
            if table.warning_loss > 0:
                requests[warning_cause] = table.warning_loss
            setattr(streaks, warning_attr, 0)
        return
    setattr(streaks, warning_attr, 0)


def _accumulate_co2(
    *,
    status: str,
    table: Co2DwellTable,
    alive: int,
    streaks: SurvivalStreaks,
    requests: Dict[str, int],
) -> None:
    if status == HealthStatus.CRITICAL.value:
        streaks.co2_warning = 0
        streaks.co2_warning_fired = False
        streaks.co2_critical += 1
        if streaks.co2_critical >= table.critical_steps and not streaks.co2_critical_fired:
            lost = alive // max(1, table.critical_divisor)
            if lost > 0:
                requests["co2_critical"] = lost
            streaks.co2_critical_fired = True
            streaks.co2_critical = 0
        return
    streaks.co2_critical = 0
    streaks.co2_critical_fired = False
    if status == HealthStatus.WARNING.value:
        streaks.co2_warning += 1
        if streaks.co2_warning >= table.warning_steps and not streaks.co2_warning_fired:
            lost = alive // max(1, table.warning_divisor)
            if lost > 0:
                requests["co2_warning"] = lost
            streaks.co2_warning_fired = True
            streaks.co2_warning = 0
        return
    streaks.co2_warning = 0
    streaks.co2_warning_fired = False


PHYSICS_LIMITING = {
    "o2": "o2_physics",
    "water": "water_physics",
    "co2": "co2_physics",
}


def map_physics_limiting(limiting: list[str]) -> list[str]:
    return [PHYSICS_LIMITING.get(item, item) for item in limiting]


__all__ = [
    "CAUSE_PRIORITY",
    "resolve_survival_bands",
    "PHYSICS_LIMITING",
    "SurvivalDwellPolicy",
    "SurvivalStreaks",
    "map_physics_limiting",
]
