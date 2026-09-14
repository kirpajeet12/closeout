"""What the contractor wrote back, set against the item it answers. A reply saying "done" with no photo cannot close an
item that asked for a photo; the office sees that at once, next to the words. Plain code over what is already recorded
(the email, the item's list of what to send, what has been filed): nothing is sent to a model here. When the email
carries files, its words also go with them to the filing desk, so they are weighed with the photos."""
from __future__ import annotations

import re

from .completeness import compute_item_status
from .store import Store

QUOTED = re.compile(r"^\s*(>|on\b.{0,300}\bwrote:\s*$|-{2,}\s*original message|_{8,}|from:\s|sent from my\b)", re.I)
WORDS_LIMIT = 600


def own_words(text: str) -> str:
    """The contractor's own words: everything above the quoted earlier message, on one line."""
    kept = []
    for line in (text or "").splitlines():
        if QUOTED.match(line):
            break
        kept.append(line)
    words = " ".join(" ".join(kept).split())
    return words[:WORDS_LIMIT].rstrip() + ("…" if len(words) > WORDS_LIMIT else "")


def _named(item_id: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(item_id)}(?!\w)", text, re.I))


def _needs(slot: dict) -> str:
    return f"{slot['type']}: {slot['description']}" if slot.get("type") else slot["description"]


def check(st: Store, project_id: str, inbound: list[dict] | None = None) -> dict[str, dict]:
    """For each placed email: the contractor's words, which items it answers, and where each of them stands now.
    Returns {inbound_id: {words, named, files, items: [{item_id, state, still_needs, said}]}}."""
    rows = st.inbound_for_project(project_id) if inbound is None else inbound
    rows = [r for r in rows if r.get("status") != "unplaced" and r.get("review_id")]
    if not rows:
        return {}
    findings = st.current_findings(project_id)
    calls = {d["item_id"]: d["decision"] for d in st.decisions(project_id)}
    thread_subjects: dict[str, list[str]] = {}
    for s in st.sends(project_id):
        if s.get("thread_id"):
            thread_subjects.setdefault(s["thread_id"], []).append(s["subject"])
    out = {}
    for r in rows:
        items = st.review_items(project_id, r["review_id"])
        words = own_words(r.get("text") or "")
        about = " ".join([r.get("subject") or "", words, *thread_subjects.get(r.get("thread_id") or "", [])])
        named = [d for d in items if _named(d["item_id"], about)]
        answers = named or [d for d in items if calls.get(d["item_id"]) != "accept"]
        checked = []
        for d in answers:
            stat = compute_item_status(d, findings)
            still = [_needs(s) for s in stat["missing_slots"]]
            checked.append({"item_id": d["item_id"], "state": "closed" if calls.get(d["item_id"]) == "accept" else stat["completeness"],
                            "still_needs": still, "said": _said(d["item_id"], r.get("files") or 0, r.get("status"), calls.get(d["item_id"]), stat["completeness"], still)})
        out[r["id"]] = {"words": words, "named": bool(named), "files": r.get("files") or 0, "items": checked}
    return out


def _said(item_id: str, files: int, status: str, call: str | None, completeness: str, still: list[str]) -> str:
    if call == "accept":
        return f"{item_id} is already closed."
    if status == "queued":
        return f"{item_id}: the files in this email are waiting to be checked."
    if completeness == "complete":
        return f"{item_id}: everything asked for is in. Ready for your call."
    need = "; ".join(still)
    if not files:
        return f"{item_id}: this email has no photo or document, so the words alone do not close it. Still needed: {need}."
    return f"{item_id}: still needed after this email: {need}."


def short(check: dict | None) -> str:
    """One line for the logs: what the email left open, by item."""
    if not check or not check.get("items"):
        return ""
    items = check["items"]
    if not check["named"] and len(items) > 1:
        still = sum(1 for i in items if i["state"] not in ("complete", "closed"))
        return f"{still} of {len(items)} open items still need something" if still else "every item it answers has what was asked for"
    parts = []
    for i in items:
        if i["state"] == "closed":
            parts.append(f"{i['item_id']} already closed")
        elif i["state"] == "complete":
            parts.append(f"{i['item_id']} has what was asked for")
        elif not check["files"]:
            parts.append(f"{i['item_id']}: words only, still needs {len(i['still_needs'])} thing{'' if len(i['still_needs']) == 1 else 's'}")
        else:
            parts.append(f"{i['item_id']} still needs {len(i['still_needs'])} thing{'' if len(i['still_needs']) == 1 else 's'}")
    return "; ".join(parts)
