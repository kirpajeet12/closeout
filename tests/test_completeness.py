"""Deterministic rules: the model proposes, this code decides slot filling and item status."""
from closeout.completeness import compute_item_status

ITEM = {"item_id": "D-06", "slots": [{"index": 0, "type": "photo", "description": "photo of repaired brick"}]}
TWO_SLOT = {"item_id": "D-03", "slots": [{"index": 0, "type": "photo", "description": "photo"},
                                          {"index": 1, "type": "report", "description": "installer report"}]}


def f(**kw):
    base = {"id": "f1", "status": "matched", "item_id": "D-06", "tier": "strong", "slot_index": 0,
            "candidates": ["D-06"], "flags": [], "sources": []}
    base.update(kw)
    return base


def test_strong_match_fills_slot():
    st = compute_item_status(ITEM, [f()])
    assert st["completeness"] == "complete" and st["missing_slots"] == []


def test_location_unconfirmed_never_fills_even_if_tier_is_strong():
    st = compute_item_status(ITEM, [f(flags=["location_unconfirmed"])])
    assert st["completeness"] == "needs_clarification"
    assert st["unresolved"][0]["kind"] == "weak_match"


def test_weak_tier_is_unresolved():
    st = compute_item_status(ITEM, [f(tier="weak")])
    assert st["completeness"] == "needs_clarification"


def test_supporting_only_reference_does_not_lift_status():
    st = compute_item_status(ITEM, [f(slot_index=-1)])
    assert st["completeness"] == "no_evidence"
    assert st["supporting"] == ["f1"]


def test_ambiguous_candidate_needs_clarification():
    amb = f(id="f2", status="ambiguous", item_id=None, tier=None, slot_index=None, candidates=["D-01", "D-06"])
    st = compute_item_status(ITEM, [amb])
    assert st["completeness"] == "needs_clarification"
    assert st["unresolved"][0]["kind"] == "ambiguous"


def test_ambiguous_for_other_items_is_ignored():
    amb = f(id="f2", status="ambiguous", item_id=None, tier=None, slot_index=None, candidates=["D-01", "D-04"])
    assert compute_item_status(ITEM, [amb])["completeness"] == "no_evidence"


def test_partial_fill_is_incomplete_and_names_missing_slot():
    st = compute_item_status(TWO_SLOT, [f(item_id="D-03", tier="explicit", slot_index=0)])
    assert st["completeness"] == "incomplete"
    assert [m["type"] for m in st["missing_slots"]] == ["report"]
