"""Drawings review: Closeout reads one discipline's current set sheet by sheet, before the engineer walks the site.

Two kinds of thing come out of it, and the engineer decides what to do with each:
- what to check on site: requirements the sheet states that are easy to get wrong in the build;
- what to ask the designer: places where the set is unclear, contradictory or incomplete.

The review is bought one sheet at a time (one model call per sheet, then one short call over all the findings for the
set as a whole), so the screen can show the cost before it starts, the cost so far while it runs, and a half-done
review can be continued rather than paid for again. Nothing here changes a deficiency, a review or a document.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image
from strands import Agent, tool

from .agent import _usage
from .config import SETTINGS, Settings
from .ingest import MAX_MODEL_EDGE
from .pipeline import make_model
from .plans import COLS, GRID_COLS, GRID_ROWS, grid_image
from .project import DISCIPLINES
from .store import Store

KINDS = {"check_on_site": "Check on site", "ask_designer": "Ask the designer", "note": "Note"}
MAX_FINDINGS = 8          # per sheet
MAX_GAPS = 6              # for the set
TEXT_CHARS = 9000         # printed words sent per sheet (the rest is repeated schedules and dimensions)
OUT_PER_SHEET = 700       # answer length assumed for the estimate
OUT_SUMMARY = 500

# USD per million tokens (input, output), matched on the model id. The estimate on screen comes from these.
PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}


def price_for(model_id: str) -> tuple[float, float]:
    m = (model_id or "").lower()
    for key, p in PRICES.items():
        if key in m:
            return p
    return PRICES["sonnet"]


def cost_usd(usage: dict, model_id: str) -> float:
    pin, pout = price_for(model_id)
    return round((usage.get("inputTokens", 0) * pin + usage.get("outputTokens", 0) * pout) / 1_000_000, 4)


def add_usage(a: dict, b: dict) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, (int, float)):
            out[k] = int(out.get(k, 0)) + int(v)
    return out


SHEET_SYSTEM = f"""You review one sheet of a construction drawing set for the engineer who will walk the site and inspect the work
against these drawings. You see the sheet with a blue grid drawn over it (columns A–{COLS[GRID_COLS - 1]} left to right, rows
1–{GRID_ROWS} top to bottom), the words printed on it, what was read from it earlier, and the list of the other sheets in the set.

Record what that engineer should know before the walk. Three kinds:
- check_on_site: a requirement this sheet states that is easy to get wrong in the build and worth verifying on the walk
  (a stated rating, size, clearance, height, count or location; a device that is often left out). Quote the requirement
  as the sheet gives it, with the room, unit or level it applies to.
- ask_designer: something on this sheet that is unclear, contradictory, missing or inconsistent (a note that refers to a
  schedule or detail that is not in the set, a symbol not in the legend, two values that disagree, a drawing with no
  title or scale, a revision cloud with no note). Say exactly what is unclear, not what the answer should be.
- note: anything else worth knowing on the walk (what the sheet covers, a revision remark, an alternate shown).

Use the tools:
- record_finding(kind, what, where, why): one finding per call, at most {MAX_FINDINGS}, most important first. `where` is
  the grid cell or range the finding sits in ("C3", "B2–D4") or "title block" / "general notes" / "legend". `what` is one
  plain sentence. `why` says what on the sheet leads to it.
- record_sheet_summary(summary): one or two sentences on what the sheet shows and its state, once, at the end.
Do not list every note on the sheet: pick what matters for the walk and for closing the project. Do not repeat what the
other sheets already cover unless this sheet contradicts them. Never state that anything complies, is approved or is
acceptable: you list what to check and what to ask; the engineer decides."""

SET_SYSTEM = f"""You have the findings recorded sheet by sheet on one discipline's drawing set. Write the review of the set as a whole.
Use the tools:
- record_gap(what, why): a gap in the SET, not on one sheet: a level or building with no plan, a schedule, legend, single
  line, riser or detail that the sheets refer to but the set does not contain, a sheet in the drawing index that is not
  in the set, sheets dated differently. At most {MAX_GAPS}. Skip it when there is none.
