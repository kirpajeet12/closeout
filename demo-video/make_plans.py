#!/usr/bin/env python3
"""Fictional drawing sets for the Closeout demo film: a four-unit townhouse row.

Every sheet says FOR DEMONSTRATION ONLY / FICTIONAL PROJECT in the title block. No real firm, person or address.
Writes one multi-page PDF per discipline into demo-video/project/<AR|EL>/ and the sheet facts to plans.json
(the floor-plan box on each sheet, which the seed script stores so no model reading is needed).
"""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
OUT = HERE / "project"
W, H = 5184, 3456  # 36 x 24 in at 144 dpi
INK, GREY, LIGHT, WHITE, BLUE = (28, 28, 30), (140, 140, 140), (210, 210, 210), (255, 255, 255), (40, 90, 170)
FD = "/System/Library/Fonts/Supplemental/"
PROJECT = "CEDAR ROW TOWNHOMES"
ADDRESS = "2150 CEDAR ROW"
SUB = "CLOSEOUT DEMONSTRATION PROJECT"
STREET = "CEDAR ROW"
DATE, REV = "2026-08-28", 3
MIRROR_NOTE = "2. UNITS 2 AND 4 ARE MIRRORED."
UNITS = 4
# the drawing area: four units side by side
X0, Y0, UW, UD = 520, 520, 900, 2200
X1, Y1 = X0 + UNITS * UW, Y0 + UD


def font(s, b=False):
    return ImageFont.truetype(FD + ("Arial Bold.ttf" if b else "Arial.ttf"), s)


def ctext(d, x, y, s, f, fill=INK):
    d.text((x - d.textlength(s, font=f) / 2, y), s, font=f, fill=fill)


def frame(d, number, title, level):
    d.rectangle([60, 60, W - 60, H - 60], outline=INK, width=8)
    d.rectangle([90, 90, W - 90, H - 90], outline=INK, width=3)
    x0, y0, x1, y1 = W - 900, 90, W - 90, H - 90
    d.line([x0, y0, x0, y1], fill=INK, width=5)
    d.text((x0 + 50, 160), PROJECT, font=font(50, True), fill=INK)
    d.text((x0 + 50, 240), ADDRESS, font=font(40), fill=INK)
    d.text((x0 + 50, 300), SUB, font=font(32), fill=GREY)
    for i, n in enumerate(["GENERAL NOTES", "1. VERIFY ALL DIMENSIONS ON SITE.", MIRROR_NOTE,
                           "3. SMOKE / CO ALARMS INTERCONNECTED.", "4. FICTIONAL SET FOR A FILM."]):
        d.text((x0 + 50, 520 + i * 60), n, font=font(36, i == 0), fill=INK if i == 0 else GREY)
    d.line([x0, 2300, x1, 2300], fill=INK, width=4)
    d.text((x0 + 50, 2340), "FOR DEMONSTRATION ONLY", font=font(48, True), fill=INK)
    d.text((x0 + 50, 2410), "FICTIONAL PROJECT - NOT FOR CONSTRUCTION", font=font(32), fill=GREY)
    d.text((x0 + 50, 2470), f"SCALE 1:50    DATE {DATE}    REV {REV}", font=font(32), fill=GREY)
    d.line([x0, 2560, x1, 2560], fill=INK, width=4)
    d.text((x0 + 50, 2600), title, font=font(58, True), fill=INK)
    d.text((x0 + 50, 2680), level.upper(), font=font(40), fill=INK)
    d.line([x0, 2780, x1, 2780], fill=INK, width=4)
    d.text((x0 + 50, 2820), "SHEET", font=font(34), fill=GREY)
    d.text((x0 + 50, 2870), number, font=font(190, True), fill=INK)


def wall(d, a, b, c, e, t=18):
    d.line([a, b, c, e], fill=INK, width=t)


def room(d, cx, cy, name, sz=40):
    ctext(d, cx, cy - sz / 2, name, font(sz, True))


def door_h(d, x, y, s=90):
    d.line([x, y, x + s, y], fill=WHITE, width=26)
    d.arc([x - s, y - s, x + s, y + s], 270, 360, fill=INK, width=4)
    d.line([x, y, x, y - s], fill=INK, width=5)


def stair(d, x0, y0, x1, y1, n=13):
    d.rectangle([x0, y0, x1, y1], outline=INK, width=6)
    for i in range(1, n):
        y = y0 + (y1 - y0) * i / n
        d.line([x0, y, x1, y], fill=INK, width=3)
    d.line([(x0 + x1) / 2, y1 - 20, (x0 + x1) / 2, y0 + 30], fill=INK, width=4)
    d.polygon([((x0 + x1) / 2, y0 + 10), ((x0 + x1) / 2 - 22, y0 + 50), ((x0 + x1) / 2 + 22, y0 + 50)], fill=INK)


