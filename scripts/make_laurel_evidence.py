"""Generate the Laurel sample: an authored deficiency register pinned to the real drawing sheets, plus a
synthetic contractor drop against it.

The Laurel Street project is a real design-phase set, so no field review or deficiency exists for it.
The eight items here were written by hand on rooms that are on the sheets (units by letter, never by
street address). Photos are PLACEHOLDERS (shapes + a caption saying what a real photo would show) with
EXIF for a FICTIONAL site position. Nothing here is a real site photo or real agent output.
"""
from pathlib import Path
import shutil
import sys

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_placeholder_evidence import exif_for, font, photo, SITE_ALT  # noqa: E402  (fictional site origin, shared)

ROOT = Path(__file__).resolve().parents[1] / "samples" / "laurel"
REG = ROOT / "register"
REF = REG / "photos"
OUT = ROOT / "evidence" / "batch-01"
for d in (REF, OUT):
    d.mkdir(parents=True, exist_ok=True)

REGISTER = """item_id,location,description,evidence_required,review_date,discipline,sheet,reference_photo
L-01,"Unit A, Upper Floor, Bath (demising wall to Unit C, behind the tub)","Fire stopping missing at 50 mm ABS drain penetration through the 1-hour demising wall","photo: completed fire stop at the penetration with product label or product name visible",2026-09-02,Fire protection,4,photos/L-01.jpg
L-02,"Unit A, Third Floor, Balcony","Metal guard measured 1010 mm high; drawings call for a 42 in (1067 mm) guard","photo: corrected guard; photo: tape measure showing the guard height at the top rail",2026-09-02,Structural,4,photos/L-02.jpg
L-03,"Unit B, Main Floor, Mech","HRV installed but its exhaust and intake ducts are not connected to the exterior hoods","photo: HRV with both ducts connected; report: HRV balancing or commissioning report",2026-09-02,Mechanical,8,photos/L-03.jpg
L-04,"Unit E, Main Floor, Electrical Closet","150 A unit panel has no circuit directory and the house panel directory is blank","photo: completed circuit directory at both panels",2026-09-02,Electrical,11,photos/L-04.jpg
L-05,"Unit F, Upper Floor, Bedroom #2 walk-in closet","No sprinkler head in the walk-in closet; NFPA 13-D coverage incomplete","photo: installed sprinkler head in the closet; letter: sprinkler contractor's confirmation of the added head",2026-09-02,Fire protection,13,photos/L-05.jpg
L-06,"Unit C, north exterior wall at grade, beside the patio door","Cultured stone veneer cracked and loose over an area of approx. 400 mm x 300 mm","photo: repaired stone veneer area",2026-09-02,Building envelope,3,photos/L-06.jpg
L-07,"Unit C, 2nd floor, Mech.","Electric hot water tank has no seismic strapping and the T&P relief is not piped to the drain","photo: strapped tank with the relief line piped to the drain",2026-09-02,Plumbing,5,photos/L-07.jpg
L-08,"Site, 2 in water service at the property line","Backflow preventer on the water service not installed at the time of review","report: backflow preventer test report; photo: installed backflow preventer with its test tag",2026-09-02,Plumbing,PL-01,photos/L-08.jpg
"""


# ---- scenes (placeholder drawings) --------------------------------------------------------------
def wall(d, colour=(205, 200, 190)):
    d.rectangle([0, 0, 1200, 780], fill=colour)


def firestop(d, sealed, pipe_w=60):
    wall(d, (225, 222, 215))
    d.rectangle([0, 600, 1200, 780], fill=(240, 240, 240))                      # tub apron
    d.ellipse([600 - pipe_w, 380 - pipe_w, 600 + pipe_w, 380 + pipe_w], fill=(230, 60, 40) if sealed else (30, 30, 30))
    d.ellipse([600 - pipe_w + 18, 380 - pipe_w + 18, 600 + pipe_w - 18, 380 + pipe_w - 18], fill=(40, 40, 40))  # black ABS
    if sealed:
        d.rectangle([700, 300, 900, 360], fill=(250, 250, 250)); d.text((710, 315), "HILTI CP 606", font=font(26), fill=(200, 30, 30))


