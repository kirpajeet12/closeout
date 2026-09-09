"""Checks the latest real run of each sample batch against the checklist written before its first run.

samples/expected/<name>.json  <->  the newest data/runs/*/packet.json whose register has exactly that checklist's items.
Needs runs on disk (python -m closeout.cli run --register ... --batch ...). Skips, never fakes, when there is none.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("CLOSEOUT_DATA_DIR", ROOT / "data"))
CHECKLISTS = {p.stem: json.loads(p.read_text()) for p in sorted((ROOT / "samples/expected").glob("*.json"))}
FORBIDDEN = ("compliant", "complies", "acceptable", "meets code", "closed", "approved", "certif", "passes")
PROVENANCE = {"register", "contractor_claim", "file_metadata", "model_observation"}


def _latest_packet(expected: dict) -> dict | None:
    runs = sorted((DATA / "runs").glob("run_*/packet.json"), key=lambda p: p.stat().st_mtime) if (DATA / "runs").exists() else []
    for path in reversed(runs):
        pk = json.loads(path.read_text())
        if {it["item"]["item_id"] for it in pk["items"]} == set(expected["items"]):
            return pk
    return None


@pytest.fixture(scope="module", params=sorted(CHECKLISTS))
def checklist(request):
    return request.param


@pytest.fixture(scope="module")
def EXPECTED(checklist):
    return CHECKLISTS[checklist]


@pytest.fixture(scope="module")
def packet(checklist, EXPECTED):
    p = _latest_packet(EXPECTED)
    if p is None:
        pytest.skip(f"no run on disk for {checklist}; run that batch first")
    return p


@pytest.fixture(scope="module")
def items(packet):
    return {it["item"]["item_id"]: it for it in packet["items"]}


@pytest.fixture(scope="module")
def by_file(packet):
    """filename -> list of finding dicts (linked, unresolved, unmatched) mentioning that file."""
    out: dict[str, list[dict]] = {}
    for it in packet["items"]:
        for f in it["evidence"]:
            out.setdefault(f["filename"], []).append(dict(f, _status="matched", _item=it["item"]["item_id"]))
    for u in packet["unmatched"]:
        out.setdefault(u["filename"], []).append(dict(u, _status=u["status"], _item=None))
    return out


def test_evidence_count_after_dedupe(packet, EXPECTED):
    """Only the files of this run's batch count; other batches stay on record but are not this drop."""
    assert len([e for e in packet["evidence_index"] if e["in_batch"]]) == EXPECTED["evidence_records"]["count_after_dedupe"]


ALL_FILES = sorted({f for c in CHECKLISTS.values() for f in c["files"]})
ALL_ITEMS = sorted({i for c in CHECKLISTS.values() for i in c["items"]})


@pytest.mark.parametrize("filename", ALL_FILES)
def test_file_expectation(filename, by_file, items, EXPECTED):
    if filename not in EXPECTED["files"]:
        pytest.skip("not in this checklist")
    exp = EXPECTED["files"][filename]
    fs = by_file.get(filename, [])
    if "supports" in exp:
        for item_id, pages in exp["supports"].items():
            hits = [f for f in fs if f["_item"] == item_id]
            assert hits, f"{filename} should support {item_id}"
            got_pages = {s.get("page") for f in hits for s in f["sources"]}
            assert set(pages) <= got_pages, f"{filename} -> {item_id} pages {got_pages} lack {pages}"
        return
    if exp.get("kind") == "contractor_note":
        assert not [f for f in fs if f["_status"] == "matched"], f"{filename} is a note and must not be linked as evidence"
        return
    assert fs, f"no finding recorded for {filename}"
    if exp.get("match"):
        hit = [f for f in fs if f["_status"] == "matched" and f["_item"] == exp["match"]]
        assert hit, f"{filename} should match {exp['match']}, got {[(f['_status'], f['_item']) for f in fs]}"
        f = hit[0]
        if "tier_in" in exp:
            assert f["tier"] in exp["tier_in"], f"{filename} tier {f['tier']} not in {exp['tier_in']}"
        if f["tier"] == "strong" and "if_strong_flags_any" in exp:
            assert set(exp["if_strong_flags_any"]) & set(f["flags"]), f"{filename} is strong without a location signal: {f['flags']}"
            assert len([o for o in f.get("observations", []) if o.get("text")]) >= 2, f"{filename} strong-by-location needs two named features"
        if f["tier"] == "weak" and "if_weak_flags_include" in exp:
            assert set(exp["if_weak_flags_include"]) <= set(f["flags"]), f"{filename} weak flags {f['flags']}"
        if "slot" in exp:
            slot_types = [s["type"] for s in items[exp["match"]]["item"]["slots"]]
            assert slot_types[f["slot_index"]] == exp["slot"]
    else:
        statuses = {f["_status"] for f in fs}
        allowed = set(exp.get("status_in") or [exp["status"]])
        assert statuses & allowed, f"{filename} status {statuses} not in {allowed}"
        if "candidates_superset_of" in exp:
            cands = {c for f in fs for c in (f.get("candidates") or [])}
            assert set(exp["candidates_superset_of"]) <= cands
    if "flags_include" in exp:
        flags = {fl for f in fs for fl in f["flags"]}
        assert set(exp["flags_include"]) <= flags, f"{filename} flags {flags}"


