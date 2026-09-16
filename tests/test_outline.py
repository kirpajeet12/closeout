"""Plan outline: walls stay, thin dimension/pipe clutter drops. Pins keep the same coordinates."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from closeout import outline


def _drawing(path: Path, size=(400, 300)):
    im = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 360, 260), outline=(0, 0, 0), width=12)  # walls
    d.line((40, 20, 360, 20), fill=(0, 0, 0), width=1)            # dimension tick
    d.line((20, 40, 20, 260), fill=(0, 0, 0), width=1)            # pipe/wire
    im.save(path)
    return im


def test_outline_keeps_walls_and_drops_hairlines(tmp_path):
    src = tmp_path / "plan.png"
    original = _drawing(src)
    out = outline.simplify_plan(original)
    assert out.size == original.size
    # a wall pixel stays dark; a hairline dimension stays paper
    wall = out.getpixel((46, 150))[0]
    paper = out.getpixel((200, 20))[0]
    assert wall < 80 and paper > 200


def test_outline_is_cached_next_to_the_sheet(tmp_path):
    src = tmp_path / "EL-2.png"
    _drawing(src)
    p1 = outline.ensure_outline(src)
    assert p1.name == "EL-2.outline.png" and p1.exists()
    mtime = p1.stat().st_mtime
    p2 = outline.ensure_outline(src)
    assert p2 == p1 and p2.stat().st_mtime == mtime


def test_electrical_overlay_matches_the_same_building():
    ar = {"id": "a", "discipline": "AR", "title": "104 ELM ST UPPER FLOOR PLAN", "read": {"sheet_kind": "floor_plan", "units": ["#1 104 Elm St"]}}
    el = {"id": "e", "discipline": "EL", "title": "104 ELM ST UPPER FLOOR POWER PLAN", "read": {"sheet_kind": "plan", "units": ["#1 104 Elm St"]}}
    other = {"id": "p", "discipline": "PL", "title": "106 ELM ST UPPER FLOOR PLAN", "read": {"sheet_kind": "floor_plan", "units": ["#2 106 Elm St"]}}
    hit = outline.electrical_overlay(ar, [ar, el, other])
    assert hit and hit["id"] == "e"
    assert outline.electrical_overlay(el, [ar, el]) is None
