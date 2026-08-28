"""CLI surface for trajectory scoring and proposal evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scenario.jobs.executor import execute_run
from scenario.jobs.spec import RunSpec
from tools.cli.commands.score import NOT_SCORABLE_EXIT
from tools.cli.main import app

runner = CliRunner()

OVERRIDES = {
    "backend": {"kind": "plant_sim"},
    "simulation": {"steps": 30},
    "inject_failures": True,
    "agents": {"mode": "labeled_rule_base"},
}


@pytest.fixture
def run_dir(tmp_path):
    result = execute_run(
        RunSpec(
            scenario="ssos_eclss_loop",
            overrides=OVERRIDES,
            run_id="demo",
            results_root=tmp_path,
            seed=101,
        )
    )
    assert result.exit_code == 0, result.error
    return result.run_dir


def test_score_defaults_to_the_bar_nothing_here_can_edit(tmp_path, run_dir):
    """No flags means NASA-STD-3001 at the scenario habitat. Defaulting to the
    run's *own* thresholds would grade it against a bar its own proposal may
    have moved; defaulting to the standard cannot drift at all."""
    result = runner.invoke(app, ["score", str(run_dir)])
    assert result.exit_code == 0
    assert "yardstick=nasa-std-3001" in result.output
    assert "[V2 6004]" in result.output


def test_score_refuses_two_yardsticks_at_once(tmp_path, run_dir):
    result = runner.invoke(
        app, ["score", str(run_dir), "--baseline", str(run_dir), "--habitat-volume", "61.3"]
    )
    assert result.exit_code == 2


def test_score_against_a_frozen_baseline(tmp_path, run_dir):
    result = runner.invoke(app, ["score", str(run_dir), "--baseline", str(run_dir)])
    assert result.exit_code == 0
    assert "yardstick=frozen-baseline" in result.output
    # Named as unscorable against a standard, and still reported: leaving the
    # axis out entirely is how a run with the best CO2 and the worst survival
    # reads as the best run (EXP-011).
    assert "not scored against a standard: o2" in result.output
    assert "o2    low" in result.output
    assert "water low" in result.output


def test_score_against_the_standard_needs_a_habitat(tmp_path, run_dir):
    result = runner.invoke(app, ["score", str(run_dir), "--habitat-volume", "61.3"])
    assert result.exit_code == 0
    assert "yardstick=nasa-std-3001" in result.output
    assert "[V2 6004]" in result.output


def test_score_writes_its_result_beside_the_run(tmp_path, run_dir):
    result = runner.invoke(
        app, ["score", str(run_dir), "--baseline", str(run_dir), "--write"]
    )
    assert result.exit_code == 0
    written = json.loads((run_dir / "trajectory_metrics.json").read_text(encoding="utf-8"))
    assert written["yardstick"]["source"] == "frozen-baseline"


def test_score_exits_four_when_the_run_cannot_be_scored(tmp_path, run_dir):
    """Unscorable is not a low score, and a CI step must tell them apart."""
    (run_dir / "telemetry.jsonl").write_text("", encoding="utf-8")
    result = runner.invoke(app, ["score", str(run_dir), "--baseline", str(run_dir)])
    assert result.exit_code == NOT_SCORABLE_EXIT


def test_evaluate_reports_both_arms_and_a_verdict(tmp_path, run_dir):
    result = runner.invoke(app, ["evaluate", str(run_dir), "--results-root", str(tmp_path)])
    assert result.exit_code == 0
    assert "deterministic=True" in result.output
    assert "verdict:" in result.output
    assert "control" in result.output and "treated" in result.output


def test_evaluate_flags_a_proposal_that_moves_an_alarm_setting(tmp_path, run_dir):
    payload = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))
    payload["changes"] = [
        {"change_kind": "set_parameter",
         "payload": {"target": "thresholds.co2_storage_high_kg", "value": 1.1}}
    ]
    (run_dir / "design_proposals.json").write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(app, ["evaluate", str(run_dir), "--results-root", str(tmp_path)])
    assert result.exit_code == 0
    assert "an alarm setting, not a plant" in result.output


def test_evaluate_writes_its_result_beside_the_baseline(tmp_path, run_dir):
    result = runner.invoke(
        app, ["evaluate", str(run_dir), "--results-root", str(tmp_path), "--write"]
    )
    assert result.exit_code == 0
    written = json.loads((run_dir / "proposal_evaluation.json").read_text(encoding="utf-8"))
    assert written["deterministic"] is True
    assert written["pairs"]


def test_evaluate_refuses_a_baseline_with_no_proposal(tmp_path, run_dir):
    payload = json.loads((run_dir / "design_proposals.json").read_text(encoding="utf-8"))
    payload["changes"] = []
    (run_dir / "design_proposals.json").write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(app, ["evaluate", str(run_dir), "--results-root", str(tmp_path)])
    assert result.exit_code == NOT_SCORABLE_EXIT


# --------------------------------------------------------------------------- #
# the documented exit contract, which the first cut did not keep
# --------------------------------------------------------------------------- #
def test_score_exits_four_when_the_named_baseline_has_no_thresholds(tmp_path, run_dir):
    """from_frozen_baseline raises NotScorable; the call used to sit outside
    the handler and surfaced as a traceback with exit 1."""
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "summary.json").write_text(json.dumps({"scenario": "ssos_eclss_loop"}), encoding="utf-8")
    result = runner.invoke(app, ["score", str(run_dir), "--baseline", str(bare)])
    assert result.exit_code == NOT_SCORABLE_EXIT


def test_score_rejects_an_unknown_band_instead_of_printing_nothing(tmp_path, run_dir):
    """Exiting 0 with no rows reads as 'nothing above threshold'."""
    result = runner.invoke(
        app, ["score", str(run_dir), "--baseline", str(run_dir), "--band", "nope"]
    )
    assert result.exit_code == 2
    assert "unknown band" in result.output


def test_evaluate_rejects_an_unknown_band_before_paying_for_the_runs(tmp_path, run_dir):
    result = runner.invoke(
        app,
        ["evaluate", str(run_dir), "--results-root", str(tmp_path),
         "--band", "nope"],
    )
    assert result.exit_code == 2
    assert "unknown band" in result.output


def test_gate_fails_a_corrupt_reading_rather_than_crashing(tmp_path, run_dir):
    """A non-numeric reading is exactly what readings_present_and_finite is
    for; float() used to raise and exit 1, which is the CLI-broken code."""
    from tools.cli.commands.gate import GATE_FAILED_EXIT

    rows = [
        json.loads(line)
        for line in (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[1]["co2_storage_kg"] = "NaN-ish"
    (run_dir / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["gate", str(run_dir)])
    assert result.exit_code == GATE_FAILED_EXIT
    assert "readings_present_and_finite" in result.output


def test_scorecard_names_the_yardstick_it_used(tmp_path, run_dir):
    """A total is meaningless without its bar. EXP-014 mixed the two once, so the
    scorecard says which one produced the number it just printed."""
    result = runner.invoke(app, ["scorecard", str(run_dir)])
    assert result.exit_code == 0
    assert "yardstick: NASA-STD-3001 at 388 m3" in result.stdout


def test_the_two_yardsticks_give_different_totals(tmp_path, run_dir):
    """Not a formality: the v5 rule arm is 82.230 against its own thresholds and
    84.35 against the standard. EXP-013..016 published the former."""
    standard = runner.invoke(app, ["scorecard", str(run_dir), "--json"])
    own = runner.invoke(app, ["scorecard", str(run_dir), "--yardstick", "run", "--json"])
    assert standard.exit_code == 0 and own.exit_code == 0
    a = json.loads(standard.stdout)
    b = json.loads(own.stdout)
    assert a["yardstick"] != b["yardstick"]
    assert a["axes"]["B_margin"]["points"] != b["axes"]["B_margin"]["points"]
    assert a["axes"]["actor_remaining"]["points"] == b["axes"]["actor_remaining"]["points"]


def test_a_clean_run_records_that_nothing_moved_its_bar(tmp_path, run_dir):
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["scoring_bar_modified"] == []
    result = runner.invoke(app, ["scorecard", str(run_dir), "--yardstick", "run"])
    assert result.exit_code == 0
    assert "unverified" not in result.stdout


def _run_with(tmp_path, run_id, extra):
    overrides = dict(OVERRIDES)
    overrides.update(extra)
    result = execute_run(
        RunSpec(scenario="ssos_eclss_loop", overrides=overrides, run_id=run_id,
                results_root=tmp_path, seed=101)
    )
    assert result.exit_code == 0, result.error
    return Path(result.run_dir)


@pytest.mark.parametrize(
    "run_id, extra, expected, standard_still_scores",
    [
        (
            "moved_thresholds",
            {"thresholds": {"co2_storage_high_kg": 99.0}},
            "thresholds",
            True,
        ),
        (
            "moved_bands",
            {"plant_sim": {"survival": {"bands": {"product_water_low_l": 0.001}}}},
            "plant_sim.survival.bands",
            False,
        ),
        (
            "attrition_off",
            {"plant_sim": {"survival": {"enabled": False}}},
            "plant_sim.survival.enabled",
            False,
        ),
        (
            "dwell_table_zeroed",
            {"plant_sim": {"survival": {"o2": {"warning_loss": 0, "critical_loss": 0}}}},
            "plant_sim.survival.o2",
            False,
        ),
        (
            "dwell_divisor_raised",
            {"plant_sim": {"survival": {"co2": {"warning_divisor": 99999}}}},
            "plant_sim.survival.co2",
            False,
        ),
    ],
)
def test_every_route_to_the_bar_is_refused_not_just_thresholds(
    tmp_path, run_id, extra, expected, standard_still_scores
):
    """Three earlier versions of this guard were narrow in the same way: each
    picked one route to the bar and assumed it was the only one. thresholds is
    not the bar -- an audit took a run from 0/4 crew to 4/4, 29.4 to 78.1 of 90,
    through plant_sim.survival.bands with thresholds untouched, and
    survival.enabled: false does it by switching attrition off outright.

    The third (EXP-021, 2026-08-29) went around both by leaving them alone and
    zeroing the dwell table beside them: ``*_loss: 0`` requests no occupant and
    ``divisor: 99999`` floors ``alive // divisor``, so either does what
    ``enabled: false`` does. 2/4 crew to 4/4, 46.9 to 71.3 of 90, and the cabin
    left worse -- peak CO2 8.81 to 12.06 against a critical band of 8.0. Hence
    the whole ``survival`` subtree is diffed rather than enumerated, and the
    last two cases here are that route.

    ``standard_still_scores`` is the other half of the correction. That yardstick
    used to be the way out of every refusal -- and it never ran the check at all,
    so the escape hatch the error message named was the unguarded one. It is a
    real way out only for ``thresholds``, which stops deciding anything once CO2
    is scored against NASA. Anything under ``survival`` set attrition, which is
    the 50-point axis baked into the run, and the o2/water bands
    ``inventory_metrics`` reads under either yardstick. No yardstick can rescue
    those.
    """
    moved = _run_with(tmp_path, run_id, extra)
    recorded = json.loads((moved / "summary.json").read_text(encoding="utf-8"))
    assert recorded["scoring_bar_modified"] == [expected]

    refused = runner.invoke(app, ["scorecard", str(moved), "--yardstick", "run"])
    assert refused.exit_code == 2
    assert expected in (refused.output or refused.stderr or '')

    standard = runner.invoke(app, ["scorecard", str(moved)])
    if standard_still_scores:
        assert standard.exit_code == 0
    else:
        assert standard.exit_code == 2
        assert expected in (standard.output or standard.stderr or '')


def test_an_easier_scenario_is_recorded_even_though_the_bar_is_clean(tmp_path):
    """A clean bar says the score was not gamed. It does not say the scenario
    was the published one.

    An audit (EXP-021) scored a --actor-mode none run at 90.000 of 90 -- 7.77
    above the published rule arm -- on four keys that are not part of the bar:
    the three ``simulation.initial_*`` inventories and
    ``plant_sim.crew.activity_factor``. scoring_bar_modified was empty and the
    physics gate passed, both correctly. Nothing anywhere said the run had been
    made easy, and the number was read off the scorecard.
    """
    easy = _run_with(
        tmp_path,
        "made_easy",
        {
            "simulation": {"steps": 30, "initial_o2_storage_kg": 200.0},
            "plant_sim": {"crew": {"activity_factor": 0.0}},
        },
    )
    summary = json.loads((easy / "summary.json").read_text(encoding="utf-8"))
    assert summary["scoring_bar_modified"] == []
    assert summary["operating_point_modified"] == [
        "simulation.initial_o2_storage_kg",
        "plant_sim.crew.activity_factor",
        "inject_failures",  # from OVERRIDES, and it belongs on this list too
    ]

    scored = runner.invoke(app, ["scorecard", str(easy), "--yardstick", "run"])
    assert scored.exit_code == 0
    assert "運用点を変更" in scored.stdout
    assert "plant_sim.crew.activity_factor" in scored.stdout


def test_run_knobs_are_not_read_as_a_changed_operating_point(tmp_path):
    """steps and seed select a run; they do not describe the plant. Flagging
    them would put the warning on every sweep and teach everyone to ignore it.
    seed is doubly inert here -- EXP-021 found 101, 202 and 999 give
    byte-identical telemetry, so it does not reach the deterministic arm at all.
    """
    plain = _run_with(tmp_path, "plain", {"simulation": {"steps": 12}})
    summary = json.loads((plain / "summary.json").read_text(encoding="utf-8"))
    # inject_failures is on in OVERRIDES and *is* an operating point change.
    assert summary["operating_point_modified"] == ["inject_failures"]

    scored = runner.invoke(app, ["scorecard", str(plain), "--yardstick", "run"])
    assert "simulation.steps" not in scored.stdout
    assert "simulation.seed" not in scored.stdout


def test_a_run_predating_the_record_is_scored_but_marked_unverified(tmp_path, run_dir):
    """v3-v5 runs cannot say whether their bar was moved. Say so rather than
    either refusing them (the published numbers stop being reproducible) or
    implying a check that did not happen."""
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    del summary["scoring_bar_modified"]
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    result = runner.invoke(app, ["scorecard", str(run_dir), "--yardstick", "run"])
    assert result.exit_code == 0
    assert "unverified" in result.stdout


def test_a_baseline_that_moved_its_own_bar_cannot_be_frozen(tmp_path, run_dir):
    """--baseline freezes a run's thresholds for everything scored against it.
    A baseline that drew its own line launders that line into every such run --
    and when the baseline is the run under test, that is exactly what
    _build_yardstick's docstring forbids. A clean run stays usable, which is why
    the check is on the bar and not on whether the paths match."""
    dirty = _run_with(tmp_path, "dirty_base", {"thresholds": {"co2_storage_high_kg": 99.0}})
    assert runner.invoke(app, ["score", str(run_dir), "--baseline", str(dirty)]).exit_code != 0
    assert runner.invoke(app, ["score", str(dirty), "--baseline", str(dirty)]).exit_code != 0
    assert runner.invoke(app, ["score", str(run_dir), "--baseline", str(run_dir)]).exit_code == 0


def test_the_run_s_own_yardstick_takes_no_habitat(tmp_path, run_dir):
    result = runner.invoke(
        app, ["scorecard", str(run_dir), "--yardstick", "run", "--habitat-volume-m3", "388"]
    )
    assert result.exit_code != 0
