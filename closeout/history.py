"""What Closeout did on a project, as one plain list: read, drafted, sent, received, filed, with times.

Built from the tables the app already keeps; nothing is written. The office's calls on items are in it too: an
item it accepts leaves the open list but stays here, with when it was closed."""
from __future__ import annotations

from .replies import short


def decision_words(item_id: str, decision: str) -> str:
    return {"accept": f"Closed {item_id}", "reject": f"{item_id} not accepted", "hold": f"{item_id} put on hold"}.get(decision, "")


def _title(reviews: dict, review_id: str) -> str:
    r = reviews.get(review_id)
    return r["title"] if r else "a field review"


def _n(k: int, one: str, many: str | None = None) -> str:
    return f"{k} {one if k == 1 else (many or one + 's')}"


def build(sends=(), inbound=(), batches=(), shares=(), reviews=(), messages=(), filings=(), document_log=(),
          drawings_reviews=(), runs=(), decisions=(), register=(), limit: int = 80) -> list[dict]:
    by_id = {r["id"]: r for r in reviews}
    out: list[dict] = []

    def add(at: str, what: str, tab: str = "", kind: str = "") -> None:
        if at:
            out.append({"at": at, "what": what, "tab": tab, "kind": kind})

    for r in runs:
        if r.get("status") != "done":
            continue
        if r.get("kind") == "project":
            add(r["finished_at"], "Read the drawing set: the address, the units and floors, the parties, and the sheet index", "drawings", "read")
        elif r.get("kind") == "documents":
            add(r["finished_at"], "Read the project's documents and sorted them by what they are", "docs", "read")
    for d in drawings_reviews:
        if d.get("status") == "done":
            add(d.get("finished_at") or d.get("created_at"), f"Checked the {d['discipline']} drawings" + (f": {d['summary'][:140]}" if d.get("summary") else ""), "drawings", "read")
    for r in reviews:
        add(r.get("started_at"), f"Field review started: {r['title']}", "field", "review")
        if r.get("status") == "finished":
            pk = r.get("package") or {}
            add(r.get("finished_at"), f"Field review finished: {r['title']}" + (f" · {_n(pk['count'], 'item')} to close" if pk.get("count") else " · nothing to close"), "field", "review")
    for m in messages:
        add(m.get("created_at"), f"Drafted the message to the contractor: {m['subject']}", "messages", "draft")
    for s in shares:
        add(s.get("created_at"), f"Contractor link created for {_title(by_id, s['review_id'])}", "messages", "link")
        add(s.get("revoked_at"), f"Contractor link turned off for {_title(by_id, s['review_id'])}", "messages", "link")
    for s in sends:
        how = {"gmail": "from the office's Gmail", "ses": "from the office address"}.get(s["via"], "")
        if how:
            add(s["at"], f"Emailed {s['to_addr']} {how}" + (", items report attached" if s.get("report") else ""), "messages", "send")
        else:
            add(s["at"], f"Handed the message for {s['to_addr']} to the mail app", "messages", "send")
    for i in inbound:
        where = f" · {_n(i['files'], 'file')}" if i.get("files") else ""
        state = {"unplaced": "waiting for you to place it", "queued": "files waiting to be filed", "placed": "placed under " + _title(by_id, i.get("review_id", ""))}.get(i.get("status"), "")
        checked = short(i.get("check"))
        add(i["at"], f"Reply came in from {i['from_addr']}{where}" + (f" · {state}" if state else "") + (f" · {checked}" if checked else ""), "messages", "receive")
    for b in batches:
        via = b.get("via") or ""
        door = "by email" if via.startswith("email:") else "through the contractor's link" if via else "dropped in by the office"
        rs = [r for r in (b.get("runs") or []) if isinstance(r, dict)]
        done = bool(rs) and rs[-1].get("status") == "done"
        add(b.get("created_at"), f"{'Filed' if done else 'Received'} {_n(b.get('files', 0), 'file')} {door}" + ("" if done else " · not filed yet"), "messages", "file")
    for f in filings:
        who = "Closeout" if (f.get("who") or "").lower() not in ("office", "engineer") else "the office"
        add(f.get("at"), f"{f['name'] or f['file']} filed under {f.get('discipline') or f.get('building') or 'the project'} by {who}", "docs", "file")
    for d in document_log:
        add(d.get("at"), d.get("note") or f"{d.get('kind', 'note')}: {d['file']}", "docs", "doc")
    what = {d["item_id"]: d.get("description", "") for d in register}
    for d in decisions:
        said = decision_words(d["item_id"], d["decision"])
        if said:
            add(d.get("created_at"), said + (f": {what[d['item_id']][:120]}" if what.get(d["item_id"]) else "") + (f" · “{d['note']}”" if d.get("note") else ""),
                f"item/{d['item_id']}", "decision")
    out.sort(key=lambda e: e["at"], reverse=True)
    return out[:limit]
