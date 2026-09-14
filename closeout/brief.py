"""Before the walk: what the reviewer should know about one discipline before setting foot on site.

Deterministic and read-only. Built from what the project already holds: the drawing issues on file, the printed
words that changed between the last two issues (the revisions comparison), the items still open from the earlier
reviews, the documents the folder check found missing, and who the drawings name for this discipline. No model call.
"""
from __future__ import annotations

from typing import Callable

from . import revisions
from .review import DISCIPLINES
from .store import Store

Compare = Callable[[dict, dict], dict]      # (old document, new document) -> the comparison; the api caches it

ACCEPTED = "accept"
TOP_CHANGED = 6
TOP_LINES = 3

_PARTY_WORDS = {"AR": ("architect",), "EL": ("electrical",), "PL": ("plumbing",), "ME": ("mechanical",),
                "ST": ("structural",), "CV": ("civil",), "LA": ("landscape",), "FP": ("fire", "sprinkler"),
                "SP": ("sprinkler", "fire")}


def field_brief(store: Store, project_id: str, discipline: str, compare: Compare | None = None) -> dict:
    prj = store.project(project_id)
    if not prj:
        raise ValueError("no such project")
    docs = store.documents(project_id)
    kinds = revisions.issues_by_kind(docs, discipline)
    chain = kinds["set"]
    current = next((i for i in reversed(chain) if i["is_current"]), chain[-1] if chain else None)
    previous = None
    if current:
        earlier = [i for i in chain if (i["dated"] or "") < (current["dated"] or "")]
        previous = earlier[-1] if earlier else None
    by_id = {d["id"]: d for d in docs}

    changes = None
    if current and previous and compare is not None:
        try:
            cmp = compare(by_id[previous["id"]], by_id[current["id"]])
        except Exception as exc:  # a missing file must not take the card down with it
            changes = {"error": str(exc)[:200]}
        else:
            changed = sorted(cmp.get("changed", []), key=lambda c: c.get("alike", 1.0))
            changes = {
                "added": len(cmp.get("added", [])), "removed": len(cmp.get("removed", [])),
                "renumbered": len(cmp.get("renumbered", [])), "changed": len(changed), "unchanged": len(cmp.get("unchanged", [])),
                "sheets": [{"number": c["number"], "title": c["title"], "now_says": c.get("now_says", [])[:TOP_LINES],
                            "more": max(0, len(c.get("now_says", [])) - TOP_LINES) + len(c.get("no_longer_says", []))}
                           for c in changed[:TOP_CHANGED]],
                "more_sheets": max(0, len(changed) - TOP_CHANGED),
                "who_else": cmp.get("who_else", []),
                "basis": cmp.get("basis", ""),
            }

    # what is still open from the earlier walks in this discipline
    latest_call: dict[str, dict] = {}
    for c in store.decisions(project_id):
        latest_call[c["item_id"]] = c
    reviews = [r for r in store.reviews(project_id) if r["discipline"] == discipline]
    finished = [r for r in reviews if r["status"] == "finished"]
    titles = {r["id"]: r["title"] for r in reviews}
    open_items = []
    for d in store.deficiencies(project_id):
        if d.get("discipline") != discipline or d.get("source") != "field":
            continue
        if not d.get("review_id") or d["review_id"] not in {r["id"] for r in finished}:
            continue
        call = latest_call.get(d["item_id"])
        if call and call["decision"] == ACCEPTED:
            continue
        open_items.append({"item_id": d["item_id"], "description": d["description"], "location": d.get("location", ""),
                           "unit": d.get("unit", ""), "level": d.get("level", ""), "review": titles.get(d["review_id"], ""),
                           "status": (call or {}).get("decision", "open"), "photo": bool(d.get("reference_photo"))})

    last = finished[-1] if finished else None
    last_review = None
    if last:
        last_review = {"id": last["id"], "title": last["title"], "finished_at": last.get("finished_at"),
                       "count": sum(1 for d in store.deficiencies(project_id) if d.get("review_id") == last["id"])}

    # documents the folder check found missing for this discipline
    review = prj.get("docs_review") or {}
    missing = [{"what": m.get("what", ""), "why": m.get("why", ""), "building": m.get("building", "")}
               for m in review.get("missing", []) if (m.get("discipline") or "") == discipline]
    general_missing = sum(1 for m in review.get("missing", []) if not m.get("discipline"))

    # who the drawings name for this discipline
    words = _PARTY_WORDS.get(discipline, (DISCIPLINES.get(discipline, discipline).lower(),))
    party = next(({"role": p.get("role", ""), "company": p.get("company", "")} for p in (prj["model"].get("parties") or [])
                  if any(w in (p.get("role") or "").lower() for w in words)), None)
    if party and party["company"].lower().startswith("not stated"):
        party["company"] = ""

    return {
        "discipline": discipline, "discipline_name": DISCIPLINES.get(discipline, discipline),
        "current": current, "previous": previous, "issues": len(chain), "other_on_file": kinds["other"],
        "changes": changes, "open_items": open_items, "last_review": last_review,
        "missing": missing, "general_missing": general_missing, "party": party,
        "reviews_done": len(finished),
    }