- record_set_summary(summary): three or four plain sentences: what the set covers, the main things to check on site, the
  main things to ask the designer. Once.
Never state that the set complies or is approved. The engineer decides."""


@dataclass
class SheetContext:
    findings: list[dict] = field(default_factory=list)
    summary: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class SetContext:
    gaps: list[dict] = field(default_factory=list)
    summary: str = ""
    errors: list[str] = field(default_factory=list)


def _clean(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


def make_sheet_tools(ctx: SheetContext):
    @tool
    def record_finding(kind: str, what: str, where: str = "", why: str = "") -> str:
        """Record one thing the field-review engineer should know from this sheet. kind is check_on_site, ask_designer or
        note; what is one plain sentence; where is the grid cell or range, or title block / general notes / legend; why
        says what on the sheet leads to it."""
        kind = _clean(kind, 40).lower().replace(" ", "_")
        if kind not in KINDS:
            ctx.errors.append(f"kind '{kind}' is not one of {', '.join(KINDS)}")
            return "REJECTED: " + ctx.errors[-1]
        what = _clean(what, 240)
        if len(what) < 8:
            ctx.errors.append("what is empty")
            return "REJECTED: " + ctx.errors[-1]
        if len(ctx.findings) >= MAX_FINDINGS:
            return f"REJECTED: {MAX_FINDINGS} findings already recorded; stop"
        if any(f["what"].lower() == what.lower() for f in ctx.findings):
            return "REJECTED: already recorded"
        ctx.findings.append({"kind": kind, "what": what, "where": _clean(where, 60), "why": _clean(why, 300)})
        return "recorded"

    @tool
    def record_sheet_summary(summary: str) -> str:
        """One or two sentences on what the sheet shows and its state. Once."""
        ctx.summary = _clean(summary, 500)
        return "recorded"

    return [record_finding, record_sheet_summary]


def make_set_tools(ctx: SetContext):
    @tool
    def record_gap(what: str, why: str = "") -> str:
        """A gap in the set as a whole (a missing plan, schedule, legend, detail or sheet), one per call."""
        what = _clean(what, 240)
        if len(what) < 8:
            return "REJECTED: what is empty"
        if len(ctx.gaps) >= MAX_GAPS:
            return f"REJECTED: {MAX_GAPS} gaps already recorded; stop"
        if any(g["what"].lower() == what.lower() for g in ctx.gaps):
            return "REJECTED: already recorded"
        ctx.gaps.append({"what": what, "why": _clean(why, 300)})
        return "recorded"

    @tool
    def record_set_summary(summary: str) -> str:
        """Three or four plain sentences on the set: what it covers, what to check on site, what to ask the designer. Once."""
        ctx.summary = _clean(summary, 900)
        return "recorded"

    return [record_gap, record_set_summary]


# --- what the review is made of --------------------------------------------------------------------------------------

def set_sheets(store: Store, project_id: str, discipline: str) -> list[dict]:
    """The current sheets of one discipline, in page order."""
    return [sh for sh in store.sheets(project_id) if sh["discipline"] == discipline]


def _reading_text(read: dict) -> str:
    if not read:
        return ""
    bits = []
    for key in ("sheet_kind", "summary", "levels", "units", "spaces", "elements", "notes"):
        v = read.get(key)
        if v:
            bits.append(f"{key}: {json.dumps(v, ensure_ascii=False)[:1400]}")
    return "\n".join(bits)


def _set_text(sheets: list[dict], index: dict) -> str:
    lines = [f"{sh.get('sheet_number') or 'p.' + str(sh['page'])}: {sh.get('title', '')}" for sh in sheets]
    prefixes = {str(sh.get("sheet_number") or "")[:1].upper() for sh in sheets if sh.get("sheet_number")}
    idx = [f"{k}: {v}" for k, v in (index or {}).items() if str(k)[:1].upper() in prefixes]
    out = "SHEETS IN THE SET:\n" + "\n".join(lines)
    if idx:
        out += "\n\nDRAWING INDEX AS PRINTED (sheets the set says it contains):\n" + "\n".join(idx[:60])
    return out


def _image_tokens(path: Path) -> int:
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        return 2000
    scale = min(1.0, MAX_MODEL_EDGE / max(w, h))
    return int(w * scale * h * scale / 750)


def estimate(store: Store, project_id: str, discipline: str, settings: Settings = SETTINGS) -> dict:
    """What the review will cost before it is bought, from the sheets' sizes and the price of the model in use."""
    sheets = set_sheets(store, project_id, discipline)
    prj = store.project(project_id) or {}
    set_text = _set_text(sheets, (prj.get("model") or {}).get("drawing_index") or {})
    fixed = len(SHEET_SYSTEM) // 4 + len(set_text) // 4 + 80
    tin = tout = 0
    for sh in sheets:
        tin += fixed + _image_tokens(Path(sh["image_path"])) + min(len(sh.get("text") or ""), TEXT_CHARS) // 4 + len(_reading_text(sh.get("read") or {})) // 4
        tout += OUT_PER_SHEET
    if sheets:
        tin += len(SET_SYSTEM) // 4 + 60 * MAX_FINDINGS // 2 * len(sheets) + 200
        tout += OUT_SUMMARY
    usage = {"inputTokens": tin, "outputTokens": tout}
    usd = cost_usd(usage, settings.model_id)
    return {"discipline": discipline, "discipline_name": DISCIPLINES.get(discipline, discipline), "sheets": len(sheets),
            "usd": usd, "usd_low": round(usd * 0.7, 2), "usd_high": round(usd * 1.5, 2), "model_id": settings.model_id}


