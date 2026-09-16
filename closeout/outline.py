"""Clean plan outlines for field review: bold walls, faint-but-readable labels, dropped hairline clutter.

Deterministic image processing. No model. Pin coordinates stay on the original sheet, so a pin
tapped on the outline is the same spot as on the full drawing.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

# Room labels, fixtures and other fine linework are kept at this grey so the sheet stays readable
# while the wall structure (drawn in black) still reads as the outline. 0 = black, 255 = paper.
DETAIL_GREY = 145


def outline_path(image_path: Path) -> Path:
    """Where the cached outline sits, next to the full sheet render."""
    src = Path(image_path)
    return src.with_name(src.stem + ".outline.png")


def ensure_outline(image_path: Path) -> Path:
    """Build (or reuse) the simplified outline for a sheet image."""
    src = Path(image_path)
    out = outline_path(src)
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    with Image.open(src) as im:
        simplify_plan(im).save(out, "PNG")
    return out


def simplify_plan(im: Image.Image) -> Image.Image:
    """Bold the wall structure and keep the labels readable; drop only hairline clutter.

    Two tiers on white paper: every stroke that survives a light clean (room labels, doors,
    fixtures, stairs) is drawn in grey so the engineer can still read the plan, and the
    wall-thick strokes are drawn over them in black so the outline still reads at a glance.

    The result is the same size as `im`, so a pin at (x, y) on the outline is the same
    place as on the original sheet.
    """
    gray = im.convert("L")
    if ImageStat.Stat(gray).mean[0] < 127:
        gray = ImageOps.invert(gray)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    # Ink is dark on paper. A high threshold keeps only the heavier strokes.
    bw = gray.point(lambda p: 0 if p < 170 else 255)
    ink = ImageOps.invert(bw)  # white = ink, for morphological filters
    # A 3px opening drops single-pixel dimension ticks and pipe/wire hairlines but keeps text.
    detail = ink.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    # A wider 5px opening isolates the wall-thick strokes from the readable detail.
    walls = detail.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    if ImageStat.Stat(walls).mean[0] < 1:
        walls = detail  # a hairline set would otherwise go blank
    out = Image.new("L", im.size, 255)
    out.paste(DETAIL_GREY, (0, 0), detail)  # labels, doors, fixtures kept legible
    out.paste(0, (0, 0), walls)             # wall structure on top, in black
    return out.convert("RGB")


def electrical_overlay(sheet: dict, sheets: list[dict]) -> dict | None:
    """The electrical floor plan that covers the same building as `sheet`, if one exists.

    Used as an optional layer on an architectural (or other) plan. None when the sheet
    itself is electrical, or when the set has no matching EL plan.
    """
    if (sheet.get("discipline") or "") == "EL":
        return None
    mine = _sheet_keys(sheet)
    if not mine:
        return None
    plans = [s for s in sheets if s.get("id") != sheet.get("id") and s.get("discipline") == "EL" and _is_plan(s)]
    scored = []
    for s in plans:
        keys = _sheet_keys(s)
        if not (mine & keys):
            continue
        title = (s.get("title") or "").upper()
        score = 2
        if "POWER" in title or "LIGHTING" in title or "ELECTR" in title:
            score += 1
        scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1] if scored else None


def _is_plan(sheet: dict) -> bool:
    kind = ((sheet.get("read") or {}).get("sheet_kind") or "").lower()
    title = (sheet.get("title") or "").upper()
    return kind in {"floor_plan", "plan"} or "FLOOR PLAN" in title or "POWER PLAN" in title or "LIGHTING PLAN" in title


def _sheet_keys(sheet: dict) -> set[str]:
    """Civic numbers and unit labels printed on the sheet, for matching buildings."""
    texts = [sheet.get("title") or "", sheet.get("sheet_number") or ""]
    read = sheet.get("read") or {}
    texts.extend(str(x) for x in (read.get("units") or []))
    for sp in read.get("spaces") or []:
        if isinstance(sp, dict):
            texts.extend(str(sp.get(k) or "") for k in ("unit", "name"))
        else:
            texts.append(str(sp))
    blob = " ".join(texts)
    return set(re.findall(r"\b\d{2,6}\b", blob)) | {t.strip().lower() for t in texts if t.strip()}
