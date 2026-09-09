"""Generate the synthetic batch-01 evidence set.

Photos are PLACEHOLDERS: drawn shapes plus a caption describing what a real photo
would show. They exist so the pipeline can be exercised before real, authorized photos
are dropped in. Replace them via scripts/adopt_inbox_photos.py. Nothing here is a real
site photo and nothing here is presented as agent output.
"""
from pathlib import Path
import shutil
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[1] / "samples" / "evidence" / "batch-01"
OUT.mkdir(parents=True, exist_ok=True)

def font(size):
    for p in ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()

def photo(name, caption, scene, bg=(214, 210, 200), stamp=None):
    im = Image.new("RGB", (1200, 900), bg)
    d = ImageDraw.Draw(im)
    scene(d)
    d.rectangle([0, 780, 1200, 900], fill=(30, 30, 30))
    d.text((30, 800), "PLACEHOLDER PHOTO (synthetic)", font=font(28), fill=(255, 200, 80))
    d.text((30, 840), caption, font=font(26), fill=(240, 240, 240))
    if stamp:
        d.text((30, 30), stamp, font=font(30), fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    im.save(OUT / name, quality=88)

def firestop(d, pipe_w=90):
    d.rectangle([0, 0, 1200, 780], fill=(200, 196, 186))          # wall
    d.ellipse([600 - pipe_w, 390 - pipe_w, 600 + pipe_w, 390 + pipe_w], fill=(230, 60, 40))  # sealant ring
    d.ellipse([600 - pipe_w + 25, 390 - pipe_w + 25, 600 + pipe_w - 25, 390 + pipe_w - 25], fill=(240, 240, 240))  # pipe

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

def truck(d):
    d.rectangle([0, 0, 1200, 780], fill=(170, 200, 230))
    d.rectangle([0, 560, 1200, 780], fill=(90, 90, 90))
    d.rectangle([250, 330, 850, 560], fill=(240, 240, 240))
    d.rectangle([850, 400, 1000, 560], fill=(240, 240, 240))
    d.ellipse([320, 520, 420, 620], fill=(20, 20, 20)); d.ellipse([860, 520, 960, 620], fill=(20, 20, 20))

# 1. contractor cover note (text)
(OUT / "cover_note.txt").write_text("""From: Site Super, Northgate Mechanical & General
To: Voltas Engineering
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
photo("IMG_2201_L2_corridor_firestop.jpg", "Red fire stop sealant around a 100mm white PVC pipe through a corridor wall. Product label reads HILTI CP 606.", firestop, stamp="L2 corridor outside 210")
photo("IMG_2202_stair1_guard.jpg", "Stair guard with added pickets, openings reduced. No measuring tape in frame.", guard, stamp="Stair 1")
photo("IMG_2203_RTU2_anchors.jpg", "Rooftop unit curb with anchor bolts visible at all four corners.", rtu, stamp="RTU-2")
photo("firestop_done.jpg", "Red fire stop sealant around a pipe through a wall. No location marking, no label visible.", firestop)
shutil.copyfile(OUT / "IMG_2203_RTU2_anchors.jpg", OUT / "IMG_2203_RTU2_anchors (1).jpg")   # byte-identical duplicate
photo("IMG_2210.jpg", "Repaired area of brick veneer near grade, new mortar visible. No location marking.", brick)
photo("IMG_2215.jpg", "A white pickup truck parked on a site road. No building element visible.", truck)
photo("IMG_2204_stair1_guard_tape.jpg", "Tape measure held across a picket opening reading approximately 95 mm.", guard_tape, stamp="Stair 1")
photo("IMG_2206_L3_firestop.jpg", "Red fire stop sealant around a conduit through a wall.", lambda d: firestop(d, 50), stamp="L3 corridor firestop")

# 9. sprinkler letter (text-based PDF, 1 page)
c = canvas.Canvas(str(OUT / "sprinkler_letter_305.pdf"), pagesize=letter)
c.setFont("Helvetica-Bold", 14); c.drawString(72, 720, "Fraser Valley Fire Protection Ltd.")
c.setFont("Helvetica", 11)
for i, line in enumerate([
    "Date: September 3, 2026",
    "To: Northgate Mechanical & General / Voltas Engineering",
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
