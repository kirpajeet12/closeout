"""Drawings review: cost shown before anything is bought, one paid call per sheet, findings checked as they are recorded,
a half-done review continues instead of paying twice, and the set summary closes it.

The model is faked: the fake records whatever `calls` says, through the real tools.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from closeout import api, drawings, pipeline
from closeout.config import Settings
from tests.test_documents import _seed


class FakeDrawingsAgent:
    calls: list[dict] = []
    prompts: list[list[dict]] = []
    usage = {"inputTokens": 5000, "outputTokens": 400}

    def __init__(self, model=None, tools=None, system_prompt="", callback_handler=None):
        self.tools = {getattr(t, "tool_name", getattr(t, "__name__", "tool")): t for t in tools}
        self.system_prompt = system_prompt

    def __call__(self, content):
        FakeDrawingsAgent.prompts.append(content)
        for c in FakeDrawingsAgent.calls:
            name = next((k for k in self.tools if c["tool"] in k), None)
            if name:
                self.tools[name](**{k: v for k, v in c.items() if k != "tool"})

        class R:
            class metrics:
                accumulated_usage = dict(FakeDrawingsAgent.usage)
        return R()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "make_model", lambda settings, fast=False, max_tokens=None: None)
    monkeypatch.setattr(drawings, "make_model", lambda settings, fast=False, max_tokens=None: None)
    monkeypatch.setattr(drawings, "Agent", FakeDrawingsAgent)
    FakeDrawingsAgent.calls, FakeDrawingsAgent.prompts = [], []
    settings = Settings(data_dir=tmp_path / "data", model_id="us.anthropic.claude-sonnet-4-6")
    c = TestClient(api.create_app(settings))
    c.settings = settings
    return c


def test_cost_is_shown_before_anything_is_bought(client, tmp_path):
    slug = _seed(client, tmp_path)
    est = client.get(f"/api/projects/{slug}/drawings/estimate?discipline=AR").json()
    assert est["sheets"] == 4 and est["discipline_name"] == "Architectural"
    assert 0 < est["usd_low"] < est["usd"] < est["usd_high"] < 2
    assert client.get(f"/api/projects/{slug}/drawings/estimate?discipline=EL").json()["sheets"] == 1
    assert client.get(f"/api/projects/{slug}/drawings/estimate?discipline=PL").json()["sheets"] == 0
    assert FakeDrawingsAgent.prompts == []          # nothing was read
    assert client.get(f"/api/projects/{slug}").json()["drawings_reviews"] == []


def test_review_is_bought_sheet_by_sheet_and_bad_findings_are_refused(client, tmp_path):
    slug = _seed(client, tmp_path)
    assert client.post(f"/api/projects/{slug}/drawings/reviews", json={"discipline": "PL"}).status_code == 400
    rv = client.post(f"/api/projects/{slug}/drawings/reviews", json={"discipline": "EL"}).json()["review"]
    assert rv["status"] == "reading" and [r["status"] for r in rv["sheets"]] == ["pending"] and rv["cost_usd"] == 0
    sheet_id = rv["sheets"][0]["sheet_id"]

    FakeDrawingsAgent.calls = [
        {"tool": "record_finding", "kind": "check_on_site", "what": "Service mast height above the roof line as noted", "where": "C3", "why": "note 4"},
        {"tool": "record_finding", "kind": "maybe", "what": "Not a real kind of finding at all", "where": "A1"},
        {"tool": "record_finding", "kind": "ask_designer", "what": "Service mast height above the roof line as noted", "where": "C3"},
        {"tool": "record_finding", "kind": "ask_designer", "what": "Legend shows a symbol the plan does not use", "where": "legend", "why": "legend vs plan"},
        {"tool": "record_sheet_summary", "summary": "Site plan with the service entry."},
    ]
    out = client.post(f"/api/projects/{slug}/drawings/reviews/{rv['id']}/sheets/{sheet_id}").json()
    sheet = out["sheet"]
    assert sheet["status"] == "done" and sheet["summary"] == "Site plan with the service entry."
    assert [f["kind"] for f in sheet["findings"]] == ["check_on_site", "ask_designer"]      # bad kind and duplicate refused
    assert any("maybe" in r for r in out["rejections"])
    # cost from the real price list: 5000 in at $3/M + 400 out at $15/M
    assert out["cost_usd"] == pytest.approx(0.021, abs=0.0005)
    assert out["review"]["cost_usd"] == out["cost_usd"]
    # the sheet image went to the model with the grid, and the printed text was sent
    prompt = FakeDrawingsAgent.prompts[0]
    assert any("image" in c for c in prompt) and any("SHEET EL-01" in c.get("text", "") for c in prompt)

    FakeDrawingsAgent.calls = [
        {"tool": "record_gap", "what": "No single line diagram in the set", "why": "sheet list has only the site plan"},
        {"tool": "record_set_summary", "summary": "One sheet. Check the mast. Ask about the legend."},
    ]
    done = client.post(f"/api/projects/{slug}/drawings/reviews/{rv['id']}/finish").json()["review"]
    assert done["status"] == "done" and done["summary"].startswith("One sheet") and done["gaps"][0]["what"].startswith("No single line")
    assert done["cost_usd"] == pytest.approx(0.042, abs=0.001) and done["finished_at"]
    # closed: reading the sheet again is refused, finishing again is a no-op
    assert client.post(f"/api/projects/{slug}/drawings/reviews/{rv['id']}/sheets/{sheet_id}").status_code == 400
    assert client.post(f"/api/projects/{slug}/drawings/reviews/{rv['id']}/finish").json()["review"]["status"] == "done"
    page = client.get(f"/api/projects/{slug}").json()
    assert page["drawings_reviews"][0]["id"] == rv["id"]
    run = next(r for r in page["runs"] if r["id"] == rv["run_id"])
    assert run["kind"] == "drawings" and run["status"] == "done"


def test_half_done_review_continues_and_a_sheet_that_read_nothing_is_marked_failed(client, tmp_path):
    slug = _seed(client, tmp_path)
    rv = client.post(f"/api/projects/{slug}/drawings/reviews", json={"discipline": "AR"}).json()["review"]
    ids = [r["sheet_id"] for r in rv["sheets"]]
    FakeDrawingsAgent.calls = [{"tool": "record_sheet_summary", "summary": "Site plan."}]
    assert client.post(f"/api/projects/{slug}/drawings/reviews/{rv['id']}/sheets/{ids[0]}").status_code == 200
    FakeDrawingsAgent.calls = []                                     # the model records nothing on the second sheet
    r = client.post(f"/api/projects/{slug}/drawings/reviews/{rv['id']}/sheets/{ids[1]}")
    assert r.status_code == 502
    rv = client.get(f"/api/projects/{slug}").json()["drawings_reviews"][0]
    assert rv["status"] == "reading" and [s["status"] for s in rv["sheets"]] == ["done", "failed", "pending", "pending"]
    assert rv["cost_usd"] > 0.02                                     # the failed call was still paid for and is shown
    # finishing with only some sheets read is allowed; the unread ones are named to the summary call
    FakeDrawingsAgent.calls = [{"tool": "record_set_summary", "summary": "Partly read."}]
    done = client.post(f"/api/projects/{slug}/drawings/reviews/{rv['id']}/finish").json()["review"]
    assert done["status"] == "done"
    assert "not read" in next(c["text"] for c in FakeDrawingsAgent.prompts[-1] if "FINDINGS" in c.get("text", ""))
    # 404 on another project's review, and delete
    assert client.post(f"/api/projects/{slug}/drawings/reviews/drw_nope/finish").status_code == 404
    assert client.delete(f"/api/projects/{slug}/drawings/reviews/{rv['id']}").json() == {"ok": True}
    assert client.get(f"/api/projects/{slug}").json()["drawings_reviews"] == []
