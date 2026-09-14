"""Generate the synthetic batch-01 evidence set.

Photos are PLACEHOLDERS: drawn shapes plus a caption describing what a real photo
would show. They exist so the pipeline can be exercised before real, authorized photos
are dropped in. Replace them via scripts/adopt_inbox_photos.py. Nothing here is a real
site photo and nothing here is presented as agent output.
"""
from pathlib import Path
import shutil
from PIL import Image, ImageDraw, ImageFont
from PIL.TiffImagePlugin import IFDRational
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[1] / "samples" / "evidence" / "batch-01"
OUT.mkdir(parents=True, exist_ok=True)
REF = Path(__file__).resolve().parents[1] / "samples" / "register" / "photos"
REF.mkdir(parents=True, exist_ok=True)

# Fictional site: "Maple Ridge Commons". One origin, everything else is an offset in metres.
SITE_LAT, SITE_LON, SITE_ALT = 49.219400, -122.598700, 12.0
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = 111_320.0 * 0.6537   # cos(49.2 deg)


def _dms(v):
    v = abs(v); d = int(v); m = int((v - d) * 60); sec = (v - d - m / 60) * 3600
    return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(int(sec * 1000), 1000))


def exif_for(when: str, north_m: float | None, east_m: float | None, alt_m: float | None, accuracy_m: float = 6.0):
    """EXIF block with DateTimeOriginal and, when north_m is given, a GPS position offset from the site origin."""
    ex = Image.Exif()
    ex[0x010F] = "Apple"; ex[0x0110] = "iPhone 15"; ex[0x0132] = when
    ex.get_ifd(0x8769)[0x9003] = when
    if north_m is not None:
        lat = SITE_LAT + north_m / M_PER_DEG_LAT
        lon = SITE_LON + east_m / M_PER_DEG_LON
        g = ex.get_ifd(0x8825)
        g[1] = "N" if lat >= 0 else "S"; g[2] = _dms(lat)
        g[3] = "E" if lon >= 0 else "W"; g[4] = _dms(lon)
        g[5] = b"\x00"; g[6] = IFDRational(int(abs(alt_m) * 10), 10)
        g[0x1F] = IFDRational(int(accuracy_m * 10), 10)
    return ex.tobytes()

def font(size):
    for p in ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()

