"""The items report: one PDF per field review that travels with the covering message."""

from __future__ import annotations

import email
import io
from email import policy

from pypdf import PdfReader

from closeout import notice as notice_mod
from closeout.store import Store
from tests.test_inbox import FakeGmail, _connected, gclient  # noqa: F401  (fixture)
from tests.test_review import FakeFieldAgent, client  # noqa: F401  (fixture)
from tests.test_share import _finished_review


def _text(pdf: bytes) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages)


def test_the_report_carries_each_item_with_its_photo_and_plan_mark(client, tmp_path):
    slug, rev, _ = _finished_review(client, tmp_path)
    st = Store(client.settings.data_dir / "closeout.db")
    pid = st.project_by_slug(slug)["id"]
    name, pdf = notice_mod.build_notice(st, pid, rev["id"], "the engineer's office", "https://closeout.example/c/abc")
    assert name.endswith("_EL1_items-to-close.pdf") and pdf.startswith(b"%PDF")
    text = _text(pdf)
    for want in ("1 item to close", "EL-01", "Receptacle beside the basin has no cover plate.", "Unit C", "photo: completed",
                 "closeout.example/c/abc", "The engineer's office"):
        assert want in text, want
    reader = PdfReader(io.BytesIO(pdf))
    images = [im for page in reader.pages for im in page.images]
    assert len(images) == 2                     # the site photo and the plan close-up with the pin
    assert "Sonnet" not in text and "agent" not in text.lower()


def test_the_report_can_be_looked_at_before_sending(client, tmp_path):
    slug, rev, _ = _finished_review(client, tmp_path)
    r = client.get(f"/api/projects/{slug}/reviews/{rev['id']}/items.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert "items-to-close.pdf" in r.headers["content-disposition"]
    assert "/c/" not in _text(r.content)        # no page link until one is made
    assert client.get(f"/api/projects/{slug}/reviews/nope/items.pdf").status_code == 404


def test_the_report_goes_with_the_email_and_is_kept_on_file(gclient, tmp_path):
    client = gclient
    slug, rev, _ = _finished_review(client, tmp_path)
    client.post(f"/api/projects/{slug}/reviews/{rev['id']}/share")
    _connected(client)
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": "site@contractor.com"})
    assert r.status_code == 200, r.text
    sent = FakeGmail.sent[-1]
    assert sent["attachments"] and sent["attachments"][0][0].endswith("_items-to-close.pdf") and sent["attachments"][0][2] == "application/pdf"
    record = r.json()["send"]
    assert record["via"] == "gmail" and record["report"] == sent["attachments"][0][0]
    kept = client.settings.data_dir / "projects" / slug / "reports" / record["report"]
    assert kept.exists() and kept.read_bytes().startswith(b"%PDF")
    # the email itself, as Google would store it: text body first, the PDF as a named attachment
    msg = email.message_from_bytes(FakeGmail.mailbox[sent["id"]]["raw"], policy=policy.default)
    parts = list(msg.walk())
    assert msg.get_body(("plain",)).get_content().rstrip().endswith("/c/" + client.get(f"/api/projects/{slug}").json()["shares"][0]["id"])
    pdfs = [p for p in parts if p.get_content_type() == "application/pdf"]
    assert len(pdfs) == 1 and pdfs[0].get_filename() == record["report"]
    assert "/c/" in _text(pdfs[0].get_payload(decode=True))
