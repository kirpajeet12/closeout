"""The office's Emails and Updates lists: every project's mail in and out, and what happened, in words the office uses."""
from __future__ import annotations

import re

from closeout.store import Store
from tests.test_review import client  # noqa: F401  (fixture)
from tests.test_share import _finished_review

BANNED = re.compile(r"\b(agent|model|run|runs|token|tokens|packet|claim|rejection)\b", re.I)


def test_emails_and_updates_cover_every_project_newest_first(client, tmp_path):
    slug, rev, _ = _finished_review(client, tmp_path)
    st = Store(client.settings.data_dir / "closeout.db")
    pid = st.project_by_slug(slug)["id"]
    st.record_send(pid, rev["id"], "", "site@contractor.example", "Field review 1 (Electrical)", "Items to close", "gmail")
    st.record_inbound("g1", "t1", pid, rev["id"], "site@contractor.example", "Re: Field review 1", "Photo attached", "", 1, "placed", "thread")
    st.record_inbound("g2", "t2", pid, "", "stranger@example.com", "Quote", "Hello", "", 0, "unplaced", "")

    out = client.get("/api/activity").json()
    emails = out["emails"]
    assert [e["dir"] for e in emails].count("in") == 2 and [e["dir"] for e in emails].count("out") == 1
    placed = next(e for e in emails if e["from"] == "site@contractor.example")
    assert placed["project"]["slug"] == slug and placed["review"].startswith("Field review 1")
    assert [e["at"] for e in emails] == sorted((e["at"] for e in emails), reverse=True)

    updates = out["updates"]
    whats = [u["what"] for u in updates]
    assert any(w.startswith("Filed an email from site@contractor.example with 1 file") for w in whats)
    assert any("needs a review picked" in w for w in whats)
    assert any(w.startswith("Sent the items to site@contractor.example") for w in whats)
    assert any(w.startswith("Finished Field review 1") for w in whats)
    assert {u["by"] for u in updates} == {"closeout", "office"}
    assert next(u for u in updates if w_is(u, "Sent the items"))["by"] == "office"
    assert [u["at"] for u in updates] == sorted((u["at"] for u in updates), reverse=True)
    for u in updates:
        assert not BANNED.search(u["what"] + " " + u["detail"]), u


def w_is(u: dict, start: str) -> bool:
    return u["what"].startswith(start)


def test_activity_is_empty_without_projects(client):
    assert client.get("/api/activity").json() == {"emails": [], "updates": []}


def test_a_failed_or_unfinished_job_says_so_plainly(client):
    st = Store(client.settings.data_dir / "closeout.db")
    client.post("/api/projects/blank", json={"name": "Maple Court"})
    pid = st.project_by_slug("maple-court")["id"]
    failed = st.create_run(pid, "", "fake", kind="ask")
    st.finish_run(failed, "failed", {})
    st.create_run(pid, "", "fake", kind="documents")
    whats = {u["what"]: u for u in client.get("/api/activity").json()["updates"]}
    assert whats["Could not answer a question"]["state"] == "failed"
    assert whats["Checking the folder for missing documents"]["state"] == "working"
    assert whats["Checking the folder for missing documents"]["project"]["slug"] == "maple-court"