def start(store: Store, project_id: str, discipline: str, settings: Settings = SETTINGS) -> dict:
    """Open a review of the discipline's current set: one run, every sheet pending. Continuing an unfinished one is the
    caller's job (it should look before it starts a new one)."""
    sheets = set_sheets(store, project_id, discipline)
    if not sheets:
        raise ValueError(f"no current {DISCIPLINES.get(discipline, discipline)} sheets on this project")
    run_id = store.create_run(project_id, batch_id="", model_id=settings.model_id, kind="drawings")
    rows = [{"sheet_id": sh["id"], "sheet_number": sh.get("sheet_number", ""), "title": sh.get("title", ""), "page": sh["page"],
             "status": "pending", "summary": "", "findings": [], "usage": {}} for sh in sheets]
    return store.create_drawings_review(project_id, discipline, run_id, rows)


def review_sheet(store: Store, review_id: str, sheet_id: str, settings: Settings = SETTINGS, model=None) -> dict:
    """One model call over one sheet. Writes the sheet's findings into the review and the cost so far."""
    rv = store.drawings_review(review_id)
    if not rv:
        raise ValueError("no such drawings review")
    row = next((r for r in rv["sheets"] if r["sheet_id"] == sheet_id), None)
    sh = store.sheet(sheet_id)
    if not row or not sh:
        raise ValueError("that sheet is not part of this review")
    if rv["status"] == "done":
        raise ValueError("this review is finished; start a new one to read the sheets again")
    prj = store.project(rv["project_id"]) or {}
    sheets = set_sheets(store, rv["project_id"], rv["discipline"])
    ctx = SheetContext()
    agent = Agent(model=model or make_model(settings), tools=make_sheet_tools(ctx), system_prompt=SHEET_SYSTEM, callback_handler=None)
    ref = sh.get("sheet_number") or f"{sh['discipline']} p.{sh['page']}"
    content = [
        {"text": f"SHEET {ref}: {sh.get('title', '')} ({DISCIPLINES.get(sh['discipline'], sh['discipline'])} set, page {sh['page']} of {len(sheets)})."},
        {"image": {"format": "jpeg", "source": {"bytes": grid_image(Path(sh["image_path"]))}}},
        {"text": "WHAT WAS READ FROM THIS SHEET EARLIER:\n" + (_reading_text(sh.get("read") or {}) or "(nothing)")},
        {"text": "WORDS PRINTED ON THE SHEET:\n" + (sh.get("text") or "")[:TEXT_CHARS]},
        {"text": _set_text(sheets, (prj.get("model") or {}).get("drawing_index") or {})},
        {"text": "Record the findings with record_finding, then record_sheet_summary once."},
    ]
    try:
        result = agent(content)
    except Exception as e:  # noqa: BLE001
        row.update({"status": "failed", "error": f"{type(e).__name__}: {e}"})
        store.update_drawings_review(review_id, sheets=rv["sheets"])
        raise
    usage = _usage(result)
    if not ctx.findings and not ctx.summary:
        row.update({"status": "failed", "error": "nothing recorded" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else "")})
        store.update_drawings_review(review_id, sheets=rv["sheets"], usage=add_usage(rv["usage"], usage),
                                     cost_usd=cost_usd(add_usage(rv["usage"], usage), settings.model_id))
        raise RuntimeError(row["error"])
    row.update({"status": "done", "summary": ctx.summary, "findings": ctx.findings, "usage": usage, "error": ""})
    total = add_usage(rv["usage"], usage)
    store.update_drawings_review(review_id, sheets=rv["sheets"], usage=total, cost_usd=cost_usd(total, settings.model_id))
    return {"sheet": row, "usage": usage, "cost_usd": cost_usd(total, settings.model_id), "rejections": ctx.errors}


