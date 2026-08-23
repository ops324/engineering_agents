"""F3's hypothesis-memory level — design.md 6, addendum 16."""

import pytest

from core.agents.hypotheses import HypothesisStore, parse_predicate

CO2_HIGH = {"metric": "co2_storage_kg", "op": ">=", "value": 1.8}
O2_LOW = {"metric": "o2_storage_kg", "op": "<", "value": 0.5}


def _store_with_one(horizon=2):
    store = HypothesisStore()
    store.propose(
        {"condition": [CO2_HIGH], "prediction": [O2_LOW], "horizon": horizon},
        step=0,
        agent_id="eclss_operator_1",
    )
    return store


def test_unknown_metric_is_refused_at_intake_with_a_reason():
    store = HypothesisStore()
    assert (
        store.propose(
            {"condition": [{"metric": "power_w", "op": ">", "value": 1}], "prediction": [O2_LOW]},
            step=1,
            agent_id="a",
        )
        is None
    )
    assert "unknown metric" in store.rejected[0]["reasons"][0]


def test_a_half_parseable_hypothesis_is_not_kept_in_part():
    store = HypothesisStore()
    store.propose(
        {"condition": [CO2_HIGH, {"metric": "nope", "op": "==", "value": 1}], "prediction": [O2_LOW]},
        step=1,
        agent_id="a",
    )
    assert store.hypotheses == []


def test_horizon_outside_the_range_is_refused():
    store = HypothesisStore()
    store.propose({"condition": [CO2_HIGH], "prediction": [O2_LOW], "horizon": 9}, step=1, agent_id="a")
    assert any("horizon" in r for r in store.rejected[0]["reasons"])


def test_prediction_is_scored_at_the_horizon_not_before():
    store = _store_with_one(horizon=2)
    store.observe(0, {"co2_storage_kg": 2.0, "o2_storage_kg": 1.0})
    assert store.stats()["scoring_events"] == 0
    store.observe(1, {"co2_storage_kg": 1.0, "o2_storage_kg": 1.0})
    assert store.stats()["scoring_events"] == 0
    store.observe(2, {"co2_storage_kg": 1.0, "o2_storage_kg": 0.2})
    assert store.stats()["scoring_events"] == 1
    assert store.hypotheses[0].support == 1


def test_a_prediction_that_fails_counts_as_refutation():
    store = _store_with_one(horizon=1)
    store.observe(0, {"co2_storage_kg": 2.0, "o2_storage_kg": 1.0})
    store.observe(1, {"co2_storage_kg": 1.0, "o2_storage_kg": 1.0})
    assert store.hypotheses[0].refute == 1


def test_a_missing_metric_is_not_a_refutation():
    store = _store_with_one(horizon=1)
    store.observe(0, {"co2_storage_kg": 2.0, "o2_storage_kg": 1.0})
    store.observe(1, {"co2_storage_kg": 1.0})
    assert store.hypotheses[0].refute == 0
    assert store.stats()["unscorable_events"] == 1


def test_one_verdict_per_firing_even_when_the_condition_holds_every_step():
    store = _store_with_one(horizon=1)
    for step in range(4):
        store.observe(step, {"co2_storage_kg": 2.0, "o2_storage_kg": 1.0})
    assert store.stats()["scoring_events"] == 3


def test_refuted_at_the_threshold_and_never_deleted():
    store = _store_with_one(horizon=1)
    for step in range(8):
        store.observe(step, {"co2_storage_kg": 2.0, "o2_storage_kg": 1.0})
    hypothesis = store.hypotheses[0]
    assert hypothesis.status == "refuted"
    assert hypothesis in store.hypotheses


def test_a_hypothesis_right_more_often_than_wrong_is_not_refuted():
    """Three refutations alone do not refute it; they have to outnumber support."""
    store = _store_with_one(horizon=1)
    for step in range(10):
        # Wrong every third step, right on the other two.
        o2 = 1.0 if step % 3 == 2 else 0.2
        store.observe(step, {"co2_storage_kg": 2.0, "o2_storage_kg": o2})
    hypothesis = store.hypotheses[0]
    assert hypothesis.refute >= 3
    assert hypothesis.support > hypothesis.refute
    assert hypothesis.status == "active"


def test_retrieval_only_surfaces_hypotheses_whose_condition_holds_now():
    store = _store_with_one()
    assert store.retrieve({"co2_storage_kg": 2.0, "o2_storage_kg": 1.0})
    assert store.retrieve({"co2_storage_kg": 0.1, "o2_storage_kg": 1.0}) == []


