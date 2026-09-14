"""What Closeout does with mail that comes in: match it to a review, file the attachments, keep the text as the
contractor's word. It reads only replies to what Closeout sent, mail whose subject carries a review's reference, and
(on Gmail) mail carrying the office's label."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .gmail import Incoming
from .store import Store

LINK = re.compile(r"/c/([A-Za-z0-9_-]{16,})")
REF = re.compile(r"\bCO-([0-9A-Fa-f]{6})\b")
DONE_LABEL = "Closeout/Filed"

# files a batch: (project_id, files, label, root, via) -> None; raises RuntimeError("busy") when the desk is taken
RunBatch = Callable[[str, list[Path], str, Path, str], None]


def review_ref(review_id: str) -> str:
    """The short reference a review carries in every subject Closeout sends: CO- and the last six of its id."""
    return "CO-" + review_id[-6:].upper()


def with_ref(subject: str, review_id: str) -> str:
    ref = review_ref(review_id)
    return subject if ref in subject else f"{subject.rstrip()} [{ref}]".strip()


def place(st: Store, inc: Incoming) -> dict:
    """Which project and review a message belongs to, and how that was decided."""
    for tid in [inc.thread_id, *inc.refs]:
        s = st.send_by_thread(tid) if tid else None
        if s:
            return {"project_id": s["project_id"], "review_id": s["review_id"], "how": "thread"}
    for tail in dict.fromkeys(REF.findall(inc.subject + " " + inc.text[:4000])):
        r = st.review_by_ref("CO-" + tail)
        if r:
            return {"project_id": r["project_id"], "review_id": r["id"], "how": "ref"}
    for tok in LINK.findall(inc.text + " " + inc.subject):
        sh = st.share(tok)
        if sh:
            return {"project_id": sh["project_id"], "review_id": sh["review_id"], "how": "link"}
    return {"project_id": "", "review_id": "", "how": ""}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _safe(name: str) -> str:
    name = re.sub(r"[\\/]+", "_", name).strip(". ")
    return name or "attachment"


def receive(st: Store, settings: Settings, inc: Incoming, run_batch: RunBatch | None) -> dict:
    """Record one message and, when it is placed and carries files, hand them to the filing desk."""
    if st.inbound_by_gmail_id(inc.id):
        return st.inbound_by_gmail_id(inc.id)
    where = place(st, inc)
    status = "unplaced" if not where["project_id"] else ("queued" if inc.files else "placed")
    row = st.record_inbound(gmail_id=inc.id, thread_id=inc.thread_id, project_id=where["project_id"], review_id=where["review_id"],
                            from_addr=inc.from_addr, subject=inc.subject, text=inc.text, sent_at=inc.sent_at, files=len(inc.files),
                            status=status, how=where["how"])
    if inc.files:
        folder = settings.data_dir / "uploads" / f"from-email-{_stamp()}-{row['id'][-4:]}"
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in inc.files:
            (folder / _safe(name)).write_bytes(data)
        st.set_inbound_folder(row["id"], str(folder))
    if status == "queued" and run_batch:
        file_queued(st, row["id"], run_batch)
    return st.inbound(row["id"])


def file_queued(st: Store, inbound_id: str, run_batch: RunBatch) -> bool:
    """Send a placed message's files to the filing desk. False when the desk is busy; it is tried again next check."""
    row = st.inbound(inbound_id)
    if not row or row["status"] != "queued" or not row["folder"]:
        return False
    root = Path(row["folder"])
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        st.set_inbound_status(row["id"], "placed")
        return True
    try:
        run_batch(row["project_id"], files, f"from-email-{root.name.split('-', 2)[-1]}", root, f"email:{row['id']}")
    except RuntimeError:
        return False
    st.set_inbound_status(row["id"], "placed")
    return True


def check(st: Store, settings: Settings, mailbox, run_batch: RunBatch | None) -> dict:
    """One pass over the mailbox: replies to what Closeout sent, then mail naming a review's reference, then (Gmail only)
    anything labelled for it. Each message is recorded once, so the next pass skips it. `mailbox` is the Gmail connection
    or the office's work mailbox; both answer thread_message_ids, subject_ids, message and mark."""
    seen = 0
    new: list[dict] = []
    for r in st.inbound_queued():
        if run_batch:
            file_queued(st, r["id"], run_batch)
    ids: list[str] = []

    def add(found):
        for mid in found:
            if mid not in ids:
                ids.append(mid)

    sent = set()
    try:
        for s in st.sends_with_threads():
            sent.add(s["message_id"])
            add(m for m in mailbox.thread_message_ids(s["thread_id"]) if m != s["message_id"])
        for rid in dict.fromkeys(s["review_id"] for s in st.all_sends() if s["review_id"]):
            add(mailbox.subject_ids(review_ref(rid)))
        if mailbox.kind == "gmail":
            add(mailbox.search(f"label:{settings.mail_label} -label:{DONE_LABEL}"))
        for mid in ids:
            if mid in sent or st.inbound_by_gmail_id(mid):
                continue
            inc = mailbox.message(mid)
            seen += 1
            if inc.from_me:
                continue
            new.append(receive(st, settings, inc, run_batch))
            try:
                mailbox.mark(mid, DONE_LABEL)
            except Exception:  # labelling is a courtesy; the inbound row already stops a re-read
                pass
    finally:
        mailbox.close()
    st.touch_mail_check("")
    return {"looked_at": seen, "new": len(new), "placed": sum(1 for r in new if r["status"] != "unplaced"),
            "unplaced": sum(1 for r in new if r["status"] == "unplaced")}
