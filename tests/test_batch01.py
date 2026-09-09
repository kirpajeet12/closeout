"""Checks the latest real run of samples/evidence/batch-01 against the checklist written before the first run.

Needs a run on disk (python -m closeout.cli run --register samples/register/register.csv --batch samples/evidence/batch-01).
Skips, never fakes, when there is none.
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
EXPECTED = json.loads((ROOT / "samples/expected/batch-01.json").read_text())
FORBIDDEN = ("compliant", "complies", "acceptable", "meets code", "closed", "approved", "certif", "passes")
PROVENANCE = {"register", "contractor_claim", "file_metadata", "model_observation"}


def _latest_packet() -> dict | None:
    runs = sorted((DATA / "runs").glob("run_*/packet.json"), key=lambda p: p.stat().st_mtime) if (DATA / "runs").exists() else []
    return json.loads(runs[-1].read_text()) if runs else None


@pytest.fixture(scope="module")
def packet():
    p = _latest_packet()
    if p is None:
        pytest.skip("no run on disk; run the batch first")
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


def test_evidence_count_after_dedupe(packet):
    assert len(packet["evidence_index"]) == EXPECTED["evidence_records"]["count_after_dedupe"]


@pytest.mark.parametrize("filename", sorted(EXPECTED["files"]))
def test_file_expectation(filename, by_file, items):
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


@pytest.mark.parametrize("item_id", sorted(EXPECTED["items"]))
def test_item_expectation(item_id, items):
    exp = EXPECTED["items"][item_id]
    it = items[item_id]
    allowed = exp.get("completeness_in") or [exp["completeness"]]
    assert it["completeness"] in allowed, f"{item_id} is {it['completeness']}, expected {allowed}"
    if "missing_slots" in exp:
        assert [m["type"] for m in it["missing_slots"]] == exp["missing_slots"]
    for name in exp.get("must_not_link", []):
        assert name not in [f["filename"] for f in it["evidence"]], f"{item_id} must not link {name}"
    if exp.get("followup_mentions"):
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
    db = DATA / "closeout.db"
    if not db.exists():
        pytest.skip("no database")
    c = sqlite3.connect(db)
    batches = c.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    evidence = c.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    assert evidence == EXPECTED["evidence_records"]["count_after_dedupe"], f"{evidence} evidence rows after {batches} batch upload(s)"
    if batches < 2:
        pytest.skip("only one upload so far; upload the same folder again to prove the invariant")
