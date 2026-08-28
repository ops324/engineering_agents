"""Tests for the standard-derived limits.

Most of these pin *provenance*, not arithmetic. The point of the module is
that a scored artifact can say which document its yardstick came from, and
that a value with no document behind it never gets used -- so the tests that
matter are the ones that fail if a number quietly loses its source, or if an
unsourced placeholder starts being treated as a limit.
"""

from __future__ import annotations

import math

import pytest

from scenario.ssos_eclss_loop.reference_limits import (
    CO2_BANDS,
    CO2_EMERGENCY,
    CO2_NOMINAL,
    CO2_OFF_NOMINAL,
    Habitat,
    HabitatUnknown,
    O2_PARTIAL_PRESSURE,
    POTABLE_WATER_QUANTITY,
    UNSOURCED,
    co2_kg_for_ppco2,
    ppco2_mmhg,
    provenance,
)

# The volume scenario.yaml's own critical threshold is self-consistent with.
IMPLIED = Habitat(volume_m3=61.3)


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #
def test_the_nominal_co2_limit_is_the_published_one():
    assert CO2_NOMINAL.value == 3.0
    assert CO2_NOMINAL.unit == "mmHg"
    assert "[V2 6004]" in CO2_NOMINAL.requirement


def test_every_scored_band_names_a_document():
    """A limit with no source must never reach scoring."""
    for limit in CO2_BANDS:
        assert limit.is_sourced, limit.label
        assert limit.source != UNSOURCED
        assert limit.url, limit.label
        assert limit.quote, limit.label


def test_the_bands_ascend_in_severity():
    values = [limit.value for limit in CO2_BANDS]
    assert values == sorted(values)
    assert (CO2_NOMINAL.value, CO2_OFF_NOMINAL.value, CO2_EMERGENCY.value) == (3.0, 15.0, 20.0)


def test_unsourced_limits_carry_no_value():
    """A plausible number with no document is worse than none: it scores runs
    while looking grounded."""
    for limit in (O2_PARTIAL_PRESSURE,):
        assert limit.value is None
        assert not limit.is_sourced
        assert limit.requirement, "an unsourced limit must still name what is missing"


def test_potable_water_is_sourced_and_says_which_quantity_it_is():
    """[V2 6109] is a provision rate, and plant_sim's 2.28 L/crew-day is a
    throughput held by a mass balance. Scoring one against the other would be a
    category error, so the limit carries the distinction it is read with."""
    assert POTABLE_WATER_QUANTITY.is_sourced
    assert POTABLE_WATER_QUANTITY.value == 2.5
    assert POTABLE_WATER_QUANTITY.unit == "L/crew-day"
    assert "[V2 6109]" in POTABLE_WATER_QUANTITY.quote
    assert "2.5 L" in POTABLE_WATER_QUANTITY.quote
    assert POTABLE_WATER_QUANTITY.url.startswith("https://www.nasa.gov/")
    assert "provision rate, not a consumption rate" in POTABLE_WATER_QUANTITY.note
    assert "2.28" in POTABLE_WATER_QUANTITY.note


def test_o2_records_that_the_model_cannot_be_scored_at_all():
    """Not just a missing number: o2_storage_kg is a supply inventory, so
    there is no cabin gas to take a partial pressure of."""
    assert "supply inventory" in O2_PARTIAL_PRESSURE.note
    assert "cabin atmosphere" in O2_PARTIAL_PRESSURE.note


def test_provenance_lists_the_gaps_as_well_as_the_limits():
    payload = provenance(IMPLIED)
    assert len(payload["co2_bands"]) == 3
    assert payload["unsourced"], "a report that hides its gaps reads as complete"
    labels = {entry["label"] for entry in payload["unsourced"]}
    assert "ppO2 range for crew exposure" in labels


# --------------------------------------------------------------------------- #
# the habitat, which is the weakest link in every mmHg figure
# --------------------------------------------------------------------------- #
def test_a_habitat_must_be_chosen_not_defaulted():
    """Nobody has picked the module this scenario models; a default would put
    an unchosen number into every artifact looking like a chosen one."""
    with pytest.raises(TypeError):
        Habitat()  # type: ignore[call-arg]


def test_no_habitat_refuses_rather_than_guesses():
    with pytest.raises(HabitatUnknown):
        ppco2_mmhg(2.2, None)
    with pytest.raises(HabitatUnknown):
        co2_kg_for_ppco2(3.0, None)


def test_provenance_says_so_when_no_habitat_was_chosen():
    assert provenance(None)["habitat"] == {"status": "not chosen"}


def test_the_co2_limit_itself_needs_no_habitat():
    """3 mmHg is 3 mmHg regardless of the volume; only the kg bridge needs it."""
    assert CO2_NOMINAL.is_sourced
    assert provenance(None)["co2_bands"][0]["value"] == 3.0


# --------------------------------------------------------------------------- #
# the gas law
# --------------------------------------------------------------------------- #
def test_ppco2_and_its_inverse_round_trip():
    for kg in (0.5, 1.3, 2.2, 3.49):
        assert co2_kg_for_ppco2(ppco2_mmhg(kg, IMPLIED), IMPLIED) == pytest.approx(kg)


def test_ppco2_scales_inversely_with_volume():
    """The reason the habitat is load-bearing: double the module, halve every
    number scored against [V2 6004]."""
    small = ppco2_mmhg(2.2, Habitat(volume_m3=61.3))
    large = ppco2_mmhg(2.2, Habitat(volume_m3=122.6))
    assert large == pytest.approx(small / 2.0)


def test_the_scenarios_critical_threshold_lands_on_the_iss_off_nominal_level():
    """Recorded because it is how the implied volume was recovered: 2.2 kg,
    the scenario's co2_storage_critical_kg, is 15 mmHg in a 61.3 m3 module --
    exactly the ISS off-nominal ppCO2 level."""
    assert ppco2_mmhg(2.2, IMPLIED) == pytest.approx(CO2_OFF_NOMINAL.value, abs=0.05)


def test_ppco2_is_zero_for_an_empty_cabin_and_finite_otherwise():
    assert ppco2_mmhg(0.0, IMPLIED) == 0.0
    assert math.isfinite(ppco2_mmhg(1000.0, IMPLIED))
