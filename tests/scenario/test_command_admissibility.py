"""The deterministic gate: structural refusals only, scarcity left to the plant."""

from __future__ import annotations

import math

import pytest

from scenario.agents.command_admissibility import (
    OPERATIONAL_KINDS,
    SCENARIO_CONTROL_KINDS,
    is_command_admissible,
)
from scenario.agents.eclss_loop_types import EclssOperationalCommand, StepEclssOutcome
from scenario.agents.ssos_eclss_loop_team import SsosEclssLoopTeam

VALID = {
    "air_revitalisation": {"initial_co2_mass": 1.8},
    "oxygen_generation": {"input_water_mass": 0.15},
    "water_recovery": {"urine_volume": 2.0},
    "request_co2": {"amount": 0.025},
    "request_o2": {"amount": 0.05},
}


# --- what must pass -------------------------------------------------------


@pytest.mark.parametrize("kind,payload", sorted(VALID.items()))
def test_default_shaped_commands_are_admissible(kind, payload):
    assert is_command_admissible(kind, payload).admissible


def test_every_operational_kind_has_a_valid_example():
    # Guards against a kind being added without anyone deciding its fields.
    assert set(VALID) == set(OPERATIONAL_KINDS)


def test_requesting_more_than_exists_is_admissible():
    """Saturation is the plant's job.

    run_ogs / run_wrs / request_* all clamp with min(); asking for more than is
    on hand is how a caller says "give me what you can". An earlier version of
    this gate refused these and blocked 45 legitimate water-recovery commands
    in a 50-step run.
    """
    assert is_command_admissible("oxygen_generation", {"input_water_mass": 10_000.0}).admissible
    assert is_command_admissible("water_recovery", {"urine_volume": 10_000.0}).admissible
    assert is_command_admissible("request_co2", {"amount": 10_000.0}).admissible


def test_same_kind_twice_is_admissible():
    """max_actions_per_step exists so several representatives can act."""
    first = is_command_admissible("oxygen_generation", VALID["oxygen_generation"])
    second = is_command_admissible("oxygen_generation", VALID["oxygen_generation"])
    assert first.admissible and second.admissible


def test_extra_keyword_arguments_are_ignored():
    """A caller still passing telemetry must not break."""
    verdict = is_command_admissible(
        "request_o2", {"amount": 0.05}, telemetry={"o2_storage_kg": 0.0}
    )
    assert verdict.admissible


# --- authority ------------------------------------------------------------


def test_scenario_control_is_never_admissible():
    """An agent that can toggle its own fault injection is grading its own exam."""
    for kind in SCENARIO_CONTROL_KINDS:
        verdict = is_command_admissible(kind, {"subsystem": "ars", "enabled": False})
        assert not verdict.admissible
        assert "KIND_NOT_OPERATIONAL" in verdict.rule_ids


def test_unknown_kind_is_refused():
    verdict = is_command_admissible("vent_the_airlock", {"amount": 1.0})
    assert not verdict.admissible
    assert "KIND_UNKNOWN" in verdict.rule_ids


# --- malformed payloads ---------------------------------------------------


def test_empty_payload_is_refused():
    assert "PAYLOAD_EMPTY" in is_command_admissible("request_o2", {}).rule_ids
    assert "PAYLOAD_EMPTY" in is_command_admissible("request_o2", None).rule_ids


def test_non_object_payload_is_refused():
    assert "PAYLOAD_NOT_OBJECT" in is_command_admissible("request_o2", [1, 2]).rule_ids


def test_unknown_field_is_refused_not_silently_dropped():
    # Applying the command minus the part the agent asked for is worse than
    # refusing it.
    verdict = is_command_admissible("request_o2", {"amount": 0.05, "urgency": "high"})
    assert not verdict.admissible
    assert "FIELD_UNKNOWN" in verdict.rule_ids


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_refused(value):
    # NaN/inf would propagate silently through the mass balance.
    verdict = is_command_admissible("oxygen_generation", {"input_water_mass": value})
    assert not verdict.admissible
    assert "FIELD_NOT_FINITE" in verdict.rule_ids