def unit_plan(d, i, level, elec):
    ux = X0 + i * UW
    mir = i % 2 == 1  # mirrored units: stair on the other party wall
    L = lambda f: ux + (UW * (1 - f) if mir else UW * f)  # noqa: E731  fraction across the unit, mirrored
    # party walls and the envelope
    wall(d, ux, Y0, ux, Y1, 26)
    if i == UNITS - 1:
        wall(d, ux + UW, Y0, ux + UW, Y1, 26)
    wall(d, ux, Y0, ux + UW, Y0, 26)
    wall(d, ux, Y1, ux + UW, Y1, 26)
    sx0, sx1 = sorted((L(0.0) + (0 if mir else 20), L(0.3)))
    stair(d, sx0 + 20, Y0 + 900, sx1 - 10, Y0 + 1500)
    wall(d, *sorted((L(0.33), L(0.33)))[:1], Y0 + 860, L(0.33), Y0 + 1540)
    rooms = []
    if level == "Main Floor":
        wall(d, ux, Y0 + 860, ux + UW, Y0 + 860)          # living | kitchen
        wall(d, ux, Y0 + 1540, ux + UW, Y0 + 1540)        # kitchen | garage
        wall(d, L(0.33), Y0 + 1540, L(0.33), Y1)          # entry | garage
        rooms = [("LIVING / DINING", (ux + UW / 2, Y0 + 400)), ("KITCHEN", ((L(0.33) + L(1)) / 2, Y0 + 1100)),
                 ("POWDER", ((L(0.33) + L(1)) / 2, Y0 + 1420)), ("ENTRY", ((L(0) + L(0.33)) / 2, Y0 + 1850)),
                 ("GARAGE", ((L(0.33) + L(1)) / 2, Y0 + 1850))]
        wall(d, L(0.33), Y0 + 1360, L(1), Y0 + 1360, 12)
        # kitchen counter + island
        a, b = sorted((L(0.92), L(0.98)))
        d.rectangle([a, Y0 + 900, b, Y0 + 1330], outline=INK, width=4)
        a, b = sorted((L(0.5), L(0.75)))
        d.rectangle([a, Y0 + 1180, b, Y0 + 1260], outline=INK, width=4)
        door_h(d, (L(0) + L(0.33)) / 2 - 45, Y1)
    else:
        wall(d, ux, Y0 + 860, ux + UW, Y0 + 860)
        wall(d, ux, Y0 + 1540, ux + UW, Y0 + 1540)
        wall(d, ux + UW / 2, Y0, ux + UW / 2, Y0 + 860)
        wall(d, L(0.33), Y0 + 1200, L(1), Y0 + 1200, 12)
        wall(d, ux + UW / 2, Y0 + 1540, ux + UW / 2, Y1)
        rooms = [("PRIMARY BED", ((L(0) + L(0.5)) / 2 if not mir else (ux + UW * 0.75), Y0 + 400)),
                 ("ENSUITE", ((L(0.5) + L(1)) / 2 if not mir else (ux + UW * 0.25), Y0 + 400)),
                 ("BATH 2", ((L(0.33) + L(1)) / 2, Y0 + 1030)), ("LAUNDRY", ((L(0.33) + L(1)) / 2, Y0 + 1370)),
                 ("BEDROOM 2", (ux + UW * 0.25, Y0 + 1850)), ("BEDROOM 3", (ux + UW * 0.75, Y0 + 1850))]
    for n, (cx, cy) in rooms:
        room(d, cx, cy, n, 36)
    # unit tag
    ctext(d, ux + UW / 2, Y1 + 90, f"UNIT {i + 1}", font(64, True))
    ctext(d, ux + UW / 2, Y1 + 170, f"#{i + 1} - {ADDRESS}", font(34), GREY)
    if elec:
        f = font(30, True)
        # receptacles along the long walls, lights as circles, smoke alarms
        for y in range(Y0 + 160, Y1 - 100, 330):
            for x in (ux + 40, ux + UW - 40):
                d.ellipse([x - 16, y - 16, x + 16, y + 16], outline=BLUE, width=5)
                d.line([x - 16, y, x + 16, y], fill=BLUE, width=4)
        for cy in (Y0 + 430, Y0 + 1100, Y0 + 1850):
            for cx in (ux + UW * 0.3, ux + UW * 0.7):
                d.ellipse([cx - 34, cy + 70, cx + 34, cy + 138], outline=BLUE, width=5)
                d.line([cx - 24, cy + 80, cx + 24, cy + 128], fill=BLUE, width=4)
                d.line([cx + 24, cy + 80, cx - 24, cy + 128], fill=BLUE, width=4)
        for cy in (Y0 + 700, Y0 + 1700):
            cx = ux + UW / 2
            d.ellipse([cx - 40, cy - 40, cx + 40, cy + 40], outline=BLUE, width=6)
            ctext(d, cx, cy - 16, "SA", f, BLUE)
        if level == "Main Floor":
            px = L(0.95)
            d.rectangle([min(px, L(0.87)), Y0 + 1700, max(px, L(0.87)), Y0 + 1960], fill=BLUE)
            ctext(d, (px + L(0.87)) / 2 + (-150 if not mir else 150), Y0 + 1990, f"PANEL {i + 1}A", f, BLUE)
            for x in (L(0.6), L(0.8)):
                d.rectangle([x - 26, Y0 + 1330, x + 26, Y0 + 1380], outline=BLUE, width=5)
            ctext(d, (L(0.6) + L(0.8)) / 2, Y0 + 1285, "GFCI", f, BLUE)
        else:
            for x in (L(0.55), L(0.85)):
                d.rectangle([x - 26, Y0 + 1150, x + 26, Y0 + 1190], outline=BLUE, width=5)
            ctext(d, (L(0.55) + L(0.85)) / 2, Y0 + 1100, "GFCI", f, BLUE)