def balcony(d, tape=False):
    d.rectangle([0, 0, 1200, 780], fill=(150, 170, 200))                          # sky
    d.rectangle([0, 0, 1200, 420], fill=(120, 120, 115))                          # grey Hardie shake wall behind
    d.rectangle([80, 60, 130, 110], fill=(250, 230, 150))                         # wall light left of the door
    d.rectangle([160, 40, 420, 420], fill=(60, 60, 65))                           # patio door
    d.rectangle([0, 420, 1200, 780], fill=(170, 160, 150))                        # deck
    for x in range(200, 1200, 70):
        d.rectangle([x, 300, x + 14, 700], fill=(20, 20, 20))                     # black pickets
    d.rectangle([180, 280, 1200, 302], fill=(20, 20, 20))                         # top rail
    d.rectangle([1100, 280, 1130, 700], fill=(20, 20, 20))                        # corner post
    if tape:
        d.rectangle([640, 290, 690, 700], fill=(250, 220, 40)); d.text((700, 470), "1070 mm", font=font(30), fill=(0, 0, 0))


def hrv(d, connected):
    wall(d, (200, 200, 205))
    d.rectangle([350, 250, 850, 600], fill=(225, 225, 230), outline=(80, 80, 80), width=4)
    d.text((420, 400), "HRV", font=font(40), fill=(60, 60, 60))
    for x in (450, 700):
        d.rectangle([x, 60, x + 80, 250] if connected else [x, 160, x + 80, 250], fill=(180, 180, 185))
    if not connected:
        d.ellipse([440, 40, 540, 140], fill=(30, 30, 30)); d.ellipse([690, 40, 790, 140], fill=(30, 30, 30))  # open hoods


def panels(d, labelled):
    wall(d, (210, 210, 210))
    for x in (250, 700):
        d.rectangle([x, 150, x + 260, 650], fill=(150, 150, 155), outline=(60, 60, 60), width=4)
        d.rectangle([x + 30, 190, x + 230, 600], fill=(240, 240, 240))
        if labelled:
            for i in range(10):
                d.line([x + 45, 220 + i * 38, x + 215, 220 + i * 38], fill=(40, 40, 40), width=3)


def closet(d, head):
    wall(d, (235, 235, 230))
    d.rectangle([0, 0, 1200, 120], fill=(245, 245, 245))                          # ceiling
    d.rectangle([100, 140, 1100, 780], fill=(200, 190, 170))                      # shelving
    if head:
        d.ellipse([570, 90, 630, 150], fill=(200, 60, 40))


