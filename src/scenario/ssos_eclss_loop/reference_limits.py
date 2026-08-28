"""Environmental limits taken from published NASA standards, with provenance.

The limits a run is scored against must be ones the run cannot move. That is
not a stylistic preference here: ``--apply-proposals`` accepts
``set_parameter`` targets under ``thresholds.*``, and
``compute_eclss_storage_health`` reads exactly those keys -- so an agent's
design proposal can lower the bar it is about to be measured against. A real
proposal in ``2026-08-ersi-v2/noise_t00__r1`` does this: it moves
``product_water_low_l`` from 50.0 to 40.0 on a run whose final reserve was
44.07 L, turning ``warning`` into ``safe`` with no physical change at all.

Sourcing the limits from a published standard closes that hole completely.
Nothing in this repository can edit NASA-STD-3001.

Every value carries where it came from. A limit whose ``source`` is
:data:`UNSOURCED` is a named gap, not a default -- it is excluded from scoring
and reported as missing, because a plausible number with no document behind it
is worse than no number: it scores runs while looking as if it were grounded.

Units follow the standards (mmHg for partial pressures, kPa for total
pressure), not the simulator (kg). :func:`ppco2_mmhg` bridges the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

# Molar mass of CO2 [g/mol] -- same source as plant_sim.stoichiometry.
MW_CO2_G_PER_MOL = 44.0095
GAS_CONSTANT_J_PER_MOL_K = 8.314462618
PA_PER_MMHG = 133.322387415

UNSOURCED = "unsourced"


@dataclass(frozen=True)
class Limit:
    """One environmental limit and the document it came from."""

    value: Optional[float]
    unit: str
    label: str
    source: str
    requirement: str = ""
    revision: str = ""
    quote: str = ""
    url: str = ""
    note: str = ""

    @property
    def is_sourced(self) -> bool:
        return self.source != UNSOURCED and self.value is not None

    def to_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "sourced": self.is_sourced,
        }
        for key in ("requirement", "revision", "quote", "url", "note"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload


TB004 = "OCHMO-TB-004 Carbon Dioxide (CO2), Rev D, 10-Mar-26"
TB004_URL = "https://www.nasa.gov/wp-content/uploads/2023/12/ochmo-tb-004-carbon-dioxide.pdf"
TB003 = "OCHMO-TB-003 Habitable Atmosphere, Rev A, 30-Nov-2023"
TB003_URL = "https://www.nasa.gov/wp-content/uploads/2023/12/ochmo-tb-003-habitable-atmosphere.pdf"
TB027 = "OCHMO-TB-027 Water -- Human, Rev C, 11/29/2023"
TB027_URL = "https://www.nasa.gov/wp-content/uploads/2023/12/ochmo-tb-027-water.pdf"


# --------------------------------------------------------------------------- #
# CO2 -- the one species this simulator models as cabin atmosphere
# --------------------------------------------------------------------------- #
CO2_NOMINAL = Limit(
    value=3.0,
    unit="mmHg",
    label="ppCO2 nominal (1-hour average)",
    source=TB004,
    requirement="NASA-STD-3001 Volume 2 [V2 6004]",
    revision="Volume 2, Rev E",
    quote=(
        "limits the average 1-hour CO2 partial pressure (ppCO2) in the "
        "habitable volume to no more than 3 mmHg"
    ),
    url=TB004_URL,
    note=(
        "Previous requirements accepted 3.8 to 7.5 mmHg; lowered to 3 mmHg on "
        "observed operational and research data."
    ),
)

CO2_OFF_NOMINAL = Limit(
    value=15.0,
    unit="mmHg",
    label="ISS off-nominal ppCO2",
    source=TB004,
    revision="Rev D",
    quote="ISS Off-Nominal ppCO2 Level",
    url=TB004_URL,
    note="2% CO2. Headache and exertional dyspnea start at this level.",
)

CO2_EMERGENCY = Limit(
    value=20.0,
    unit="mmHg",
    label="ISS emergency ppCO2",
    source=TB004,
    revision="Rev D",
    quote="ISS Emergency ppCO2 Level",
    url=TB004_URL,
    note="Also the Orlan EVA termination limit.",
)

#: Bands in ascending severity. Scoring integrates exposure above each.
CO2_BANDS = (CO2_NOMINAL, CO2_OFF_NOMINAL, CO2_EMERGENCY)


# --------------------------------------------------------------------------- #
# O2 and water -- named gaps, deliberately not filled
# --------------------------------------------------------------------------- #
#: [V2 6003] is written in PIO2, inspired oxygen partial pressure, which
#: subtracts the lung's water vapour before taking the O2 fraction. [V2 6004]
#: for CO2 is a dry-gas partial pressure, so the two are not interchangeable.
PH2O_LUNG_MMHG = 47.0
#: Cabin total pressure the PIO2 conversion assumes. Holds while nothing leaks;
#: a leak (roadmap R4) moves it, and N2 has to be modelled before then.
ASSUMED_TOTAL_PRESSURE_MMHG = 760.0

MW_O2_G_PER_MOL = 31.998


def pio2_mmhg(ppo2_mmhg: float) -> float:
    """Dry-gas ppO2 -> inspired PIO2, at the assumed cabin total pressure.

    PIO2 = (PB - 47) * FIO2 and FIO2 = ppO2 / PB, so PIO2 = ppO2 * (1 - 47/PB).
    """
    pb = ASSUMED_TOTAL_PRESSURE_MMHG
    return float(ppo2_mmhg) * (pb - PH2O_LUNG_MMHG) / pb


def ppo2_mmhg(cabin_o2_kg: float, habitat: Optional["Habitat"]) -> float:
    """Cabin O2 mass to dry-gas partial pressure, by the ideal gas law."""
    if habitat is None:
        raise HabitatUnknown("taking a partial pressure needs a habitat volume")
    moles = float(cabin_o2_kg) * 1000.0 / MW_O2_G_PER_MOL
    pascals = moles * GAS_CONSTANT_J_PER_MOL_K * habitat.temperature_k / habitat.volume_m3
    return pascals / PA_PER_MMHG


def o2_kg_for_pio2(pio2: float, habitat: Optional["Habitat"]) -> float:
    """Cabin O2 mass that reads as ``pio2``, for expressing a limit in kg."""
    if habitat is None:
        raise HabitatUnknown("converting a limit to kg needs a habitat volume")
    pb = ASSUMED_TOTAL_PRESSURE_MMHG
    ppo2 = float(pio2) * pb / (pb - PH2O_LUNG_MMHG)
    pascals = ppo2 * PA_PER_MMHG
    moles = pascals * habitat.volume_m3 / (GAS_CONSTANT_J_PER_MOL_K * habitat.temperature_k)
    return moles * MW_O2_G_PER_MOL / 1000.0


O2_NORMOXIA_FLOOR = Limit(
    value=145.0,
    unit="mmHg PIO2",
    label="normoxia target range, lower edge",
    source=TB003,
    requirement="NASA-STD-3001 Volume 2 [V2 6003]",
    revision="Volume 2, Rev D, Table 6.2-1",
    quote=(
        "The system shall maintain inspired oxygen partial pressure (PIO2) in "
        "accordance with Table 6.2-1. Normoxia Target Range 145-155 mmHg "
        "(2.80-3.00 psia), acceptable duration Indefinite"
    ),
    url=TB003_URL,
    note=(
        "Leaving this band is a breach of [V2 6003]; it is not a way to die. "
        "The table calls 145-127 mmHg mild hypoxia, 'Indefinite with "
        "monitoring'. Occupant loss is scored from the floor below, not here."
    ),
)

O2_MILD_HYPOXIA_FLOOR = Limit(
    value=127.0,
    unit="mmHg PIO2",
    label="mild hypoxia lower limit",
    source=TB003,
    requirement="NASA-STD-3001 Volume 2 [V2 6003]",
    revision="Volume 2, Rev D, Table 6.2-1",
    quote=(
        "Mild Hypoxia Lower Limit 127 mmHg (2.46 psia), acceptable duration "
        "Indefinite with monitoring. 1-hour time-weighted average with an "
        "absolute lower limit for the minimum hypoxia range of 122 mmHg"
    ),
    url=TB003_URL,
)

#: Ascending severity, like CO2_BANDS: the normoxia floor is the breach, the
#: mild hypoxia floor is where occupant loss starts.
O2_BANDS = (O2_NORMOXIA_FLOOR, O2_MILD_HYPOXIA_FLOOR)

O2_PARTIAL_PRESSURE = Limit(
    value=None,
    unit="mmHg",
    label="ppO2 range for crew exposure",
    source=UNSOURCED,
    requirement="NASA-STD-3001 Volume 2 [V2 6003]",
    note=(
        "Two separate problems, and the second is the blocking one. (1) The "
        "numeric range was not read off a primary document, so no value is "
        "recorded here. (2) More fundamentally, plant_sim's o2_storage_kg is "
        "available_o2_kg -- a supply inventory that OGS fills and the crew "
        "draws from -- not the O2 in the cabin atmosphere. There is no cabin "
        "gas state to take a partial pressure of, so [V2 6003] cannot be "
        "evaluated against this model no matter which number is supplied. "
        "Superseded 2026-08-28: plant_sim now holds cabin_o2_kg, so [V2 6003] "
        "is scored through O2_NORMOXIA_FLOOR and O2_MILD_HYPOXIA_FLOOR. Kept "
        "as the record of what the gap was and what closing it required."
    ),
)

POTABLE_WATER_QUANTITY = Limit(
    value=2.5,
    unit="L/crew-day",
    label="potable water for hydration, minimum allocation",
    source=TB027,
    requirement="NASA-STD-3001 Volume 2 [V2 6109]",
    revision="Volume 2, Rev D (table reproduced in TB-027 from Rev C Table 4)",
    quote=(
        "[V2 6109] Water Quantity: The system shall provide a minimum water "
        "quantity as specified in Table 6.3-1 -- Water Quantities and "
        "Temperatures, for the expected needs of each mission, which are "
        "considered mutually independent. Table: Potable Water for Hydration, "
        "Minimum 2.5 L (84.5 fl oz) per crewmember per day"
    ),
    url=TB027_URL,
    note=(
        "A provision rate, not a consumption rate, and the two are different "
        "quantities. plant_sim's crew draws 2.28 L/crew-day, which is fixed by "
        "a mass balance the config enforces (urine 1.50 + condensate 0.75 + "
        "unrecoverable 0.03 must equal potable intake). 2.5 is what the system "
        "must be able to supply; 2.28 is what passes through the crew. The "
        "standard sets no reserve horizon -- it says 'for the expected needs "
        "of each mission' -- so the reserve is reported as crew-days at this "
        "allocation rather than against an invented floor.\n"
        "The table's other two rows carry no scalar for this scenario: personal "
        "hygiene is 'mission dependent', and eye irrigation is 500 mL per "
        "crewmember, a one-off rather than a daily rate."
    ),
)

TOTAL_PRESSURE_INDEFINITE_EXPOSURE = Limit(
    value=None,
    unit="kPa",
    label="total pressure, indefinite crew exposure",
    source=TB003,
    requirement="NASA-STD-3001 Volume 2 [V2 6006]",
    revision="Volume 2, Rev D",
    quote=(
        "maintain the pressure to which the crew is exposed to between "
        "34.5 kPa < pressure <= 103 kPa (5.0 psia < pressure <= 15.0 psia) "
        "for indefinite human exposure without measurable impairments to "
        "health or performance"
    ),
    url=TB003_URL,
    note=(
        "Recorded for the habitat assumption below, not scored: the plant "
        "models no total pressure. Value is a range, so the scalar field "
        "stays None."
    ),
)


# --------------------------------------------------------------------------- #
# habitat assumption -- the one number here that is NOT from a standard
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Habitat:
    """Cabin gas volume and temperature, needed to turn kg into mmHg.

    This is an **assumption, not a measurement**, and it is the weakest link
    in every ppCO2 figure downstream: ppCO2 scales as 1/volume, so a habitat
    twice as large halves every number scored against [V2 6004].

    ``plant_sim`` carries no cabin volume (``config.py`` has crew_size and
    inventories, nothing geometric), so there is no value to read off the run.
    It is therefore recorded in every scored artifact rather than hidden in a
    default, and a program that knows its own habitat should pass its own.

    There is deliberately **no default volume**. Nobody has chosen the habitat
    this scenario models: ``scenario.yaml`` names none, ``plant_sim/config.py``
    carries no geometry, and the docs do not say. A default here would put an
    unchosen number into every scored artifact looking exactly like a chosen
    one -- and since ppCO2 scales as 1/volume, wrongly by whatever factor the
    guess was off. Callers that have no volume get no mmHg, which is the honest
    answer, rather than a plausible one.

    For reference, the thresholds already in ``scenario.yaml`` are
    self-consistent with roughly 61 m3 -- a single pressurised module, not a
    station. That is a hint about what the scenario has been implicitly
    modelling, not a value to adopt without checking.
    """

    volume_m3: float
    temperature_k: float = 295.15
    source: str = "caller-supplied"
    note: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "volume_m3": self.volume_m3,
            "temperature_k": self.temperature_k,
            "source": self.source,
            "note": self.note,
        }


#: The habitat this scenario is scored in, chosen 2026-08-24.
#:
#: A modelling choice, not a measured module, and it is the weakest number in
#: any ppCO2 figure downstream -- ppCO2 scales as 1/volume, so it is recorded
#: in every scored artifact rather than assumed.
#:
#: It was chosen because it is the only volume tried that makes the scenario's
#: existing thresholds a coherent alarm ladder *below* the standard rather than
#: above it. At 388 m3, with the operating points measured over 270 runs:
#:
#:     1.40 mmHg  nominal (1.3 kg)          comfortably under the limit
#:     1.62 mmHg  co2_storage_high_kg       "start acting"
#:     2.37 mmHg  co2_storage_critical_kg   "escalate"
#:     3.00 mmHg  NASA-STD-3001 [V2 6004]   "the standard is now violated"
#:     3.45 mmHg  worst peak under failure  crossed only when something breaks
#:
#: The 61.3 m3 that the critical threshold back-solves to was rejected: it puts
#: nominal operation at 8.9 mmHg, already past the standard, with both alarms
#: firing long after the limit rather than before it -- and it is circular, a
#: volume derived from the threshold it is then said to corroborate. It also
#: only fits one of the two thresholds; at 61.3 m3 co2_storage_high_kg lands on
#: 10.23 mmHg, which corresponds to nothing.
#:
#: Replace it when the modelled module is known. Nothing else changes: the
#: volume converts kg to mmHg on the output side and touches no physics, so
#: existing runs can simply be re-scored.
SCENARIO_HABITAT = Habitat(
    volume_m3=388.0,
    source="modelling choice (2026-08-24)",
    note=(
        "Chosen so the scenario's own thresholds sit below NASA-STD-3001 "
        "[V2 6004] rather than above it; ISS USOS habitable volume is the "
        "nearest real reference. Not a measured module."
    ),
)


class HabitatUnknown(RuntimeError):
    """Asked for a partial pressure with no habitat to take it in."""


def ppco2_mmhg(cabin_co2_kg: float, habitat: Optional[Habitat]) -> float:
    """Cabin CO2 mass to partial pressure, by the ideal gas law.

    The standard is written in mmHg and the simulator in kg; nothing can be
    compared until one is expressed in the other. p = nRT/V, with n from the
    CO2 molar mass.

    Raises :class:`HabitatUnknown` when no habitat is supplied, rather than
    falling back to a volume nobody chose.
    """
    if habitat is None:
        raise HabitatUnknown(
            "ppCO2 needs a habitat volume; none is recorded in the scenario. "
            "Pass Habitat(volume_m3=...) once the modelled module is decided."
        )
    moles = (float(cabin_co2_kg) * 1000.0) / MW_CO2_G_PER_MOL
    pascals = moles * GAS_CONSTANT_J_PER_MOL_K * habitat.temperature_k / habitat.volume_m3
    return pascals / PA_PER_MMHG


def co2_kg_for_ppco2(ppco2: float, habitat: Optional[Habitat]) -> float:
    """Inverse of :func:`ppco2_mmhg`, for expressing a limit in the run's units."""
    if habitat is None:
        raise HabitatUnknown("converting a limit to kg needs a habitat volume")
    pascals = float(ppco2) * PA_PER_MMHG
    moles = pascals * habitat.volume_m3 / (GAS_CONSTANT_J_PER_MOL_K * habitat.temperature_k)
    return moles * MW_CO2_G_PER_MOL / 1000.0


def provenance(habitat: Optional[Habitat] = None) -> Dict[str, object]:
    """Everything a scored artifact needs to say where its yardstick came from.

    A ``habitat`` of None is recorded as such: the artifact then says the
    limits could not be expressed in the run's units, which is a reviewable
    statement. A default would have said nothing.
    """
    return {
        "co2_bands": [limit.to_dict() for limit in CO2_BANDS],
        "o2_bands": [limit.to_dict() for limit in O2_BANDS],
        "potable_water": POTABLE_WATER_QUANTITY.to_dict(),
        "habitat": habitat.to_dict() if habitat is not None else {"status": "not chosen"},
        "unsourced": [
            limit.to_dict()
            for limit in (TOTAL_PRESSURE_INDEFINITE_EXPOSURE,)
            if not limit.is_sourced
        ],
    }
