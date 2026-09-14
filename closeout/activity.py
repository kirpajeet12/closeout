"""The office's two running lists across every project: the email that came in and went out, and what happened, newest
first. Closeout's own work (reading a folder, checking evidence, filing an email) is marked apart from the office's presses
(finishing a review, sending the items), so the office can see at a glance what was done for it."""
from __future__ import annotations

from .project import DISCIPLINES
from .store import Store

LIMIT = 300
SNIPPET = 4000

RUN_WORDS = {
    "project": "Read the project folder",
    "documents": "Checked the folder for missing documents",
    "review": "Wrote the message to the contractor",
    "batch": "Checked the evidence the contractor sent",
    "ask": "Answered a question",
}
FAILED_WORDS = {
    "project": "Could not finish reading the project folder",
    "documents": "Could not finish checking the folder",
    "review": "Could not write the message to the contractor",
    "batch": "Could not finish checking the contractor's evidence",
    "ask": "Could not answer a question",
}
WORKING_WORDS = {
    "project": "Reading the project folder",
    "documents": "Checking the folder for missing documents",
    "review": "Writing the message to the contractor",
    "batch": "Checking the evidence the contractor sent",
    "ask": "Answering a question",
}
DOC_WORDS = {"received": "new", "updated": "updated", "current": "now the set to walk with", "superseded": "replaced by a newer issue",
             "removed": "gone from the folder"}


def _review_name(rv: dict | None) -> str:
    if not rv:
        return ""
    disc = DISCIPLINES.get(rv.get("discipline") or "", "")
    return f"{rv['title']} ({disc})" if disc else rv["title"]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _context(st: Store) -> tuple[dict, dict]:
    projects = {p["id"]: {"slug": p["slug"], "name": p["name"]} for p in st.projects()}
    reviews = {rv["id"]: rv for pid in projects for rv in st.reviews(pid)}
    return projects, reviews


def emails(st: Store, projects: dict | None = None, reviews: dict | None = None) -> list[dict]:
    """Every email Closeout read from the mailbox and every message that left for a contractor, newest first."""
    if projects is None or reviews is None:
        projects, reviews = _context(st)
    out = []
    for r in st.conn.execute("SELECT * FROM inbound ORDER BY at DESC, rowid DESC LIMIT ?", (LIMIT,)):
        r = dict(r)
        out.append({"id": r["id"], "dir": "in", "at": r["at"], "sent_at": r["sent_at"], "from": r["from_addr"], "to": "",
                    "subject": r["subject"], "text": (r["text"] or "")[:SNIPPET], "files": r["files"], "status": r["status"], "how": r["how"],
                    "project": projects.get(r["project_id"]), "review": _review_name(reviews.get(r["review_id"])), "review_id": r["review_id"]})
    for r in st.conn.execute("SELECT * FROM sends ORDER BY at DESC, rowid DESC LIMIT ?", (LIMIT,)):
        r = dict(r)
        out.append({"id": r["id"], "dir": "out", "at": r["at"], "sent_at": r["at"], "from": "", "to": r["to_addr"],
                    "subject": r["subject"], "text": (r["body"] or "")[:SNIPPET], "files": 1 if r.get("report") else 0, "status": "sent",
                    "how": r["via"], "project": projects.get(r["project_id"]), "review": _review_name(reviews.get(r["review_id"])),
                    "review_id": r["review_id"]})
    out.sort(key=lambda e: e["at"] or "", reverse=True)
    return out[:LIMIT]


