"""Stages and units: which walks a discipline needs, which units each walk covered, what is still pending.

All of it is read from what the project holds. No model call anywhere in this module.
- A project has buildings, each with units (from the drawings' unit list).
- A discipline has a list of stages: the walks the office has to do over the life of the job. The list below is the usual
  one; the office can replace it per project.
- Each field review is one stage on one day, covering some units. The units come from the deficiencies recorded during the
  walk unless the reviewer edited the list.
"""
from __future__ import annotations

import re

from .store import Store

STAGES: dict[str, list[str]] = {
    "EL": ["Underground / slab", "Rough-in", "Pre-drywall", "Final"],
    "PL": ["Underground", "Rough-in", "Final"],
    "ME": ["Rough-in", "Final"],
    "AR": ["Framing", "Envelope", "Insulation", "Final"],
    "ST": ["Footings", "Foundation walls", "Framing", "Final"],
    "FP": ["Rough-in", "Final"],
    "SP": ["Underground", "Rough-in", "Final"],
    "CV": ["Services", "Final"],
    "LA": ["Final"],
}
DEFAULT_STAGES = ["Rough-in", "Final"]

_BUILDING = re.compile(r"(\d{2,6})\s+([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3})")


def stages_for(project: dict, discipline: str) -> list[str]:
    """The office's list for this project, else the usual list for the discipline."""
    custom = (project.get("stages") or {}).get(discipline)
    if custom:
        return list(custom)
    return list(STAGES.get(discipline, DEFAULT_STAGES))


def building_of(text: str) -> dict | None:
    """'#1 6895 Elm St' → {key: '6895', name: '6895 Elm St'}. Same rule as the app's screen."""
    m = _BUILDING.search(str(text or ""))
    if not m:
        return None
    return {"key": m.group(1), "name": (m.group(1) + " " + m.group(2)).rstrip(" ,")}


def buildings(project: dict) -> list[dict]:
    """Buildings → unit labels, in the order the drawings list them."""
    out: list[dict] = []
    for u in (project.get("model") or {}).get("units") or []:
        label = (u.get("label") or u.get("name") or str(u)) if isinstance(u, dict) else str(u)
        b = (building_of(u.get("address") or label) if isinstance(u, dict) else building_of(label)) or {"key": label, "name": label}
        g = next((x for x in out if x["key"] == b["key"]), None)
        if not g:
            g = {"key": b["key"], "name": b["name"], "units": []}
            out.append(g)
        if label not in g["units"]:
            g["units"].append(label)
    return out


def unit_labels(project: dict) -> list[str]:
    return [u for b in buildings(project) for u in b["units"]]


def short_unit(label: str) -> str:
    """'Unit B (#1 – 6893 Elm St)' → 'Unit B'."""
    return re.sub(r"\s*\(.*$", "", label).strip()


def review_units(review: dict, items: list[dict], known: list[str]) -> tuple[list[str], list[str]]:
    """(units this walk covered, units the deficiencies alone point to). The reviewer's edit wins when there is one."""
    auto: list[str] = []
    for d in items:
        u = (d.get("unit") or "").strip()
        if not u:
            continue
        hit = next((k for k in known if k == u or short_unit(k) == short_unit(u)), u)
        if hit not in auto:
            auto.append(hit)
    chosen = review.get("units_set")
    if chosen is None:
        return auto, auto
    return [u for u in chosen if u], auto


def decorate_reviews(store: Store, project: dict, reviews: list[dict]) -> list[dict]:
    """Each review gains units (effective), units_auto (from the deficiencies), units_edited, stage (already there)."""
    known = unit_labels(project)
    by_review: dict[str, list[dict]] = {}
    for d in store.deficiencies(project["id"]):
        by_review.setdefault(d.get("review_id") or "", []).append(d)
    out = []
    for r in reviews:
        units, auto = review_units(r, by_review.get(r["id"], []), known)
        out.append({**r, "units": units, "units_auto": auto, "units_edited": r.get("units_set") is not None})
    return out


def coverage(store: Store, project: dict, discipline: str) -> dict:
    """The grid for one discipline: every unit × every stage, with the walks that covered that cell and what is still open."""
    reviews = [r for r in decorate_reviews(store, project, store.reviews(project["id"])) if r["discipline"] == discipline]
    stages = stages_for(project, discipline)
    for r in reviews:                                   # a walk whose stage is not on the list still gets a column
        if r["stage"] and r["stage"] not in stages:
            stages.append(r["stage"])
    latest: dict[str, str] = {}
    for d in store.decisions(project["id"]):
        latest[d["item_id"]] = d.get("decision") or ""
    items = [d for d in store.deficiencies(project["id"]) if d.get("discipline") == discipline and d.get("source") == "field"]
    known = unit_labels(project)

    def unit_key(u: str) -> str:
        return next((k for k in known if k == u or short_unit(k) == short_unit(u)), u)

    open_by_unit: dict[str, int] = {}
    items_by_unit: dict[str, int] = {}
    for d in items:
        u = unit_key(d.get("unit") or "")
        items_by_unit[u] = items_by_unit.get(u, 0) + 1
        if latest.get(d["item_id"]) != "accept":
            open_by_unit[u] = open_by_unit.get(u, 0) + 1
    grid = []
    for b in buildings(project):
        rows = []
        for u in b["units"]:
            cells = {}
            for s in stages:
                walks = [{"id": r["id"], "title": r["title"], "status": r["status"], "finished_at": r.get("finished_at"), "started_at": r["started_at"],
                          "items": sum(1 for d in items if d.get("review_id") == r["id"] and unit_key(d.get("unit") or "") == u)}
                         for r in reviews if r["stage"] == s and u in r["units"]]
                cells[s] = walks
            rows.append({"label": u, "short": short_unit(u), "cells": cells, "items": items_by_unit.get(u, 0), "open": open_by_unit.get(u, 0)})
        grid.append({"key": b["key"], "name": b["name"], "units": rows})
    stage_state = []
    for s in stages:
        walks = [r for r in reviews if r["stage"] == s]
        covered = {u for r in walks for u in r["units"]}
        stage_state.append({"stage": s, "walks": len(walks), "units_covered": len(covered), "units_total": len(known),
                            "done": bool(walks) and all(u in covered for u in known) and all(r["status"] == "finished" for r in walks),
                            "started": bool(walks)})
    return {"discipline": discipline, "stages": stages, "stage_state": stage_state, "buildings": grid,
            "unstaged": [r["id"] for r in reviews if not r["stage"]],
            "general_items": items_by_unit.get("", 0), "general_open": open_by_unit.get("", 0)}