def test_measurement_reorders_retrieval():
    """6.4's difference from RAG: the score, not the text, decides the order."""
    store = HypothesisStore()
    store.propose({"condition": [CO2_HIGH], "prediction": [O2_LOW], "horizon": 1}, step=0, agent_id="a")
    store.propose(
        {
            "condition": [CO2_HIGH],
            "prediction": [{"metric": "o2_storage_kg", "op": ">", "value": 0.5}],
            "horizon": 1,
        },
        step=0,
        agent_id="b",
    )
    reading = {"co2_storage_kg": 2.0, "o2_storage_kg": 0.2}
    # Before any measurement the two are tied and fall back to id order.
    assert [h.id for h in store.retrieve(reading)] == ["H-1", "H-2"]
    for step in range(3):
        store.observe(step, reading)
    # H-2 predicted the O2 that did not happen, so it now ranks below H-1 —
    # the order moved because of measurement, which is the whole claim.
    assert store.hypotheses[0].score() > store.hypotheses[1].score()
    assert [h.id for h in store.retrieve(reading)] == ["H-1", "H-2"]
    store.hypotheses[0].support = 0
    store.hypotheses[0].refute = 5
    assert [h.id for h in store.retrieve(reading)] == ["H-2", "H-1"]


def test_refuted_hypotheses_still_surface_and_say_so():
    store = _store_with_one(horizon=1)
    for step in range(8):
        store.observe(step, {"co2_storage_kg": 2.0, "o2_storage_kg": 1.0})
    text = store.describe({"co2_storage_kg": 2.0, "o2_storage_kg": 1.0})
    assert "REFUTED" in text


def test_the_same_claim_from_two_operators_is_one_ledger_entry():
    store = _store_with_one()
    store.propose({"condition": [CO2_HIGH], "prediction": [O2_LOW], "horizon": 2}, step=3, agent_id="b")
    assert len(store.hypotheses) == 1


def test_boolean_and_status_metrics_parse_and_compare():
    predicate, note = parse_predicate({"metric": "ars_failure_enabled", "op": "==", "value": True})
    assert note is None and predicate.holds({"ars_failure_enabled": True}) is True
    predicate, note = parse_predicate({"metric": "co2_status", "op": "==", "value": "CRITICAL"})
    assert note is None and predicate.holds({"co2_status": "critical"}) is True


def test_an_ordering_operator_is_refused_on_a_status_metric():
    _, note = parse_predicate({"metric": "co2_status", "op": ">", "value": "critical"})
    assert "not allowed" in note


def test_the_ledger_records_what_it_refused_and_why(tmp_path):
    """A count alone cannot tell a bad contract from a model that cannot comply."""
    store = HypothesisStore()
    store.propose(
        {"condition": [CO2_HIGH], "prediction": [O2_LOW], "horizon": 1},
        step=0, agent_id="a",
    )
    store.propose(
        {"condition": [CO2_HIGH], "prediction": ["request_o2"], "horizon": 2},
        step=1, agent_id="b",
    )
    path = tmp_path / "hypotheses.jsonl"
    store.write_jsonl(path)
    rows = [__import__("json").loads(line) for line in path.read_text().splitlines()]
    kinds = [r["kind"] for r in rows]
    assert kinds == ["hypothesis", "refused"]
    refused = rows[1]
    assert refused["agent_id"] == "b"
    assert refused["raw"]["prediction"] == ["request_o2"]
    assert any("prediction" in reason for reason in refused["reasons"])


def test_a_bare_string_prediction_is_refused_with_a_reason():
    """The shape every offer in the first check run came back with."""
    store = HypothesisStore()
    assert store.propose(
        {"condition": [CO2_HIGH], "prediction": ["request_o2"], "horizon": 2},
        step=0, agent_id="a",
    ) is None
    assert any("predicate must be an object" in r for r in store.rejected[0]["reasons"])


def test_a_one_element_list_is_read_as_the_hypothesis_inside_it():
    """What ninety-three of ninety-seven offers came back as."""
    store = HypothesisStore()
    got = store.propose(
        [{"condition": [CO2_HIGH], "prediction": [O2_LOW], "horizon": 2}],
        step=0, agent_id="a",
    )
    assert got is not None and store.rejected == []


def test_a_longer_list_is_refused_rather_than_truncated():
    """Picking one of three would be choosing on the agent's behalf."""
    store = HypothesisStore()
    one = {"condition": [CO2_HIGH], "prediction": [O2_LOW], "horizon": 2}
    assert store.propose([one, one], step=0, agent_id="a") is None
    assert store.hypotheses == []
    assert "offer one hypothesis, not several" in store.rejected[0]["reasons"][0]


def test_a_refused_offer_keeps_its_raw_shape_whatever_it_was():
    store = HypothesisStore()
    store.propose("not an object at all", step=0, agent_id="a")
    store.propose([1, 2, 3], step=1, agent_id="b")
    assert store.rejected[0]["raw"] == "not an object at all"
    assert store.rejected[1]["raw"] == [1, 2, 3]