def updates(st: Store, projects: dict | None = None, reviews: dict | None = None) -> list[dict]:
    """What happened on every project, newest first. by: 'closeout' for its own work, 'office' for a press in the office."""
    if projects is None or reviews is None:
        projects, reviews = _context(st)
    out: list[dict] = []

    def add(at, by, what, detail, project_id, tab="", state="done"):
        if at:
            out.append({"at": at, "by": by, "what": what, "detail": detail, "project": projects.get(project_id), "tab": tab, "state": state})

    drawing_runs = {}
    for pid in projects:
        for d in st.drawings_reviews(pid):
            drawing_runs[d.get("run_id")] = d
            sheets = d.get("sheets") or []
            found = sum(len(s.get("findings") or []) for s in sheets if isinstance(s, dict))
            disc = DISCIPLINES.get(d["discipline"], d["discipline"])
            what = f"Read {_plural(len(sheets), disc.lower() + ' sheet')}" if d["status"] == "done" else f"Reading {_plural(len(sheets), disc.lower() + ' sheet')}"
            add(d.get("finished_at") or d["created_at"], "closeout", what,
                (f"{_plural(found, 'thing')} to look at on the walk" if found else "Nothing flagged") if d["status"] == "done" else "",
                pid, "drawings", "done" if d["status"] == "done" else "working")

    for r in st.runs():
        kind = r.get("kind") or "batch"
        if kind == "drawings" or r["id"] in drawing_runs or kind not in RUN_WORDS:
            continue
        detail = ""
        if kind == "batch" and r["status"] == "done":
            items = st.item_status_for_run(r["id"]).values()
            ready = sum(1 for i in items if i.get("completeness") == "complete")
            short = sum(1 for i in items if i.get("completeness") in ("incomplete", "needs_clarification"))
            detail = ", ".join(x for x in (f"{ready} ready to close" if ready else "", f"{short} still short" if short else "") if x)
        state = {"running": "working", "failed": "failed"}.get(r["status"], "done")
        what = RUN_WORDS[kind]
        if state == "failed":
            what, detail = FAILED_WORDS[kind], "It can be tried again"
        elif state == "working":
            what = WORKING_WORDS[kind]
        add(r.get("finished_at") or r["started_at"], "closeout", what, detail, r.get("project_id") or "",
            {"batch": "list", "review": "messages", "documents": "docs", "project": "", "ask": ""}[kind], state)

    for r in st.conn.execute("SELECT * FROM inbound ORDER BY at DESC LIMIT ?", (LIMIT,)):
        r = dict(r)
        who = r["from_addr"]
        files = f" with {_plural(r['files'], 'file')}" if r["files"] else ""
        if r["status"] == "unplaced":
            add(r["at"], "closeout", f"An email from {who} needs a review picked", r["subject"], r["project_id"], "messages", "needs")
        elif r["how"] == "engineer":
            add(r["at"], "office", f"Placed an email from {who}{files}", _review_name(reviews.get(r["review_id"])), r["project_id"], "messages")
        else:
            add(r["at"], "closeout", f"Filed an email from {who}{files}", _review_name(reviews.get(r["review_id"])) or r["subject"], r["project_id"], "messages")

    for r in st.conn.execute("SELECT * FROM sends ORDER BY at DESC LIMIT ?", (LIMIT,)):
        r = dict(r)
        add(r["at"], "office", f"Sent the items to {r['to_addr']}", _review_name(reviews.get(r["review_id"])), r["project_id"], "messages")

    for rv in reviews.values():
        pid = rv["project_id"]
        if rv["status"] == "finished":
            n = (rv.get("package") or {}).get("count")
            add(rv.get("finished_at"), "office", f"Finished {_review_name(rv)}", _plural(n, "item") + " for the contractor" if n is not None else "", pid, "field")
        add(rv["started_at"], "office", f"Started {_review_name(rv)}", "", pid, "field")

    for pid in projects:
        batches: dict[str, list[dict]] = {}
        for e in st.document_log(pid):
            batches.setdefault(e["at"], []).append(e)
        for at, evs in batches.items():
            counts: dict[str, int] = {}
            for e in evs:
                counts[e["kind"]] = counts.get(e["kind"], 0) + 1
            files = len({e["file"] for e in evs})
            detail = ", ".join(f"{n} {DOC_WORDS.get(k, k)}" for k, n in counts.items())
            add(at, "closeout", f"Filed {_plural(files, 'document')}", detail, pid, "docs")

    out.sort(key=lambda e: e["at"] or "", reverse=True)
    return out[:LIMIT]


def summary(st: Store) -> dict:
    projects, reviews = _context(st)
    return {"emails": emails(st, projects, reviews), "updates": updates(st, projects, reviews)}
