"""Tests for the A/B trajectory axes.

The motivating case is pinned first: two runs the instantaneous metrics call
identical, which the trajectory separates. After that, most of these guard the
one property the module exists for -- that the bar a run is graded against
cannot have come from the run.
"""

from __future__ import annotations

import json

import pytest

from scenario.ssos_eclss_loop.reference_limits import CO2_NOMINAL, Habitat, co2_kg_for_ppco2
from scenario.ssos_eclss_loop.trajectory_metrics import (
    NOT_SCORED,
    Band,
    NotScorable,
    Yardstick,
    from_frozen_baseline,
    from_reference_limits,
    trajectory_metrics,
)
from tests.scenario.test_physics_gate import valid_trajectory

BASELINE = {"co2_storage_high_kg": 1.5, "co2_storage_critical_kg": 2.2}
YARD = from_frozen_baseline(BASELINE, baseline_run_id="baseline-1")


def _write(run_dir, records):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return run_dir


def _trajectory(tmp_path, cabin_series, name="run"):
    """A gate-passing trajectory whose cabin CO2 follows the given series."""
    records = []
    for step, value in enumerate(cabin_series):
        base = valid_trajectory()[0]
        record = json.loads(json.dumps(base))
        record["step"] = step
        record["co2_storage_kg"] = value
        records.append(record)
    return _write(tmp_path / name, records)


def _scored(tmp_path, series, name="run"):
    run_dir = _trajectory(tmp_path, series, name)
    return trajectory_metrics(run_dir, YARD, require_gate=False)


# --------------------------------------------------------------------------- #
# the case the module exists for
# --------------------------------------------------------------------------- #
def test_two_runs_with_the_same_peak_separate_on_exposure(tmp_path):
    """Measured for real on a245/a174: identical 2.65 kg peak against an
    identical limit, 15 steps above versus 3."""
    brief = _scored(tmp_path, [1.0, 3.0, 1.0, 1.0, 1.0, 1.0], "brief")
    long_ = _scored(tmp_path, [1.0, 3.0, 3.0, 3.0, 3.0, 1.0], "long")
    assert brief["co2"]["peak_kg"] == long_["co2"]["peak_kg"]
    assert brief["co2"]["terminal_kg"] == long_["co2"]["terminal_kg"]
    b = brief["co2"]["bands"]["critical"]
    l = long_["co2"]["bands"]["critical"]
    assert b["steps_above"] == 1 and l["steps_above"] == 4
    assert l["exposure_integral_kg_steps"] > b["exposure_integral_kg_steps"]


def test_exposure_is_depth_times_duration_not_either_alone(tmp_path):
    """A shallow long excursion and a deep brief one must not tie, and neither
    should dominate by construction."""
    shallow_long = _scored(tmp_path, [2.4] * 10, "sl")["co2"]["bands"]["critical"]
    deep_brief = _scored(tmp_path, [4.2] * 1 + [1.0] * 9, "db")["co2"]["bands"]["critical"]
    assert shallow_long["steps_above"] == 10 and deep_brief["steps_above"] == 1
    # 10 x 0.2 = 2.0 against 1 x 2.0 = 2.0: the integral rates them equal where
    # steps_above and peak each rate one of them far worse.
    assert shallow_long["exposure_integral_kg_steps"] == pytest.approx(2.0)
    assert deep_brief["exposure_integral_kg_steps"] == pytest.approx(2.0)


def test_longest_streak_separates_scattered_from_sustained(tmp_path):
    """Same steps above, same exposure, different shape."""
    scattered = _scored(tmp_path, [3.0, 1.0, 3.0, 1.0, 3.0, 1.0], "sc")["co2"]["bands"]["critical"]
    sustained = _scored(tmp_path, [3.0, 3.0, 3.0, 1.0, 1.0, 1.0], "su")["co2"]["bands"]["critical"]
    assert scattered["steps_above"] == sustained["steps_above"] == 3
    assert scattered["exposure_integral_kg_steps"] == sustained["exposure_integral_kg_steps"]
    assert scattered["longest_streak"] == 1
    assert sustained["longest_streak"] == 3


# --------------------------------------------------------------------------- #
# the yardstick cannot come from the run
# --------------------------------------------------------------------------- #
def test_the_same_trajectory_scores_differently_under_a_moved_bar(tmp_path):
    """Why the bar is an argument: a proposal that lowers a threshold would
    otherwise improve its own score without touching the physics."""
    run_dir = _trajectory(tmp_path, [2.4] * 5)
    strict = trajectory_metrics(run_dir, YARD, require_gate=False)
    lax = trajectory_metrics(
        run_dir,
        from_frozen_baseline(
            {"co2_storage_high_kg": 1.5, "co2_storage_critical_kg": 2.5},
            baseline_run_id="baseline-1",
        ),
        require_gate=False,
    )
    assert strict["co2"]["bands"]["critical"]["steps_above"] == 5
    assert lax["co2"]["bands"]["critical"]["steps_above"] == 0


def test_every_scored_band_records_where_its_number_came_from(tmp_path):
    m = _scored(tmp_path, [1.0, 2.5])
    for band in m["co2"]["bands"].values():
        assert band["origin"], "a band with no origin cannot be reviewed"
    assert m["yardstick"]["source"] == "frozen-baseline"
    assert m["yardstick"]["detail"]["baseline_run_id"] == "baseline-1"


def test_a_frozen_baseline_missing_a_threshold_is_refused():
    with pytest.raises(NotScorable, match="co2_storage_critical_kg"):
        from_frozen_baseline({"co2_storage_high_kg": 1.5}, baseline_run_id="b")


