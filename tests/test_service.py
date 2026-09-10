"""The job service against a faked PunchPilot door: manifest in, files pulled, progress and result posted back."""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.pdfgen import canvas

from closeout import service

SECRET = "test-secret-with-enough-length"
BASE = "http://punchpilot.test"


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _pdf(text: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(1200, 900))
    c.drawString(100, 800, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def manifest(run_id="run-1", project_id="proj-1", with_readings=False):
    def url(kind, ident):
        return f"{BASE}/api/closeout/service/file?project={project_id}&{kind}={ident}"
    return {
        "run": {"id": run_id, "status": "queued", "batchId": "batch-1", "scope": ["file-photo", "file-note", "file-pdf"]},
        "project": {"id": project_id, "name": "Test project", "phase": "closeout", "location": "Somewhere"},
        "disciplines": [{"id": "disc-1", "code": "ARCH", "name": "Architectural"}],
        "drawings": [{"id": "dr-1", "sheetNumber": "A-201", "name": "Level 2 plan", "fileName": "A-201.pdf", "discipline": "ARCH",
                      "floor": {"name": "Level 2", "building": "Building B"}, "url": url("drawing", "dr-1"),
                      "readings": [{"page": 1, "summary": "cached"}] if with_readings else []}],
        "findings": [
            {"id": "D-001", "title": "Firestop missing at pipe", "trade": "Mechanical", "location": "Unit B, Level 2", "severity": "critical",
             "status": "corrected", "description": "Seal penetration", "notes": None, "contractor": "ACME", "drawingId": "dr-1",
             "sheetNumber": "A-201", "pinX": 1, "pinY": 2, "pinPage": 1,
             "proofSlots": [{"id": "slot-photo", "kind": "photo", "description": "Photo of sealed penetration with label", "required": True},
                            {"id": "slot-doc", "kind": "document", "description": "Product data sheet", "required": True}],
             "filledSlotIds": [], "referencePhotos": [{"id": "ph-1", "kind": "defect", "fileName": "ref.jpg", "mimeType": "image/jpeg", "url": url("photo", "ph-1")}]},
            {"id": "D-002", "title": "Guard height short", "trade": "Carpentry", "location": "Unit C, deck", "severity": "warning",
             "status": "assigned", "description": None, "notes": None, "contractor": None, "drawingId": None, "sheetNumber": None,
             "pinX": 0, "pinY": 0, "pinPage": 1, "proofSlots": [{"id": "slot-meas", "kind": "measurement", "description": "Tape at guard", "required": True}],
             "filledSlotIds": [], "referencePhotos": []},
        ],
        "batches": [{"id": "batch-1", "label": "Drop 1", "contractor": "ACME", "note": "Photos for D-001 attached", "createdAt": "2026-09-09T00:00:00Z"}],
        "files": [
            {"id": "file-photo", "batchId": "batch-1", "fileName": "IMG_0001.jpg", "mimeType": "image/jpeg", "fileSize": 10, "sha256": "a", "duplicateOfId": None, "url": url("file", "file-photo")},
            {"id": "file-note", "batchId": "batch-1", "fileName": "note.txt", "mimeType": "text/plain", "fileSize": 10, "sha256": "b", "duplicateOfId": None, "url": url("file", "file-note")},
            {"id": "file-pdf", "batchId": "batch-1", "fileName": "datasheet.pdf", "mimeType": "application/pdf", "fileSize": 10, "sha256": "c", "duplicateOfId": None, "url": url("file", "file-pdf")},
        ],
        "callbackUrl": f"{BASE}/api/closeout/service?project={project_id}",
    }


class FakeDoor:
    """The PunchPilot side: serves bytes, records what the service posts, can refuse the result."""

    def __init__(self, refuse_complete: str | None = None):
        self.posts: list[dict] = []
        self.refuse_complete = refuse_complete
        self.bytes = {"file": {"file-photo": ("image/jpeg", "IMG_0001.jpg", _jpeg()),
                               "file-note": ("text/plain", "note.txt", b"Firestop at Unit B done, see photo."),
                               "file-pdf": ("application/pdf", "datasheet.pdf", _pdf("Product data sheet FS-ONE"))},
                      "drawing": {"dr-1": ("application/pdf", "A-201.pdf", _pdf("LEVEL 2 PLAN A-201"))},
                      "photo": {"ph-1": ("image/jpeg", "ref.jpg", _jpeg())}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx.Response(401, json={"error": "Service authentication required"})
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/closeout/service/file":
            for kind in ("file", "drawing", "photo"):
                if kind in q:
                    ctype, name, data = self.bytes[kind][q[kind]]
                    return httpx.Response(200, content=data, headers={"content-type": ctype, "content-disposition": f'inline; filename="{name}"'})
            return httpx.Response(404, json={"error": "Not found"})
        if url.path == "/api/closeout/service" and request.method == "POST":
            body = json.loads(request.content)
            self.posts.append(body)
            if body["status"] == "complete" and self.refuse_complete:
                return httpx.Response(400, json={"error": "Result refused", "details": [self.refuse_complete]})
            return httpx.Response(200, json={"ok": True, "recorded": {"findings": len(body.get("findings", []))}})
        if url.path == "/api/closeout/service" and request.method == "GET":
            if q.get("queued") == "1":
                return httpx.Response(200, json={"runs": [{"id": "run-q", "projectId": "proj-1", "startedAt": "2026-09-09T00:00:00Z"}]})
            return httpx.Response(200, json=manifest(run_id=q["run"], project_id=q["project"]))
        return httpx.Response(404, json={"error": "no route"})

    def door(self) -> service.Door:
        return service.Door(secret=SECRET, client=httpx.Client(transport=httpx.MockTransport(self.handler)))


@pytest.fixture
def fake_agents(monkeypatch):
    calls = {"read": [], "match": [], "draft": []}

    def read_sheet(run, drawing, pdf, page):
        calls["read"].append((drawing["id"], page))
        return {"drawingId": drawing["id"], "page": page, "summary": "A-201 Level 2 plan [floor_plan]. Units: Unit B; Unit C.", "usage": {"inputTokens": 1000, "outputTokens": 100}}

    def match_file(run, f, register, notes, project):
        calls["match"].append(f.filename)
        assert "D-001 | Firestop missing at pipe" in register
        assert "slot 0 [photo]" in register
        assert "Photos for D-001 attached" in notes and "Firestop at Unit B done" in notes
        assert "Sheet A-201" in project and ("Unit B; Unit C" in project or "cached" in project)
        run.add_usage({"inputTokens": 2000, "outputTokens": 200})
        if f.kind == "image":
            return [{"fileId": f.id, "deficiencyId": "D-001", "slotId": "slot-photo", "status": "matched", "tier": "explicit", "candidates": ["D-001"],
                     "flags": [], "rationale": "Label in frame names D-001.", "sources": [{"page": None}], "observations": []}]
        return [{"fileId": f.id, "deficiencyId": None, "slotId": None, "status": "ambiguous", "tier": None, "candidates": ["D-001", "D-002"],
                 "flags": [], "rationale": "A product sheet with no item named.", "sources": [{"page": 1}], "observations": []}]

    def draft_item(run, item, st):
        calls["draft"].append((item["id"], st["state"]))
        return {"deficiencyId": item["id"], "kind": "clarification" if st["state"] == "needs_clarification" else "followup",
                "subject": f"{item['id']} evidence", "body": "Please send the missing item."}

    monkeypatch.setattr(service, "read_sheet", read_sheet)
    monkeypatch.setattr(service, "match_file", match_file)
    monkeypatch.setattr(service, "draft_item", draft_item)
    return calls


def test_full_run_posts_progress_then_result(tmp_path, fake_agents):
    pp = FakeDoor()
    run = service.Run(run_id="run-1", project_id="proj-1", manifest=manifest(), door=pp.door(), work=tmp_path)
    payload = service.execute(run)

    assert fake_agents["read"] == [("dr-1", 1)]
    assert fake_agents["match"] == ["IMG_0001.jpg", "datasheet.pdf"]          # the note never reaches the model
    assert fake_agents["draft"] == [("D-001", "incomplete"), ("D-002", "needs_clarification")]

    statuses = [p["status"] for p in pp.posts]
    assert statuses[0] == "running" and statuses[-1] == "complete" and statuses.count("complete") == 1
    assert pp.posts[0]["progress"] == {"done": 0, "total": 4}
    assert pp.posts[-2]["progress"] == {"done": 4, "total": 4}
    assert all(p["runId"] == "run-1" and p["modelId"] for p in pp.posts)

    assert payload["readings"] == [{"drawingId": "dr-1", "page": 1, "summary": "A-201 Level 2 plan [floor_plan]. Units: Unit B; Unit C."}]
    by_file = {f["fileId"]: f for f in payload["findings"]}
    assert set(by_file) == {"file-photo", "file-note", "file-pdf"}
    assert by_file["file-note"]["status"] == "note" and by_file["file-note"]["observations"][0]["provenance"] == "contractor_claim"
    assert by_file["file-photo"]["slotId"] == "slot-photo"
    assert [d["deficiencyId"] for d in payload["drafts"]] == ["D-001", "D-002"]
    assert payload["drafts"][1]["kind"] == "clarification"
    assert payload["usage"] == {"inputTokens": 5000, "outputTokens": 500, "estimatedUsd": 0.0225}
    assert (tmp_path / "runs" / "run-1" / "result.json").exists()
    assert run.files["file-pdf"].text and "FS-ONE" in run.files["file-pdf"].text[0]


def test_cached_sheets_are_not_read_again(tmp_path, fake_agents):
    pp = FakeDoor()
    run = service.Run(run_id="run-2", project_id="proj-1", manifest=manifest(run_id="run-2", with_readings=True), door=pp.door(), work=tmp_path)
    payload = service.execute(run)
    assert fake_agents["read"] == [] and payload["readings"] == []
    assert pp.posts[0]["progress"] == {"done": 0, "total": 3}


def test_refused_result_is_reported_as_failed(tmp_path, fake_agents):
    pp = FakeDoor(refuse_complete="file file-pdf was in the run but has no result")
    run = service.Run(run_id="run-3", project_id="proj-1", manifest=manifest(run_id="run-3"), door=pp.door(), work=tmp_path)
    with pytest.raises(RuntimeError, match="refused the result"):
        service.execute(run)
    assert pp.posts[-1]["status"] == "failed" and "file-pdf" in pp.posts[-1]["error"]
    assert (tmp_path / "runs" / "run-3" / "log.txt").read_text().count("FAILED") == 1


def test_agent_crash_is_reported_as_failed(tmp_path, fake_agents, monkeypatch):
    def boom(run, f, register, notes, project):
        raise RuntimeError("ThrottlingException")
    monkeypatch.setattr(service, "match_file", boom)
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    pp = FakeDoor()
    run = service.Run(run_id="run-4", project_id="proj-1", manifest=manifest(run_id="run-4"), door=pp.door(), work=tmp_path)
    with pytest.raises(RuntimeError, match="IMG_0001.jpg: ThrottlingException"):
        service.execute(run)
    assert pp.posts[-1]["status"] == "failed"


def test_item_status_mirrors_punchpilot_rules():
    item = {"id": "D-001", "proofSlots": [{"id": "s1", "kind": "photo", "required": True}, {"id": "s2", "kind": "document", "required": True}], "filledSlotIds": []}
    row = lambda **kw: {"fileId": "f", "deficiencyId": "D-001", "slotId": "s1", "status": "matched", "tier": "strong", "candidates": [], "flags": [], **kw}
    assert service.compute_item_status(item, [])["state"] == "no_evidence"
    assert service.compute_item_status(item, [row()])["state"] == "incomplete"
    assert service.compute_item_status(item, [row(), row(slotId="s2")])["state"] == "complete"
    weak = service.compute_item_status(item, [row(tier="weak", flags=["location_unconfirmed"])])
    assert weak["state"] == "needs_clarification" and weak["unresolved"][0]["kind"] == "weak_match"
    assert service.compute_item_status(item, [row(slotId=None)])["state"] == "no_evidence"       # supporting only
    amb = service.compute_item_status(item, [row(status="ambiguous", deficiencyId=None, slotId=None, tier=None, candidates=["D-001", "D-002"])])
    assert amb["state"] == "needs_clarification"
    prior = service.compute_item_status({**item, "filledSlotIds": ["s1", "s2"]}, [])
    assert prior["state"] == "complete"


def test_validation_refuses_judgement_and_bad_slots(tmp_path):
    pp = FakeDoor()
    run = service.Run(run_id="run-5", project_id="proj-1", manifest=manifest(run_id="run-5"), door=pp.door(), work=tmp_path)
    photo = service.Prepared(id="file-photo", filename="IMG_0001.jpg", kind="image", path=tmp_path / "x.jpg", mime="image/jpeg")
    pdf = service.Prepared(id="file-pdf", filename="d.pdf", kind="pdf", path=tmp_path / "x.pdf", mime="application/pdf")
    ok = dict(candidates=None, flags=None, rationale="Label reads D-001.", pages=None, provenance="file_metadata", observations=None)
    v = service.validate_finding
    assert v(run, photo, [], "D-001", "matched", "explicit", 0, **ok) is None
    assert "photo cannot fill" in v(run, photo, [], "D-001", "matched", "explicit", 1, **ok)
    assert "PDF cannot fill" in v(run, pdf, [], "D-001", "matched", "explicit", 0, **ok)
    assert "slots 0..1" in v(run, photo, [], "D-001", "matched", "explicit", 5, **ok)
    assert "must use tier 'weak'" in v(run, photo, [], "D-001", "matched", "strong", 0, **{**ok, "flags": ["location_unconfirmed"]})
    assert "no reference photo" in v(run, photo, [], "D-002", "matched", "strong", 0, **{**ok, "flags": ["location_by_reference"], "observations": [{"text": "a", "provenance": "model_observation"}, {"text": "b", "provenance": "model_observation"}]})
    assert "location_from_sequence cannot apply" in v(run, photo, [], "D-001", "matched", "strong", 0, **{**ok, "flags": ["location_from_sequence"]})
    assert "do not use the word 'approved'" in v(run, photo, [], "D-001", "matched", "explicit", 0, **{**ok, "rationale": "Approved firestop."})
    assert "at least two candidates" in v(run, photo, [], None, "ambiguous", None, None, **{**ok, "candidates": ["D-001"]})
    assert "must not carry an item_id" in v(run, photo, [], "D-001", "unrelated", None, None, **ok)
    assert "not in the register" in v(run, photo, [], None, "ambiguous", None, None, **{**ok, "candidates": ["D-001", "D-999"]})


def test_http_contract_ping_and_invocations(tmp_path, fake_agents, monkeypatch):
    pp = FakeDoor()
    monkeypatch.setattr(service, "WORK_DIR", tmp_path)
    monkeypatch.setattr(service.Run, "work", tmp_path)
    monkeypatch.setenv("CLOSEOUT_AGENT_TOKEN", "inbound-token")
    app = service.create_service(door=pp.door())
    client = TestClient(app)
    assert client.get("/ping").json()["status"] == "Healthy"
    assert client.post("/invocations", json={"runId": "run-6", "projectId": "proj-1"}).status_code == 401
    r = client.post("/invocations", json={"runId": "run-6", "projectId": "proj-1", "manifest": manifest(run_id="run-6")},
                    headers={"authorization": "Bearer inbound-token"})
    assert r.status_code == 202 and r.json()["accepted"] is True
    for _ in range(100):
        if service._active.get("run-6") == "complete":
            break
        time.sleep(0.05)
    assert service._active.get("run-6") == "complete"
    assert pp.posts[-1]["status"] == "complete" and pp.posts[-1]["runId"] == "run-6"
    # a run with no manifest and no app url is refused, not started
    monkeypatch.delenv("CLOSEOUT_APP_URL", raising=False)
    assert client.post("/invocations", json={"runId": "run-7", "projectId": "proj-1"}, headers={"authorization": "Bearer inbound-token"}).status_code == 400


def test_poll_collects_queued_run(tmp_path, fake_agents, monkeypatch):
    pp = FakeDoor()
    monkeypatch.setattr(service.Run, "work", tmp_path)
    service.poll(BASE, 0, door=pp.door(), once=True)
    assert service._active.get("run-q") == "complete"
    assert pp.posts[-1]["runId"] == "run-q" and pp.posts[-1]["status"] == "complete"
