"""What Closeout does with mail that comes in: match it to a review, file the attachments, keep the text as the
contractor's word. It reads only replies in threads Closeout sent and mail carrying the office's label."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .gmail import Gmail, Incoming
from .store import Store

LINK = re.compile(r"/c/([A-Za-z0-9_-]{16,})")
DONE_LABEL = "Closeout/Filed"

# files a batch: (project_id, files, label, root, via) -> None; raises RuntimeError("busy") when the desk is taken
RunBatch = Callable[[str, list[Path], str, Path, str], None]


def place(st: Store, inc: Incoming) -> dict:
    """Which project and review a message belongs to, and how that was decided."""
    if inc.thread_id:
        s = st.send_by_thread(inc.thread_id)
        if s:
            return {"project_id": s["project_id"], "review_id": s["review_id"], "how": "thread"}
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


def check(st: Store, settings: Settings, gmail: Gmail, run_batch: RunBatch | None) -> dict:
    """One pass over the mailbox: replies in Closeout's own threads, then anything labelled for it. Each message is
    read once and labelled Filed so the next pass skips it."""
    seen = 0
    new: list[dict] = []
    for r in st.inbound_queued():
        if run_batch:
            file_queued(st, r["id"], run_batch)
    ids: list[str] = []
    for s in st.sends_with_threads():
        for mid in gmail.thread_message_ids(s["thread_id"]):
            if mid != s["message_id"] and mid not in ids:
                ids.append(mid)
    for mid in gmail.search(f"label:{settings.mail_label} -label:{DONE_LABEL}"):
        if mid not in ids:
            ids.append(mid)
    for mid in ids:
        if st.inbound_by_gmail_id(mid):
            continue
        inc = gmail.message(mid)
        seen += 1
        if inc.from_me:
            continue
        new.append(receive(st, settings, inc, run_batch))
        try:
            gmail.mark(mid, DONE_LABEL)
        except Exception:  # labelling is a courtesy; the inbound row already stops a re-read
            pass
    st.touch_mail_check("")
    return {"looked_at": seen, "new": len(new), "placed": sum(1 for r in new if r["status"] != "unplaced"),
            "unplaced": sum(1 for r in new if r["status"] == "unplaced")}