def sheet(number, title, level, elec=False, site=False):
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    frame(d, number, title, level if not site else "Site")
    if site:
        d.rectangle([400, 400, 4000, 3000], outline=GREY, width=6)
        d.rectangle([X0, 1100, X1, 2200], outline=INK, width=24)
        for i in range(1, UNITS):
            wall(d, X0 + i * UW, 1100, X0 + i * UW, 2200, 12)
        for i in range(UNITS):
            ctext(d, X0 + (i + 0.5) * UW, 1560, f"UNIT {i + 1}", font(64, True))
        d.rectangle([400, 2400, 4000, 2700], fill=(235, 235, 235))
        ctext(d, 2200, 2510, f"{STREET}  (FICTIONAL STREET)", font(56, True), GREY)
        for i in range(UNITS):
            d.rectangle([X0 + i * UW + 250, 2200, X0 + i * UW + 650, 2400], outline=GREY, width=4)
        ctext(d, 2200, 700, "SITE PLAN", font(80, True))
    else:
        for i in range(UNITS):
            unit_plan(d, i, level, elec)
        ctext(d, (X0 + X1) / 2, Y0 - 260, f"{level.upper()} PLAN{' - ELECTRICAL' if elec else ''}", font(80, True))
        if elec:
            d.text((X0, Y1 + 300), "LEGEND:  (-) DUPLEX RECEPTACLE    (X) CEILING LIGHT    SA  SMOKE / CO ALARM    GFCI  GROUND FAULT RECEPTACLE",
                   font=font(36), fill=BLUE)
    # north arrow
    nx, ny = 4050, 400
    d.ellipse([nx - 80, ny - 80, nx + 80, ny + 80], outline=INK, width=5)
    d.polygon([(nx, ny - 70), (nx - 26, ny + 36), (nx, ny + 10), (nx + 26, ny + 36)], fill=INK)
    return img


def main():
    sets = {
        ("AR", "Cedar Row - Architectural Set 2026-08-28.pdf"): [
            ("A-101", "SITE PLAN", None, False, True),
            ("A-201", "MAIN FLOOR PLAN", "Main Floor", False, False),
            ("A-202", "UPPER FLOOR PLAN", "Upper Floor", False, False)],
        ("EL", "Cedar Row - Electrical Set 2026-08-28.pdf"): [
            ("E-201", "MAIN FLOOR ELECTRICAL", "Main Floor", True, False),
            ("E-202", "UPPER FLOOR ELECTRICAL", "Upper Floor", True, False)],
    }
    facts = {}
    for (disc, name), pages in sets.items():
        (OUT / disc).mkdir(parents=True, exist_ok=True)
        imgs = [sheet(n, t, lv or "Site", e, s) for n, t, lv, e, s in pages]
        imgs[0].save(OUT / disc / name, "PDF", resolution=144.0, save_all=True, append_images=imgs[1:])
        imgs[1].resize((W // 4, H // 4)).save(HERE / f"preview-{disc}.png")
        # the floor-plan box on the sheet, as fractions of the page (what the viewer opens on)
        box = {"x": round((X0 - 60) / W, 4), "y": round((Y0 - 340) / H, 4), "w": round((X1 - X0 + 120) / W, 4), "h": round((UD + 560) / H, 4)}
        facts[disc] = [{"page": k + 1, "sheet_number": n, "title": t, "level": lv, **({"view": box} if lv else {})}
                       for k, (n, t, lv, e, s) in enumerate(pages)]
        print("wrote", OUT / disc / name)
    (HERE / "plans.json").write_text(json.dumps(facts, indent=2))


if __name__ == "__main__":
    main()