@pytest.mark.parametrize("item_id", ALL_ITEMS)
def test_item_expectation(item_id, items, EXPECTED):
    if item_id not in EXPECTED["items"]:
        pytest.skip("not in this checklist")
    exp = EXPECTED["items"][item_id]
    it = items[item_id]
    allowed = exp.get("completeness_in") or [exp["completeness"]]
    assert it["completeness"] in allowed, f"{item_id} is {it['completeness']}, expected {allowed}"
    if "missing_slots" in exp:
        assert [m["type"] for m in it["missing_slots"]] == exp["missing_slots"]
    for name in exp.get("must_not_link", []):
        assert name not in [f["filename"] for f in it["evidence"]], f"{item_id} must not link {name}"
    if exp.get("followup_mentions") and it["completeness"] != "complete":
        draft = it["followup_draft"]
        assert draft, f"{item_id} needs a follow-up draft"
        text = (draft["subject"] + " " + draft["body"]).lower()
        for word in exp["followup_mentions"]:
            assert word in text, f"{item_id} draft does not mention '{word}'"
    if it["completeness"] == "complete":
        assert it["followup_draft"] is None


def test_no_acceptability_language(packet):
    def scan(text, where):
        low = text.lower()
        for w in FORBIDDEN:
            assert not re.search(r"\b" + re.escape(w), low), f"'{w}' in {where}"
    for it in packet["items"]:
        for f in it["evidence"] + it["unresolved"]:
            scan(f["rationale"], f"{f['filename']} rationale")
        if it["followup_draft"]:
            scan(it["followup_draft"]["subject"] + " " + it["followup_draft"]["body"], f"{it['item']['item_id']} draft")
    for u in packet["unmatched"]:
        scan(u["rationale"], f"{u['filename']} rationale")


def test_every_finding_has_provenance_and_source(packet):
    kinds = {e["id"]: e["kind"] for e in packet["evidence_index"]}
    for it in packet["items"]:
        for f in it["evidence"]:
            assert f["provenance"] in PROVENANCE
            assert f["sources"], f"{f['filename']} has no source link"
            for s in f["sources"]:
                if kinds.get(s["evidence_id"]) == "pdf":
                    assert s.get("page"), f"PDF link without page: {f['filename']}"
    for u in packet["unmatched"]:
        assert u["sources"]


def test_rerun_created_no_new_evidence_records():
    """Re-uploading a folder never creates evidence rows: one row per distinct file content, across every sample batch."""
    db = DATA / "closeout.db"
    if not db.exists():
        pytest.skip("no database")
    c = sqlite3.connect(db)
    batches = c.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    evidence = c.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    distinct = c.execute("SELECT COUNT(DISTINCT sha256) FROM evidence").fetchone()[0]
    assert evidence == distinct, f"{evidence} evidence rows but {distinct} distinct files after {batches} upload(s)"
    assert evidence <= sum(cl["evidence_records"]["count_after_dedupe"] for cl in CHECKLISTS.values())
    if batches < 2:
        pytest.skip("only one upload so far; upload the same folder again to prove the invariant")