def test_reference_limits_yardstick_converts_the_standard_into_run_units():
    habitat = Habitat(volume_m3=61.3)
    yard = from_reference_limits(habitat)
    assert yard.source == "nasa-std-3001"
    nominal = yard.bands[0]
    assert nominal.threshold_kg == pytest.approx(co2_kg_for_ppco2(CO2_NOMINAL.value, habitat))
    assert "[V2 6004]" in nominal.origin
    # The habitat is the soft spot, so it travels with the score.
    assert yard.detail["habitat"]["volume_m3"] == 61.3


def test_a_bigger_habitat_makes_the_same_standard_a_looser_bar():
    small = from_reference_limits(Habitat(volume_m3=61.3)).bands[0].threshold_kg
    large = from_reference_limits(Habitat(volume_m3=122.6)).bands[0].threshold_kg
    assert large == pytest.approx(small * 2.0)


# --------------------------------------------------------------------------- #
# refusals and coverage
# --------------------------------------------------------------------------- #
def test_a_run_that_fails_the_gate_is_refused_not_scored_badly(tmp_path):
    records = valid_trajectory()
    records[2]["o2_storage_kg"] = -1.0
    run_dir = _write(tmp_path / "void", records)
    with pytest.raises(NotScorable, match="physics gate"):
        trajectory_metrics(run_dir, YARD)


def test_a_passing_run_records_which_gate_form_it_got(tmp_path):
    run_dir = _write(tmp_path / "ok", valid_trajectory())
    m = trajectory_metrics(run_dir, YARD)
    assert m["physics_gate"]["verdict"] == "pass"
    assert m["physics_gate"]["form"] in {"full", "retroactive"}


def test_an_empty_trajectory_is_refused(tmp_path):
    run_dir = tmp_path / "empty"
    run_dir.mkdir()
    (run_dir / "telemetry.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(NotScorable):
        trajectory_metrics(run_dir, YARD, require_gate=False)


def test_the_report_names_what_it_did_not_measure(tmp_path):
    """O2 here is a supply tank, not cabin atmosphere; silence would read as
    coverage."""
    m = _scored(tmp_path, [1.0, 2.5])
    assert set(m["not_scored"]) == {"o2", "water"}
    assert "supply inventory" in NOT_SCORED["o2"]


def test_terminal_margin_is_positive_when_there_is_headroom(tmp_path):
    m = _scored(tmp_path, [1.0, 1.0, 1.2])
    band = m["co2"]["bands"]["critical"]
    assert band["terminal_margin_kg"] == pytest.approx(2.2 - 1.2)
    over = _scored(tmp_path, [1.0, 1.0, 3.0], "over")["co2"]["bands"]["critical"]
    assert over["terminal_margin_kg"] < 0


# --------------------------------------------------------------------------- #
# one sample per step, whatever the agents did
# --------------------------------------------------------------------------- #
def _with_post_ops(tmp_path, pairs, name="run"):
    """pairs: (step, cabin_kg, post_ops_kg_or_None) — the shape scenario_run emits."""
    records = []
    for step, pre, post in pairs:
        base = json.loads(json.dumps(valid_trajectory()[0]))
        base["step"] = step
        base["co2_storage_kg"] = pre
        records.append(base)
        if post is not None:
            after = json.loads(json.dumps(base))
            after["post_ops"] = True
            after["co2_storage_kg"] = post
            records.append(after)
    return _write(tmp_path / name, records)


def test_the_post_ops_refresh_is_a_second_look_not_a_second_step(tmp_path):
    """scenario_run appends a post_ops row only on steps where the team acted.
    Counting rows makes the metric a function of how chatty the agents were."""
    quiet = _with_post_ops(tmp_path, [(0, 3.0, None), (1, 3.0, None)], "quiet")
    busy = _with_post_ops(tmp_path, [(0, 3.0, 3.0), (1, 3.0, 3.0)], "busy")
    q = trajectory_metrics(quiet, YARD, require_gate=False)
    b = trajectory_metrics(busy, YARD, require_gate=False)
    assert q["steps"] == b["steps"] == 2
    assert (
        q["co2"]["bands"]["critical"]["steps_above"]
        == b["co2"]["bands"]["critical"]["steps_above"]
        == 2
    )
    assert (
        q["co2"]["bands"]["critical"]["exposure_integral_kg_steps"]
        == b["co2"]["bands"]["critical"]["exposure_integral_kg_steps"]
    )


def test_a_streak_can_never_exceed_the_run_length(tmp_path):
    """A 40-step run once reported longest_streak = 53."""
    run_dir = _with_post_ops(tmp_path, [(s, 3.0, 3.0) for s in range(5)])
    m = trajectory_metrics(run_dir, YARD, require_gate=False)
    assert m["steps"] == 5
    for stats in m["co2"]["bands"].values():
        assert stats["longest_streak"] <= m["steps"]


def test_the_pre_ops_reading_is_the_one_scored(tmp_path):
    """Not the post-ops refresh: the pre-ops row exists for every step, so the
    sample count depends on run length alone."""
    run_dir = _with_post_ops(tmp_path, [(0, 3.0, 1.0), (1, 3.0, 1.0)], "mixed")
    m = trajectory_metrics(run_dir, YARD, require_gate=False)
    assert m["co2"]["peak_kg"] == 3.0
    assert m["co2"]["terminal_kg"] == 3.0
    assert m["co2"]["bands"]["critical"]["steps_above"] == 2


def test_a_duplicated_pre_ops_row_is_refused_not_double_counted(tmp_path):
    records = [json.loads(json.dumps(valid_trajectory()[0])) for _ in range(2)]
    for r in records:
        r["step"] = 0
        r["co2_storage_kg"] = 3.0
    run_dir = _write(tmp_path / "dupe", records)
    with pytest.raises(NotScorable, match="more than one pre-ops row"):
        trajectory_metrics(run_dir, YARD, require_gate=False)
