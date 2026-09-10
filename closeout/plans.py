"""Floor plans for the field review: which sheet is the plan for a building and floor, and where on that sheet
each floor's plan drawing sits.

Choosing the sheet is deterministic (see the web app's planFor): it scores the sheet readings. This module does the one
thing that needs eyes: finding the box of each floor plan drawing on a sheet that shows several floors at once, so the
viewer can open straight onto "Upper Floor" instead of the whole sheet. The agent sees the sheet with a lettered grid
drawn over it and answers in grid cells; code turns the cells into fractions of the sheet. Pins are never moved: a
view is only a zoom, the pin coordinates stay on the real sheet.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from strands import Agent, tool

from .agent import _usage
from .config import SETTINGS, Settings
from .ingest import MAX_MODEL_EDGE
from .pipeline import make_model
from .store import Store

COLS = "ABCDEFGHIJKLMNOP"   # up to 16 columns
GRID_COLS, GRID_ROWS = 12, 8
GRID_COLOUR = (0, 120, 255)
PLAN_KINDS = {"floor_plan", "plan"}

VIEWS_SYSTEM = """You look at one drawing sheet from a set of construction drawings. A grid is drawn over it in blue:
columns are lettered A, B, C … left to right and rows are numbered 1, 2, 3 … top to bottom, with the labels along the edges.
Find every FLOOR PLAN drawing on the sheet (a plan of one storey: rooms, walls, doors, seen from above). Ignore sections,
elevations, area overlays, tables, notes, schedules, details and the title block.
For each floor plan drawing call record_plan once:
- title: the drawing title as printed under or above it (e.g. "UPPER FLOOR PLAN").
- level: which storey it is, using exactly one of the level names given to you. If the title says "Second Floor" and the
  project calls it "Upper Floor", answer "Upper Floor".