def photo(name, caption, scene, bg=(214, 210, 200), stamp=None, exif=None, out=None):
    im = Image.new("RGB", (1200, 900), bg)
    d = ImageDraw.Draw(im)
    scene(d)
    d.rectangle([0, 780, 1200, 900], fill=(30, 30, 30))
    d.text((30, 800), "PLACEHOLDER PHOTO (synthetic)", font=font(28), fill=(255, 200, 80))
    d.text((30, 840), caption, font=font(26), fill=(240, 240, 240))
    if stamp:
        d.text((30, 30), stamp, font=font(30), fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    kw = {"quality": 88}
    if exif:
        kw["exif"] = exif
    im.save((out or OUT) / name, **kw)

def firestop(d, pipe_w=90, sealed=True, fixtures=None):
    d.rectangle([0, 0, 1200, 780], fill=(200, 196, 186))          # wall
    if sealed:
        d.ellipse([600 - pipe_w, 390 - pipe_w, 600 + pipe_w, 390 + pipe_w], fill=(230, 60, 40))  # sealant ring
    else:
        d.ellipse([600 - pipe_w, 390 - pipe_w, 600 + pipe_w, 390 + pipe_w], fill=(40, 40, 40))   # open annular gap
    d.ellipse([600 - pipe_w + 25, 390 - pipe_w + 25, 600 + pipe_w - 25, 390 + pipe_w - 25], fill=(240, 240, 240))  # pipe
    for fx in fixtures or []:
        fx(d)

def blue_conduit(d):   # runs down the left of the D-01 wall
    d.rectangle([180, 0, 215, 780], fill=(40, 90, 200))

def grey_jbox(d):      # square junction box right of the D-04 conduit
    d.rectangle([880, 300, 1000, 420], fill=(120, 120, 120)); d.rectangle([895, 315, 985, 405], fill=(150, 150, 150))

def firestop_closeup(d):
    d.rectangle([0, 0, 1200, 780], fill=(205, 200, 190))
    d.ellipse([300, 100, 900, 700], fill=(230, 60, 40))
    d.ellipse([400, 200, 800, 600], fill=(240, 240, 240))

def brick_before(d):
    brick(d)
    d.rectangle([420, 480, 780, 640], fill=(90, 70, 60))                    # damaged, loose area
    d.rectangle([60, 0, 110, 780], fill=(50, 50, 55))                        # dark downspout on the left
    d.ellipse([1040, 560, 1090, 610], fill=(180, 160, 40)); d.rectangle([1055, 610, 1075, 780], fill=(120, 120, 120))  # hose bib

def guard(d):
    d.rectangle([0, 0, 1200, 780], fill=(120, 120, 125))
    for x in range(150, 1100, 95):
        d.rectangle([x, 120, x + 22, 700], fill=(40, 40, 45))
    d.rectangle([120, 100, 1100, 130], fill=(40, 40, 45))

def guard_tape(d):
    guard(d)
    d.rectangle([300, 380, 470, 420], fill=(250, 220, 40))
    d.text((305, 385), "0   |   50   |  95mm", font=font(24), fill=(0, 0, 0))

def rtu(d):
    d.rectangle([0, 0, 1200, 780], fill=(150, 150, 150))
    d.rectangle([250, 200, 950, 620], fill=(190, 190, 195))
    for (x, y) in [(270, 220), (900, 220), (270, 580), (900, 580)]:
        d.ellipse([x, y, x + 30, y + 30], fill=(60, 60, 60))

def brick(d):
    d.rectangle([0, 0, 1200, 780], fill=(160, 80, 60))
    for row in range(0, 780, 60):
        off = 60 if (row // 60) % 2 else 0
        for x in range(-60, 1200, 120):
            d.rectangle([x + off, row, x + off + 110, row + 52], fill=(175, 90, 65), outline=(230, 220, 205), width=4)
    d.rectangle([420, 300, 780, 480], fill=(185, 100, 70), outline=(255, 255, 255), width=3)
    d.rectangle([60, 0, 110, 780], fill=(50, 50, 55))                        # same downspout on the left
    d.ellipse([1040, 560, 1090, 610], fill=(180, 160, 40)); d.rectangle([1055, 610, 1075, 780], fill=(120, 120, 120))  # same hose bib

def truck(d):
    d.rectangle([0, 0, 1200, 780], fill=(170, 200, 230))
    d.rectangle([0, 560, 1200, 780], fill=(90, 90, 90))
    d.rectangle([250, 330, 850, 560], fill=(240, 240, 240))
    d.rectangle([850, 400, 1000, 560], fill=(240, 240, 240))
    d.ellipse([320, 520, 420, 620], fill=(20, 20, 20)); d.ellipse([860, 520, 960, 620], fill=(20, 20, 20))

# 1. contractor cover note (text)
(OUT / "cover_note.txt").write_text("""From: Site Super, Northgate Mechanical & General
To: Elm Street Engineering
Subject: Deficiency photos - Maple Ridge Commons, review of Aug 28

Hi,

Attached are the photos for the fire stopping in the Level 2 corridor outside 210
(item D-01), the stair 1 guard fix (D-02), and the RTU-2 anchors (D-03).
The torque report for the RTU is still with the installer, I will send it when I get it.

Sprinkler contractor letter for 305 is attached. The after photo for 305 will come
once the ceiling tile is back in.

Brick repair at the north side is done, photo attached.

Thanks
""")

# 2-8, 11-12 photos
def main():
    # Engineer's field-review photos (Aug 28), one per item: the "before" state. GPS/altitude = where the item is.
    photo("D-01.jpg", "Open annular gap around a 100 mm white PVC pipe through a corridor wall. Blue conduit runs down the wall to the left.",
          lambda d: firestop(d, 90, sealed=False, fixtures=[blue_conduit]), exif=exif_for("2026:08:28 10:12:05", 4, 6, SITE_ALT + 4.0), out=REF)
    photo("D-02.jpg", "Stair guard with wide picket openings at a landing.", guard, exif=exif_for("2026:08:28 10:21:40", -2, -18, SITE_ALT + 2.0), out=REF)
    photo("D-03.jpg", "Rooftop unit curb, no anchor bolts at the corners.", lambda d: (rtu(d), [d.rectangle([x, y, x + 30, y + 30], fill=(190, 190, 195)) for (x, y) in [(270, 220), (900, 220), (270, 580), (900, 580)]]),
          exif=exif_for("2026:08:28 10:48:10", 10, 2, SITE_ALT + 12.0), out=REF)
    photo("D-04.jpg", "Open gap around a 50 mm steel conduit through a corridor wall. Grey junction box on the wall to the right.",
          lambda d: firestop(d, 50, sealed=False, fixtures=[grey_jbox]), exif=exif_for("2026:08:28 09:58:30", -6, 3, SITE_ALT + 0.0), out=REF)
    photo("D-05.jpg", "Sprinkler head close to a new supply duct in a ceiling space.", lambda d: (d.rectangle([0, 0, 1200, 780], fill=(90, 90, 95)), d.rectangle([200, 200, 1000, 420], fill=(170, 170, 175)), d.ellipse([560, 430, 640, 510], fill=(200, 60, 40))),
          exif=exif_for("2026:08:28 10:35:15", 8, 20, SITE_ALT + 8.0), out=REF)
    photo("D-06.jpg", "Damaged, loose brick veneer at grade. Dark downspout on the left, yellow hose bib on the right.",
          brick_before, exif=exif_for("2026:08:28 11:02:00", 26, 4, SITE_ALT + 0.0), out=REF)

    # Contractor's photos (Sept 3). Indoor GPS is jittery; IMG_2210 is outdoors on the north side.
    photo("IMG_2201_L2_corridor_firestop.jpg", "Red fire stop sealant around a 100mm white PVC pipe through a corridor wall. Product label reads HILTI CP 606. Blue conduit to the left.",
          lambda d: firestop(d, 90, fixtures=[blue_conduit]), stamp="L2 corridor outside 210", exif=exif_for("2026:09:03 14:02:11", 7, 4, SITE_ALT + 4.5, 9))
    photo("IMG_2202_stair1_guard.jpg", "Stair guard with added pickets, openings reduced. No measuring tape in frame.", guard, stamp="Stair 1",
          exif=exif_for("2026:09:03 14:10:47", -4, -15, SITE_ALT + 2.2, 12))
    photo("IMG_2203_RTU2_anchors.jpg", "Rooftop unit curb with anchor bolts visible at all four corners.", rtu, stamp="RTU-2",
          exif=exif_for("2026:09:03 14:31:03", 9, 1, SITE_ALT + 12.3, 5))
    photo("firestop_done.jpg", "Close-up of red fire stop sealant around a pipe. No wall context, no scale, no label, no location marking.", firestop_closeup,
          exif=exif_for("2026:09:03 15:20:40", None, None, None))   # location services were off: no GPS
    shutil.copyfile(OUT / "IMG_2203_RTU2_anchors.jpg", OUT / "IMG_2203_RTU2_anchors (1).jpg")   # byte-identical duplicate
    photo("IMG_2210.jpg", "Repaired area of brick veneer near grade, new mortar visible. Dark downspout on the left, yellow hose bib on the right. No location marking.",
          brick, exif=exif_for("2026:09:03 14:45:22", 24, 6, SITE_ALT + 0.4, 5))
    photo("IMG_2215.jpg", "A white pickup truck parked on a site road. No building element visible.", truck,
          exif=exif_for("2026:09:03 14:52:09", -30, 55, SITE_ALT + 0.2, 5))
    photo("IMG_2204_stair1_guard_tape.jpg", "Tape measure held across a picket opening reading approximately 95 mm.", guard_tape, stamp="Stair 1",
          exif=exif_for("2026:09:03 14:11:30", -3, -16, SITE_ALT + 2.0, 12))
    photo("IMG_2206_L3_firestop.jpg", "Red fire stop sealant around a conduit through a wall.", lambda d: firestop(d, 50), stamp="L3 corridor firestop",
          exif=exif_for("2026:09:03 14:20:15", 2, 5, SITE_ALT + 8.1, 14))

    # 9. sprinkler letter (text-based PDF, 1 page)
    c = canvas.Canvas(str(OUT / "sprinkler_letter_305.pdf"), pagesize=letter)
    c.setFont("Helvetica-Bold", 14); c.drawString(72, 720, "Fraser Valley Fire Protection Ltd.")
    c.setFont("Helvetica", 11)
    for i, line in enumerate([
        "Date: September 3, 2026",
        "To: Northgate Mechanical & General / Elm Street Engineering",
        "Re: Maple Ridge Commons - Suite 305 sprinkler head relocation",
        "",
        "This letter confirms that the pendent sprinkler head in the ceiling space of Suite 305",
        "was relocated on September 2, 2026 to provide clearance from the new supply duct.",
        "The head was re-piped from the existing branch line and the system was restored to",
        "service and pressure tested the same day.",
        "",
        "Photos will be provided by the general contractor once ceiling finishes are reinstated.",
        "",
        "Signed,",
        "R. Dhaliwal, Project Manager",
    ]):
        c.drawString(72, 690 - i * 18, line)
    c.save()

    # 10. site daily log (text-based PDF, 3 pages)
    c = canvas.Canvas(str(OUT / "site_daily_log_sept3.pdf"), pagesize=letter)
    pages = [
        ["Northgate Mechanical & General - Daily Site Log", "Project: Maple Ridge Commons", "Date: September 3, 2026", "Weather: overcast, 17 C",
         "", "Crew on site: 6 (2 carpenters, 2 mechanical, 1 fire stop sub, 1 labourer)", "Deliveries: none",
         "", "General: continued deficiency clean-up from Aug 28 field review."],
        ["Page 2 - Work completed", "",
         "- Level 2 corridor 2-C outside Suite 210: fire stop sub (Fire Safe Ltd.) sealed the 100 mm PVC",
         "  penetration with Hilti CP 606. Label applied.",
         "- Stair 1: additional pickets welded in at landing guard, openings now under 100 mm.",
         "- Level 1 corridor: not started, conduit penetration outside 105 still open."],
        ["Page 3 - Outstanding", "",
         "- RTU-2 curb anchors installed at all four corners on Sept 2. Torque report requested from",
         "  installer (Pacific Rooftop Ltd.), not yet received.",
         "- Suite 305 sprinkler: relocated by Fraser Valley Fire Protection, ceiling tile to be reinstated.",
         "- North elevation brick at gridline 4: mason repaired Sept 3 morning."],
    ]
    for lines in pages:
        c.setFont("Helvetica", 11)
        for i, line in enumerate(lines):
            c.drawString(72, 720 - i * 18, line)
        c.showPage()
    c.save()

    print("\n".join(sorted(p.name for p in OUT.iterdir())))



if __name__ == "__main__":
    main()
