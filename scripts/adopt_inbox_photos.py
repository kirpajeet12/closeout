"""Replace the synthetic placeholder photos with real photos from inbox/photos.

The demo needs 14 photos: six engineer "before" reference photos (samples/register/photos/D-0x.jpg)
and eight contractor "after" photos (samples/evidence/batch-01). Each has a role in the checklist
samples/expected/batch-01.json, so the real photo must show roughly the same thing. Some copies get a
burned-in location stamp (that is what drives the "explicit" tier and the D-06 conflict case).

Privacy: the copy never keeps your phone's real GPS. Every copy gets the same FICTIONAL site
coordinates, altitude and capture time that scripts/make_placeholder_evidence.py uses, so the location
signals (reference photo, sequence, GPS, altitude) behave the same with real photos as with placeholders.

Usage:
    python3 scripts/adopt_inbox_photos.py --list
    python3 scripts/adopt_inbox_photos.py D-06.jpg=inbox/photos/IMG_0010.HEIC IMG_2210.jpg=inbox/photos/IMG_0031.jpg ...

HEIC is converted with macOS `sips`. The byte-identical duplicate "IMG_2203_RTU2_anchors (1).jpg"
is regenerated automatically. Nothing in inbox/ is ever committed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_placeholder_evidence import SITE_ALT, exif_for  # noqa: E402  (shares the fictional site)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "evidence" / "batch-01"
REF = ROOT / "samples" / "register" / "photos"

# name -> (what the photo must show, stamp burned into the copy or None, exif args: when, north_m, east_m, alt_m, accuracy_m)
NEEDED = {
    # engineer's field-review photos, Aug 28: the deficiency BEFORE repair
    "D-01.jpg": ("BEFORE: open gap around a pipe through a wall; include something distinctive nearby (a conduit, a sign)", None, ("2026:08:28 10:12:05", 4, 6, SITE_ALT + 4.0, 6)),
    "D-02.jpg": ("BEFORE: a stair guard with wide picket openings", None, ("2026:08:28 10:21:40", -2, -18, SITE_ALT + 2.0, 6)),
    "D-03.jpg": ("BEFORE: rooftop unit / equipment curb without anchor bolts", None, ("2026:08:28 10:48:10", 10, 2, SITE_ALT + 12.0, 6)),
    "D-04.jpg": ("BEFORE: open gap around a smaller conduit through a wall; different surroundings from D-01", None, ("2026:08:28 09:58:30", -6, 3, SITE_ALT + 0.0, 6)),
    "D-05.jpg": ("BEFORE: a sprinkler head close to a duct or obstruction", None, ("2026:08:28 10:35:15", 8, 20, SITE_ALT + 8.0, 6)),
    "D-06.jpg": ("BEFORE: damaged brick / masonry near the ground; include two fixed features (downspout, hose bib, meter)", None, ("2026:08:28 11:02:00", 26, 4, SITE_ALT + 0.0, 6)),
    # contractor's photos, Sept 3
    "IMG_2201_L2_corridor_firestop.jpg": ("Fire-stop sealant around the D-01 pipe, same surroundings, product label readable if possible", "L2 corridor outside 210", ("2026:09:03 14:02:11", 7, 4, SITE_ALT + 4.5, 9)),
    "IMG_2202_stair1_guard.jpg": ("The D-02 guard with added pickets, NO tape measure in frame", "Stair 1", ("2026:09:03 14:10:47", -4, -15, SITE_ALT + 2.2, 12)),
    "IMG_2204_stair1_guard_tape.jpg": ("Same guard with a tape measure held across a picket opening", "Stair 1", ("2026:09:03 14:11:30", -3, -16, SITE_ALT + 2.0, 12)),
    "IMG_2203_RTU2_anchors.jpg": ("The D-03 curb with anchor bolts visible", "RTU-2", ("2026:09:03 14:31:03", 9, 1, SITE_ALT + 12.3, 5)),
    "IMG_2206_L3_firestop.jpg": ("Fire-stop sealant around a smaller conduit, NOT the D-04 spot", "L3 corridor firestop", ("2026:09:03 14:20:15", 2, 5, SITE_ALT + 8.1, 14)),
    "firestop_done.jpg": ("Tight close-up of sealant around a pipe: no wall context, no scale, nothing that says where", None, ("2026:09:03 15:20:40", None, None, None, 0)),
    "IMG_2210.jpg": ("Repaired brick at the D-06 spot with the SAME two fixed features visible, no stamp", None, ("2026:09:03 14:45:22", 24, 6, SITE_ALT + 0.4, 5)),
    "IMG_2215.jpg": ("Anything that is NOT a building element: a truck, a road, a lunch table", None, ("2026:09:03 14:52:09", -30, 55, SITE_ALT + 0.2, 5)),
}


def _font(size: int):
    for p in ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except Exception:  # noqa: BLE001
            pass
    return ImageFont.load_default()


def _to_jpeg(src: Path) -> Path:
    if src.suffix.lower() in (".heic", ".heif"):
        tmp = Path(tempfile.mkdtemp()) / (src.stem + ".jpg")
        subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(tmp)], check=True, capture_output=True)
        return tmp
    return src


def adopt(name: str, src: Path) -> None:
    if name not in NEEDED:
        raise SystemExit(f"{name} is not one of the demo photos; run with --list")
    if not src.exists():
        raise SystemExit(f"{src} does not exist")
    _, stamp, (when, north, east, alt, acc) = NEEDED[name]
    im = Image.open(_to_jpeg(src))
    im = im.convert("RGB")           # drops the phone's EXIF (real GPS, real time) entirely
    im.thumbnail((2000, 2000))
    if stamp:
        d = ImageDraw.Draw(im)
        size = max(24, im.width // 40)
        d.text((size, size), stamp, font=_font(size), fill=(255, 255, 255), stroke_width=max(2, size // 12), stroke_fill=(0, 0, 0))
    dest = (REF if name.startswith("D-") else OUT) / name
    im.save(dest, "JPEG", quality=88, exif=exif_for(when, north, east, alt, acc or 6.0))
    print(f"adopted {src.name} -> {dest.relative_to(ROOT)}" + (f" (stamp: {stamp})" if stamp else "") + (" (no GPS)" if north is None else ""))
    if name == "IMG_2203_RTU2_anchors.jpg":
        shutil.copyfile(OUT / name, OUT / "IMG_2203_RTU2_anchors (1).jpg")
        print("regenerated byte-identical duplicate IMG_2203_RTU2_anchors (1).jpg")


def main(argv: list[str]) -> int:
    if not argv or argv == ["--list"]:
        print("Demo photos needed (name: what to shoot [stamp added to the copy]):")
        for name, (what, stamp, _) in NEEDED.items():
            print(f"  {name:38} {what}" + (f"  [stamp: {stamp}]" if stamp else ""))
        inbox = ROOT / "inbox" / "photos"
        files = sorted(p.name for p in inbox.glob("*") if p.is_file() and not p.name.startswith("."))
        print(f"\ninbox/photos has {len(files)} file(s)" + (": " + ", ".join(files) if files else ""))
        return 0
    for arg in argv:
        if "=" not in arg:
            raise SystemExit(f"expected NAME=path, got {arg}")
        name, src = arg.split("=", 1)
        adopt(name, Path(src).expanduser())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