- left_col / right_col: the first and last grid columns the drawing covers (letters, inclusive).
- top_row / bottom_row: the first and last grid rows it covers (numbers, inclusive).
Cover the whole drawing including its title, a little generous is better than cutting it. If two drawings share a level
(for example, two units drawn separately), record both.
If there is no floor plan drawing on the sheet, call record_plan with title "none" and level "" and nothing else.
"""


@dataclass
class ViewsContext:
    views: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    none: bool = False


def make_views_tools(ctx: ViewsContext, allowed_levels: set[str]):
    @tool
    def record_plan(title: str, level: str, left_col: str = "", right_col: str = "", top_row: int = 0, bottom_row: int = 0) -> str:
        """Record one floor plan drawing on the sheet: its printed title, the project level name it shows, and the grid
        columns (letters) and rows (numbers) it covers, inclusive."""
        title = " ".join(str(title or "").split())
        if title.lower() == "none":
            ctx.none = True
            return "recorded"
        level = " ".join(str(level or "").split())
        if level not in allowed_levels:
            ctx.errors.append(f"level '{level}' is not a project level (use one of: {', '.join(sorted(allowed_levels))})")
            return "REJECTED: " + ctx.errors[-1]
        l, r = str(left_col).strip().upper()[:1], str(right_col).strip().upper()[:1]
        if l not in COLS[:GRID_COLS] or r not in COLS[:GRID_COLS] or COLS.index(l) > COLS.index(r):
            ctx.errors.append(f"columns '{left_col}'–'{right_col}' are not a left-to-right range within A–{COLS[GRID_COLS - 1]}")
            return "REJECTED: " + ctx.errors[-1]
        try:
            t, b = int(top_row), int(bottom_row)
        except (TypeError, ValueError):
            t, b = 0, 0
        if not (1 <= t <= b <= GRID_ROWS):
            ctx.errors.append(f"rows '{top_row}'–'{bottom_row}' are not a top-to-bottom range within 1–{GRID_ROWS}")
            return "REJECTED: " + ctx.errors[-1]
        x, y = COLS.index(l) / GRID_COLS, (t - 1) / GRID_ROWS
        w, h = (COLS.index(r) + 1) / GRID_COLS - x, b / GRID_ROWS - y
        ctx.views.append({"title": title[:80], "level": level, "x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4),
                          "cells": f"{l}{t}–{r}{b}", "source": "agent"})
        return "recorded"
    return [record_plan]


def grid_image(image_path: Path) -> bytes:
    """The sheet, downscaled for the model, with the lettered grid drawn over it."""
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_MODEL_EDGE, MAX_MODEL_EDGE))
        w, h = im.size
        d = ImageDraw.Draw(im)
        size = max(14, min(w, h) // 45)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
        except Exception:
            font = ImageFont.load_default()
        for c in range(GRID_COLS + 1):
            x = round(c * w / GRID_COLS)
            d.line((x, 0, x, h), fill=GRID_COLOUR, width=2)
            if c < GRID_COLS:
                cx = round((c + 0.5) * w / GRID_COLS)
                for y in (2, h - size - 4):
                    d.text((cx - size // 3, y), COLS[c], fill=GRID_COLOUR, font=font)
        for r in range(GRID_ROWS + 1):
            y = round(r * h / GRID_ROWS)
            d.line((0, y, w, y), fill=GRID_COLOUR, width=2)
            if r < GRID_ROWS:
                cy = round((r + 0.5) * h / GRID_ROWS)
                for x in (2, w - size - 2):
                    d.text((x, cy - size // 2), str(r + 1), fill=GRID_COLOUR, font=font)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def project_levels(store: Store, project_id: str) -> list[str]:
    prj = store.project(project_id) or {}
    m = prj.get("model", {}) if prj else {}
    out: list[str] = []
    for l in m.get("levels", []):
        name = l if isinstance(l, str) else l.get("name", "")
        if name and name not in out:
            out.append(name)
    for u in m.get("units", []):
        for l in u.get("levels", []) if isinstance(u.get("levels"), list) else []:
            name = l if isinstance(l, str) else l.get("name", "")
            if name and name not in out:
                out.append(name)
    return out


def find_plan_views(store: Store, project_id: str, sheet_id: str, settings: Settings = SETTINGS, model=None) -> dict:
    """One agent call: where each floor plan drawing sits on this sheet. Stores the views on the sheet."""
    sh = store.sheet(sheet_id)
    if not sh or sh["project_id"] != project_id:
        raise ValueError("no such sheet in this project")
    levels = project_levels(store, project_id)
    if not levels:
        raise ValueError("the project has no level names yet; read the drawings first")
    read = sh.get("read") or {}
    ctx = ViewsContext()
    agent = Agent(model=model or make_model(settings), tools=make_views_tools(ctx, set(levels)),
                  system_prompt=VIEWS_SYSTEM, callback_handler=None)
    ref = sh.get("sheet_number") or f"{sh['discipline']} p.{sh['page']}"
    content = [
        {"text": f"SHEET {ref}: {sh.get('title', '')}. Grid: columns A–{COLS[GRID_COLS - 1]}, rows 1–{GRID_ROWS}."},
        {"image": {"format": "jpeg", "source": {"bytes": grid_image(Path(sh['image_path']))}}},
        {"text": "PROJECT LEVEL NAMES (use exactly these): " + ", ".join(levels)
                 + (f"\nWhat the sheet-reading agent found: levels {read.get('levels')}; summary: {str(read.get('summary', ''))[:600]}" if read else "")},
        {"text": "Call record_plan once per floor plan drawing."},
    ]
    result = agent(content)
    if not ctx.views and not ctx.none:
        raise RuntimeError("agent finished without recording any plan" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
    store.set_sheet_views(sheet_id, ctx.views)
    return {"sheet_id": sheet_id, "views": ctx.views, "usage": _usage(result), "rejections": ctx.errors}


def plan_sheets(store: Store, project_id: str) -> list[dict]:
    """Sheets that look like floor plans and have no views yet: what the 'map the floors' button works through."""
    out = []
    for sh in store.sheets(project_id):
        read = sh.get("read") or {}
        kind = read.get("sheet_kind", "")
        title = (sh.get("title") or "").upper()
        if (kind in PLAN_KINDS or "FLOOR PLAN" in title) and not sh.get("views"):
            out.append(sh)
    return out