def finish(store: Store, review_id: str, settings: Settings = SETTINGS, model=None) -> dict:
    """One short call over every sheet's findings: the set's gaps and its summary. Closes the run with the total usage."""
    rv = store.drawings_review(review_id)
    if not rv:
        raise ValueError("no such drawings review")
    done = [r for r in rv["sheets"] if r["status"] == "done"]
    if not done:
        raise ValueError("no sheet has been read yet")
    prj = store.project(rv["project_id"]) or {}
    sheets = set_sheets(store, rv["project_id"], rv["discipline"])
    lines = []
    for r in rv["sheets"]:
        head = f"{r['sheet_number'] or 'p.' + str(r['page'])}: {r['title']}"
        if r["status"] != "done":
            lines.append(f"{head} — not read")
            continue
        lines.append(f"{head} — {r['summary']}")
        for f in r["findings"]:
            lines.append(f"  [{f['kind']}] {f['what']} ({f['where']})")
    ctx = SetContext()
    agent = Agent(model=model or make_model(settings), tools=make_set_tools(ctx), system_prompt=SET_SYSTEM, callback_handler=None)
    content = [{"text": f"{DISCIPLINES.get(rv['discipline'], rv['discipline'])} set, {len(sheets)} current sheets, {len(done)} read."},
               {"text": _set_text(sheets, (prj.get("model") or {}).get("drawing_index") or {})},
               {"text": "FINDINGS SHEET BY SHEET:\n" + "\n".join(lines)},
               {"text": "Record any gaps in the set with record_gap, then record_set_summary once."}]
    result = agent(content)
    usage = _usage(result)
    total = add_usage(rv["usage"], usage)
    cost = cost_usd(total, settings.model_id)
    if not ctx.summary:
        ctx.summary = "Read sheet by sheet; see the findings under each sheet."
    store.update_drawings_review(review_id, status="done", summary=ctx.summary, gaps=ctx.gaps, usage=total, cost_usd=cost, finished=True)
    store.finish_run(rv["run_id"], "done", total)
    return store.drawings_review(review_id)