def stone(d, damaged):
    wall(d, (170, 160, 150))
    for row in range(0, 780, 70):
        for x in range(-40, 1200, 130):
            d.rectangle([x + (60 if (row // 70) % 2 else 0), row, x + 120 + (60 if (row // 70) % 2 else 0), row + 60],
                        fill=(150, 130, 110), outline=(220, 215, 205), width=3)
    if damaged:
        d.rectangle([450, 500, 800, 720], fill=(70, 60, 55))
    else:
        d.rectangle([450, 500, 800, 720], fill=(160, 140, 120), outline=(255, 255, 255), width=3)
    d.rectangle([60, 0, 100, 780], fill=(30, 30, 30))                             # black downspout on the left
    d.rectangle([1020, 300, 1140, 460], fill=(120, 120, 125))                      # grey meter base on the right


def tank(d, strapped):
    wall(d, (215, 215, 210))
    d.rectangle([450, 120, 750, 700], fill=(245, 245, 245), outline=(100, 100, 100), width=4)
    d.rectangle([380, 80, 440, 120], fill=(40, 90, 200))                          # blue-handled shutoff top left
    d.rectangle([950, 660, 1080, 700], fill=(80, 80, 80))                          # floor drain grate lower right
    if strapped:
        d.rectangle([420, 250, 780, 275], fill=(140, 140, 140)); d.rectangle([420, 520, 780, 545], fill=(140, 140, 140))
        d.rectangle([760, 300, 785, 700], fill=(200, 200, 200))                    # relief line to drain


def backflow(d, installed):
    d.rectangle([0, 0, 1200, 780], fill=(120, 110, 100))                          # soil / pit
    d.rectangle([0, 500, 1200, 560], fill=(60, 80, 140))                          # 2" water main
    if installed:
        d.rectangle([420, 440, 780, 620], fill=(180, 60, 40)); d.rectangle([760, 380, 820, 440], fill=(250, 230, 60))  # device + tag


def dumpster(d):
    d.rectangle([0, 0, 1200, 780], fill=(170, 200, 230))
    d.rectangle([0, 560, 1200, 780], fill=(120, 120, 120))
    d.rectangle([250, 300, 950, 600], fill=(40, 100, 60)); d.rectangle([250, 260, 950, 300], fill=(30, 80, 50))


def pdf(path, lines, title):
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 14); c.drawString(72, 720, title)
    c.setFont("Helvetica", 11)
    for i, line in enumerate(lines):
        c.drawString(72, 690 - i * 18, line)
    c.save()


def main():
    (REG / "register.csv").write_text(REGISTER)

    # Engineer's field-review photos (Sept 2): the "before" state, one per item.
    photo("L-01.jpg", "Open gap around a 50 mm black ABS drain through the demising wall behind the tub. White tub apron below.",
          lambda d: firestop(d, sealed=False), exif=exif_for("2026:09:02 10:05:00", 10, 4, SITE_ALT + 4.0), out=REF)
    photo("L-02.jpg", "Black metal balcony guard, short. Grey Hardie shake wall behind, wall light left of the patio door, corner post on the right.",
          balcony, exif=exif_for("2026:09:02 10:12:00", 10, 6, SITE_ALT + 8.0), out=REF)
    photo("L-03.jpg", "HRV unit on a mech room wall, two open duct hoods above it, ducts not connected.",
          lambda d: hrv(d, connected=False), exif=exif_for("2026:09:02 10:40:00", -12, 4, SITE_ALT + 0.5), out=REF)
    photo("L-04.jpg", "Two electrical panels side by side, both circuit directories blank.",
          lambda d: panels(d, labelled=False), exif=exif_for("2026:09:02 11:05:00", -30, 8, SITE_ALT + 0.5), out=REF)
    photo("L-05.jpg", "Walk-in closet ceiling with shelving, no sprinkler head.",
          lambda d: closet(d, head=False), exif=exif_for("2026:09:02 11:20:00", -44, 6, SITE_ALT + 4.0), out=REF)
    photo("L-06.jpg", "Cracked, loose cultured stone at grade. Black downspout on the left, grey meter base on the right.",
          lambda d: stone(d, damaged=True), exif=exif_for("2026:09:02 11:35:00", 22, 10, SITE_ALT + 0.0), out=REF)
    photo("L-07.jpg", "Electric hot water tank with no straps, relief valve open to the room. Blue shutoff top left, floor drain grate lower right.",
          lambda d: tank(d, strapped=False), exif=exif_for("2026:09:02 11:48:00", 12, 12, SITE_ALT + 4.2), out=REF)
    photo("L-08.jpg", "Open pit at the property line with the 2 in water main and no backflow device.",
          lambda d: backflow(d, installed=False), exif=exif_for("2026:09:02 12:02:00", 40, -20, SITE_ALT - 1.0), out=REF)

    # Contractor's drop (Sept 8)
    (OUT / "cover_note.txt").write_text("""From: Site Super, Cedar Ridge Construction
To: Voltas Engineering
Subject: Deficiency photos - Laurel, review of Sept 2

Hi,

Fire stop at the Unit A upper bath is done (L-01), photo attached with the label.
Guards on the third floor balconies were raised, photo and tape attached.
HRV in Unit B is connected now, photo attached. The balancing report is with the mechanical sub.
Sprinkler head was added in the Unit F closet, the sprinkler contractor's letter is attached;
photo to follow once the drywall patch is painted.
Stone at the patio is repaired, photo attached. Hot water tank is strapped and piped.
Backflow test report attached, the device is in.
Panel directories will be done next week.

Thanks
""")
    photo("IMG_3301_unitA_bath_firestop.jpg", "Red fire stop sealant around a 50 mm black ABS pipe behind a tub. Product label reads HILTI CP 606.",
          lambda d: firestop(d, sealed=True), stamp="Unit A upper bath", exif=exif_for("2026:09:08 13:02:10", 11, 5, SITE_ALT + 4.3, 10), out=OUT)
    photo("IMG_3302_balcony_guard.jpg", "Black metal balcony guard, taller than before. Grey Hardie shake wall behind, wall light left of the patio door, corner post on the right. No unit named.",
          balcony, stamp="3rd floor balcony", exif=exif_for("2026:09:08 13:10:30", 9, 7, SITE_ALT + 8.2, 12), out=OUT)
    photo("IMG_3303_balcony_guard_tape.jpg", "Tape measure held against the top rail of the balcony guard reading 1070 mm.",
          lambda d: balcony(d, tape=True), stamp="Unit A balcony", exif=exif_for("2026:09:08 13:11:20", 10, 6, SITE_ALT + 8.1, 12), out=OUT)
    photo("IMG_3305_HRV.jpg", "HRV unit with both ducts connected up to the hoods above it.",
          lambda d: hrv(d, connected=True), stamp="Unit B mech", exif=exif_for("2026:09:08 13:30:00", -11, 5, SITE_ALT + 0.6, 14), out=OUT)
    shutil.copyfile(OUT / "IMG_3305_HRV.jpg", OUT / "IMG_3305_HRV (1).jpg")   # byte-identical duplicate
    photo("IMG_3310.jpg", "Repaired cultured stone at grade, new mortar visible. Black downspout on the left, grey meter base on the right. No location marking.",
          lambda d: stone(d, damaged=False), exif=exif_for("2026:09:08 13:45:00", 21, 11, SITE_ALT + 0.3, 5), out=OUT)
    photo("IMG_3312_mech_tank.jpg", "Electric hot water tank with two seismic straps and a relief line running down to a floor drain. Blue shutoff top left, floor drain grate lower right.",
          lambda d: tank(d, strapped=True), stamp="Mech", exif=exif_for("2026:09:08 13:55:00", 12, 13, SITE_ALT + 4.0, 15), out=OUT)
    photo("IMG_3315.jpg", "Green garbage and recycling bins inside an enclosure. No building element visible.",
          dumpster, exif=exif_for("2026:09:08 14:05:00", 30, 40, SITE_ALT + 0.1, 5), out=OUT)
    photo("IMG_3320_closet_head.jpg", "A new pendent sprinkler head in a closet ceiling above shelving.",
          lambda d: closet(d, head=True), stamp="Unit D closet sprinkler", exif=exif_for("2026:09:08 14:15:00", -20, 5, SITE_ALT + 4.1, 14), out=OUT)

    pdf(OUT / "sprinkler_letter_unitF.pdf", [
        "Date: September 8, 2026", "To: Cedar Ridge Construction / Voltas Engineering",
        "Re: Laurel - Unit F, upper floor, bedroom #2 walk-in closet", "",
        "This letter confirms that one pendent sprinkler head was added in the walk-in closet of",
        "bedroom #2, upper floor, Unit F on September 6, 2026, piped from the existing branch line.",
        "The system was refilled and pressure tested the same day.", "",
        "A photo will be provided by the general contractor once the ceiling patch is painted.", "",
        "Signed,", "Project Manager", "Fraser Valley Fire Protection Ltd."], "Fraser Valley Fire Protection Ltd.")
    pdf(OUT / "backflow_test_report.pdf", [
        "Date of test: September 8, 2026", "Site: Laurel, water service at the property line",
        "Device: 2 in double check valve assembly, serial DC-448121, tag 7731", "",
        "Check valve #1: 2.4 psid   held tight", "Check valve #2: 2.1 psid   held tight",
        "Result: device tested, tag attached", "", "Tester: certified backflow tester #B-2210", "Pacific Backflow Services"],
        "Backflow Prevention Assembly Test Report")
    print("\n".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