def test_negative_values_are_refused():
    verdict = is_command_admissible("water_recovery", {"urine_volume": -1.0})
    assert "FIELD_NEGATIVE" in verdict.rule_ids


def test_zero_is_refused_where_it_means_nothing():
    assert "FIELD_NOT_POSITIVE" in is_command_admissible(
        "request_co2", {"amount": 0.0}
    ).rule_ids


def test_zero_is_allowed_where_the_backend_treats_it_as_a_no_op():
    assert is_command_admissible("air_revitalisation", {"initial_co2_mass": 0.0}).admissible


@pytest.mark.parametrize("value", [-0.1, 100.1, 1000.0])
def test_percentages_are_bounded_by_the_unit_convention(value):
    # units.py documents moisture/contaminants as percent (0-100).
    verdict = is_command_admissible(
        "air_revitalisation", {"initial_moisture_content": value}
    )
    assert not verdict.admissible


def test_non_numeric_and_boolean_values_are_refused():
    assert "FIELD_NOT_NUMERIC" in is_command_admissible(
        "request_o2", {"amount": "a lot"}
    ).rule_ids
    # bool is an int subclass; True must not silently become 1.0.
    assert "FIELD_NOT_NUMERIC" in is_command_admissible(
        "request_o2", {"amount": True}
    ).rule_ids


def test_rejections_carry_a_readable_reason():
    verdict = is_command_admissible("set_subsystem_failure", {"subsystem": "ars"})
    assert verdict.summary
    assert verdict.to_dict()["rejections"][0]["rule_id"] == "KIND_NOT_OPERATIONAL"


# --- the gate is on the only path to the backend --------------------------


class _RecordingBackend:
    def __init__(self):
        self.applied = []

    def send_air_revitalisation_goal(self, goal):
        self.applied.append(("ars", goal))
        return type("R", (), {"success": True, "to_dict": lambda s: {}})()

    def set_subsystem_failure(self, subsystem, enabled):  # pragma: no cover
        raise AssertionError("scenario control must never reach the backend")


def _team():
    return SsosEclssLoopTeam({
        "mode": "labeled_rule_base",
        "team": {"count": 2, "id_prefix": "eclss_operator"},
        "policy": {},
    })


def test_inadmissible_command_never_reaches_the_backend():
    backend = _RecordingBackend()
    outcome = StepEclssOutcome(commands=[
        EclssOperationalCommand(
            kind="set_subsystem_failure",
            payload={"subsystem": "ars", "enabled": False},
            issued_by="eclss_operator_1",
        )
    ])

    events = _team().apply_outcome(backend, outcome)

    assert backend.applied == []
    assert events[0]["kind"].endswith("operational_inadmissible")
    assert events[0]["decision_source"] == "deterministic_gate"


def test_admissible_command_still_reaches_the_backend():
    backend = _RecordingBackend()
    outcome = StepEclssOutcome(commands=[
        EclssOperationalCommand(
            kind="air_revitalisation",
            payload={"initial_co2_mass": 1.8},
            issued_by="eclss_operator_1",
        )
    ])

    _team().apply_outcome(backend, outcome)

    assert len(backend.applied) == 1


def test_refusal_records_why_for_the_reflection_ledger():
    backend = _RecordingBackend()
    outcome = StepEclssOutcome(commands=[
        EclssOperationalCommand(
            kind="oxygen_generation",
            payload={"input_water_mass": math.nan},
            issued_by="eclss_operator_1",
        )
    ])

    events = _team().apply_outcome(backend, outcome)

    rejections = events[0]["admissibility"]["rejections"]
    assert rejections and rejections[0]["rule_id"] == "FIELD_NOT_FINITE"
    assert rejections[0]["reason"]
