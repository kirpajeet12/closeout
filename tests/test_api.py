"""Evidence Desk API against a faked agent: upload register, drop a folder, follow the SSE feed, read the packet, retry."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from closeout import api, pipeline
from closeout.config import Settings

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "samples/register/register.csv"
BATCH = ROOT / "samples/evidence/batch-01"


class FakeAgent:
    """Stands in for the Strands agent: records one finding per file, fails once on demand."""

    def __init__(self):
        self.fail_once: set[str] = set()
        self.calls: list[str] = []

    def match(self, store, run_id, job_id, evidence_id, register_text, notes_text, filenames, model=None,
              file_context="", neighbours=None):
        ev = store.evidence(evidence_id)
        self.calls.append(ev["filename"])
        if ev["filename"] in self.fail_once:
            self.fail_once.discard(ev["filename"])
            raise RuntimeError("simulated Bedrock throttle")
        item = "D-01" if "firestop" in ev["filename"].lower() else None
        if item:
            store.add_finding(run_id=run_id, job_id=job_id, evidence_id=evidence_id, status="matched", item_id=item,
                              slot_index=0, tier="strong", provenance="contractor_claim", rationale="fake", flags=[],
                              sources=[{"evidence_id": evidence_id, "page": None}], observations=[], candidates=[])
        else:
            store.add_finding(run_id=run_id, job_id=job_id, evidence_id=evidence_id, status="unrelated",
                              provenance="contractor_claim", rationale="fake", flags=[],
                              sources=[{"evidence_id": evidence_id, "page": None}], candidates=[])
        return {"findings": 1, "usage": {"inputTokens": 10, "outputTokens": 5}}

    def draft(self, store, run_id, job_id, item_id, item_brief, model=None):
        did = store.upsert_draft(run_id, item_id, f"Follow-up {item_id}", "Please send the missing evidence.")
        return {"draft_id": did, "usage": {"inputTokens": 3, "outputTokens": 2}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(pipeline, "run_match_job", fake.match)
    monkeypatch.setattr(pipeline, "run_draft_job", fake.draft)
    monkeypatch.setattr(pipeline, "make_model", lambda settings: None)
    settings = Settings(data_dir=tmp_path / "data", model_id="fake-model")
    c = TestClient(api.create_app(settings))
    c.fake = fake
    return c


def _events(client, run_id="pending"):
    out = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def _files(root: Path):
    return [("files", (p.name, p.read_bytes())) for p in sorted(root.iterdir()) if p.is_file() and not p.name.startswith(".")]


def _register_drop():
    """The register folder as the browser would send it: CSV plus photos/ with relative paths."""
    files, paths = [], []
    for p in sorted(REGISTER.parent.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            files.append(("files", (p.name, p.read_bytes())))
            paths.append(str(p.relative_to(REGISTER.parent)))
    return files, {"paths": paths}


def _import_register(client):
    files, paths = _register_drop()
    return client.post("/api/register", files=files, data=paths)


def test_batch_needs_register(client):
    r = client.post("/api/batches", files=_files(BATCH)[:1])
    assert r.status_code == 409


def test_register_csv_alone_is_rejected_when_photos_missing(client):
    r = client.post("/api/register", files=[("files", ("register.csv", REGISTER.read_bytes()))])
    assert r.status_code == 400 and "reference_photo" in r.text


def test_full_flow_with_sse_and_retry(client):
    r = _import_register(client)
    assert r.status_code == 200, r.text
    assert r.json()["imported"] >= 6
    assert client.get("/api/register/D-01/reference").status_code == 200

    files = _files(BATCH)
    client.fake.fail_once.add("IMG_2201_L2_corridor_firestop.jpg")
    data = {"paths": [f"Firestopping/{name}" if "firestop" in name else name for _, (name, _) in files], "label": "drop 1"}
    r = client.post("/api/batches", files=files, data=data)
    assert r.status_code == 200, r.text
    assert r.json()["files"] == len(files)

    evs = _events(client)  # follows the pending run to its packet
    kinds = [e["event"] for e in evs]
    assert kinds[0] == "uploaded" and kinds[-1] == "packet"
    assert "job_failed" in kinds and "completeness" in kinds
    run_id = next(e["data"]["run_id"] for e in evs if e["event"] == "jobs_created")

    ov = client.get("/api/overview").json()
    assert ov["latest_run_id"] == run_id and ov["active_run_id"] is None
    assert ov["packet"]["run"]["status"] == "failed"
    firestop = next(e for e in ov["packet"]["evidence_index"] if "firestop" in e["filename"])
    assert firestop["metadata"]["folder"] == "Firestopping"
    assert client.get(f"/api/evidence/{firestop['id']}/file").status_code == 200

    # retry re-runs only the failed job and finishes the packet
    r = client.post(f"/api/runs/{run_id}/retry")
    assert r.status_code == 200
    evs2 = _events(client, run_id)
    assert evs2[-1]["event"] == "packet"
    assert [e["data"]["filename"] for e in evs2 if e["event"] == "job_start" and e["data"]["kind"] == "match"] == ["IMG_2201_L2_corridor_firestop.jpg"]
    packet = client.get(f"/api/runs/{run_id}/packet").json()
    assert packet["run"]["status"] == "done"
    d01 = next(i for i in packet["items"] if i["item"]["item_id"] == "D-01")
    assert d01["evidence"] and d01["evidence"][0]["tier"] == "strong"
    assert client.get(f"/api/runs/{run_id}/packet.md").text.startswith("#")

    # review actions
    d02 = next(i for i in packet["items"] if i["item"]["item_id"] == "D-02")
    draft = d02["followup_draft"]
    assert draft and client.patch(f"/api/drafts/{draft['id']}", json={"body": "edited"}).status_code == 200
    assert client.post("/api/items/D-02/decision", json={"decision": "hold", "note": "waiting"}).status_code == 200
    packet = client.get(f"/api/runs/{run_id}/packet").json()
    d02 = next(i for i in packet["items"] if i["item"]["item_id"] == "D-02")
    assert d02["followup_draft"]["body"] == "edited" and d02["decision"]["decision"] == "hold"

    # replaying a finished run streams its history and terminates
    evs3 = _events(client, run_id)
    assert evs3[-1]["event"] == "packet"


def test_second_upload_while_running_is_refused(client, monkeypatch):
    assert _import_register(client).status_code == 200
    gate = threading.Event()
    orig = client.fake.match

    def slow(*a, **kw):
        gate.wait(5)
        return orig(*a, **kw)

    monkeypatch.setattr(pipeline, "run_match_job", slow)
    files = _files(BATCH)[:2]
    assert client.post("/api/batches", files=files).status_code == 200
    assert client.post("/api/batches", files=files).status_code == 409
    gate.set()
    assert _events(client)[-1]["event"] == "packet"
