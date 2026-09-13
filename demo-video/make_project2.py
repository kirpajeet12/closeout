#!/usr/bin/env python3
"""A second fictional project for the "new project" beat of the demo film: Alder Court, three rowhomes.

Built as a whole project folder, the way an office keeps one, then zipped:
    Alder Court/AR/  the current architectural set and the one it replaced
    Alder Court/EL/  the electrical set, and the electrical letter of assurance
    Alder Court/Permits/  the building permit
Every page says FOR DEMONSTRATION ONLY / FICTIONAL PROJECT. No real firm, person or address.

    python3 demo-video/make_project2.py     # writes demo-video/project2/Alder Court.zip
"""
import shutil
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

import make_plans as mp

HERE = Path(__file__).parent
ROOT = HERE / "project2" / "Alder Court"
ZIP = HERE / "project2" / "Alder Court.zip"

mp.PROJECT, mp.ADDRESS, mp.STREET = "ALDER COURT ROWHOMES", "418 ALDER COURT", "ALDER COURT"
mp.UNITS, mp.UW = 3, 1200
mp.X1 = mp.X0 + mp.UNITS * mp.UW
mp.MIRROR_NOTE = "2. UNIT 2 IS MIRRORED."


def letter(path, title, lines):
    """A one-page letter-size document (not a drawing sheet)."""
    W, H = 1224, 1584  # 8.5 x 11 in at 144 dpi
    img = Image.new("RGB", (W, H), mp.WHITE)
    d = ImageDraw.Draw(img)
    d.text((110, 120), "FOR DEMONSTRATION ONLY - FICTIONAL PROJECT", font=mp.font(22, True), fill=mp.GREY)
    d.text((110, 200), title, font=mp.font(44, True), fill=mp.INK)
    d.line([110, 280, W - 110, 280], fill=mp.INK, width=3)
    y = 330
    for ln in lines:
        d.text((110, y), ln, font=mp.font(26, ln.isupper()), fill=mp.INK)
        y += 52
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PDF", resolution=144.0)


def drawing_set(path, pages):
    imgs = [mp.sheet(n, t, lv or "Site", e, s) for n, t, lv, e, s in pages]
    path.parent.mkdir(parents=True, exist_ok=True)
    imgs[0].save(path, "PDF", resolution=144.0, save_all=True, append_images=imgs[1:])


def main():
    if ROOT.parent.exists():
        shutil.rmtree(ROOT.parent)
    ar = [("A-101", "SITE PLAN", None, False, True),
          ("A-201", "MAIN FLOOR PLAN", "Main Floor", False, False),
          ("A-202", "UPPER FLOOR PLAN", "Upper Floor", False, False)]
    el = [("E-201", "MAIN FLOOR ELECTRICAL", "Main Floor", True, False),
          ("E-202", "UPPER FLOOR ELECTRICAL", "Upper Floor", True, False)]
    mp.DATE, mp.REV = "2026-06-12", 1
    drawing_set(ROOT / "AR" / "Alder Court - Architectural Set 2026-06-12.pdf", ar)
    mp.DATE, mp.REV = "2026-09-02", 2
    drawing_set(ROOT / "AR" / "Alder Court - Architectural Set 2026-09-02.pdf", ar)
    drawing_set(ROOT / "EL" / "Alder Court - Electrical Set 2026-09-02.pdf", el)
    letter(ROOT / "Permits" / "Alder Court - Building Permit 2026-05-20.pdf", "BUILDING PERMIT", [
        "PROJECT: ALDER COURT ROWHOMES", "Address: 418 Alder Court (fictional)", "Permit: BP-0000-DEMO",
        "Work: three-unit rowhouse, two storeys, attached garages", "Issued: 2026-05-20",
        "", "Conditions:", "1. Field reviews by the registered professionals.",
        "2. Letters of assurance before occupancy."])
    letter(ROOT / "EL" / "Alder Court - Electrical Letter of Assurance 2026-05-28.pdf", "SCHEDULE B - ELECTRICAL", [
        "PROJECT: ALDER COURT ROWHOMES", "Address: 418 Alder Court (fictional)", "Discipline: Electrical",
        "Assurance of professional design and commitment", "for field review.", "Dated: 2026-05-28"])
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(ROOT.rglob("*.pdf")):
            zf.write(f, f.relative_to(ROOT.parent))
    print("wrote", ZIP, f"{ZIP.stat().st_size / 1e6:.1f} MB")
    img = mp.sheet(*el[0][:2], el[0][2], el[0][3], el[0][4]).resize((mp.W // 4, mp.H // 4))
    img.save(HERE / "project2" / "preview-EL.png")


if __name__ == "__main__":
    main()
