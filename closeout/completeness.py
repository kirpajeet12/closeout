"""Deterministic completeness check. The model proposes matches; this code does the bookkeeping."""
from __future__ import annotations

FILLING_TIERS = {"explicit", "strong"}
# A finding carrying any of these flags never fills a slot, whatever tier the model chose.
NON_FILLING_FLAGS = {"location_unconfirmed", "conflicting_reference"}


def compute_item_status(deficiency: dict, findings: list[dict]) -> dict:
    """findings: current findings across all evidence. Returns completeness for one item."""
    item_id = deficiency["item_id"]
    slots = deficiency["slots"]
    filled: dict[int, list[str]] = {s["index"]: [] for s in slots}
    unresolved: list[dict] = []
    supporting: list[str] = []

    for f in findings:
        if f["status"] == "matched" and f["item_id"] == item_id:
            blocked = NON_FILLING_FLAGS & set(f["flags"])
            if f["slot_index"] is not None and f["slot_index"] >= 0 and f["tier"] in FILLING_TIERS and not blocked:
                filled[f["slot_index"]].append(f["id"])
            elif f["slot_index"] is not None and f["slot_index"] >= 0:
                unresolved.append({"finding_id": f["id"], "kind": "weak_match", "slot_index": f["slot_index"], "flags": f["flags"]})
            else:
                supporting.append(f["id"])
        elif f["status"] in ("ambiguous", "conflict") and item_id in f["candidates"]:
            unresolved.append({"finding_id": f["id"], "kind": f["status"], "flags": f["flags"]})

    # Supporting-only references (e.g. a daily-log line) never raise an item above no_evidence:
    # they describe work, they are not the evidence the register asked for.
    missing = [s for s in slots if not filled[s["index"]]]
    if not missing:
        completeness = "complete"
    elif any(filled.values()):
        completeness = "incomplete"
    elif unresolved:
        completeness = "needs_clarification"
    else:
        completeness = "no_evidence"
    return {
        "item_id": item_id,
        "completeness": completeness,
        "missing_slots": [{"index": s["index"], "type": s["type"], "description": s["description"]} for s in missing],
        "filled_slots": [{"index": i, "finding_ids": ids} for i, ids in filled.items() if ids],
        "supporting": supporting,
        "unresolved": unresolved,
    }
