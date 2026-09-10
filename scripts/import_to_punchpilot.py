"""Set up a real project in PunchPilot from a Closeout intake (project.json).

Everything real from the intake goes in: buildings, floors, disciplines, every current sheet as its own
drawing, and the sheet readings the agent already produced (so PunchPilot does not pay Bedrock to read
them again). Nothing authored goes in: no deficiencies, no photos, no contractor drops. Those come from a
real field review inside PunchPilot.

The source folder path is taken from --root and is never written anywhere; only sheet PDFs are copied.

Usage:
    .venv/bin/python scripts/import_to_punchpilot.py --root "<project folder>" \
        --intake data/projects/laurel/project.json --app-url http://localhost:3002 \
        --db "<path to the dev D1 sqlite file>"
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from closeout.service import sheet_summary  # noqa: E402

# PunchPilot's standard discipline codes; anything else is created on the project.
STANDARD = {"AR": "ARCH", "EL": "ELEC"}
EXTRA = {"PL": ("PLUMB", "Plumbing", "#0f6b41")}

# How the Laurel sheets fold into PunchPilot's building → floor tree. A sheet that shows all
# levels of one building at once stays one sheet under one floor named for what it shows.
SITE = "Site"
LEVELS = "Main / Upper / Third Floor"
LEVELS_2 = "Main / Upper Floor"
EXTERIOR = "Exterior"


def building_for(sheet: dict) -> tuple[str, str]:
    """(building name, floor name) for one intake sheet, by its title and kind."""
    title = sheet["title"].upper()
    kind = (sheet.get("read") or {}).get("sheet_kind", "")
    m = re.search(r"(689[1357]) LAUREL", title)
    if not m:
        return SITE, SITE
    addr = f"{m.group(1)} Laurel St"
    three_storey = m.group(1) in ("6895", "6893")
    if kind == "elevation":
        return addr, EXTERIOR
    return addr, (LEVELS if three_storey else LEVELS_2)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="project folder holding the discipline subfolders")
    ap.add_argument("--intake", required=True)
    ap.add_argument("--app-url", default="http://localhost:3002")
    ap.add_argument("--db", required=True, help="dev D1 sqlite file, for the readings table and two renames")
    ap.add_argument("--name", default="Laurel Street Townhouses")
    ap.add_argument("--phase", default="Construction — closeout")
    ap.add_argument("--location", default="Vancouver, BC")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--project", help="reuse an existing (empty) project id instead of creating one")
    a = ap.parse_args(argv)

    root = Path(a.root).expanduser()
    intake = json.loads(Path(a.intake).read_text())
    current = {d["discipline"]: d for d in intake["documents"] if d.get("is_current") and d.get("kind") == "drawing"}
    sheets = [s for s in intake["sheets"] if s["discipline"] in current]
    print(f"{len(sheets)} sheets across {sorted(current)}")

    # split every current set into single-page PDFs, in memory
    pages: dict[str, list[bytes]] = {}
    for code, doc in current.items():
        reader = PdfReader(str(root / doc["rel_path"]))
        out = []
        for page in reader.pages:
            w = PdfWriter(); w.add_page(page); buf = io.BytesIO(); w.write(buf); out.append(buf.getvalue())
        pages[code] = out
        print(f"  {code}: {len(out)} pages from {Path(doc['rel_path']).name}")

    plan = []
    for s in sheets:
        b, f = building_for(s)
        number = s["sheet_number"] or ("COVER" if s["page"] == 1 else f"{s['discipline']}-p{s['page']}")
        plan.append((s, b, f, number))
    for s, b, f, number in plan:
        print(f"  {b:>14} / {f:<26} {number:>6}  {s['title']}")
    if a.dry_run:
        return

    c = httpx.Client(base_url=a.app_url, timeout=60)
    codes = sorted({STANDARD[d] for d in current if d in STANDARD})
    if a.project:
        project_id = a.project
    else:
        r = c.post("/api/projects", json={"name": a.name, "phase": a.phase, "location": a.location, "disciplines": codes})
        r.raise_for_status(); project_id = r.json()["project"]["id"]
    print("project", project_id)

    def act(**body):
        r = c.post("/api/workspace", params={"project": project_id}, json=body)
        if r.status_code >= 400:
            raise SystemExit(f"{body.get('action')}: {r.status_code} {r.text[:200]}")
        return r.json()

    disc_ids: dict[str, str] = {}
    ws = c.get("/api/workspace", params={"project": project_id}).json()
    for d in ws.get("disciplines", []):
        disc_ids[d["code"]] = d["id"]
    for code, (pcode, name, color) in EXTRA.items():
        if code in current and pcode not in disc_ids:
            disc_ids[pcode] = act(action="create_discipline", code=pcode, name=name, color=color)["discipline"]["id"]
    code_of = {**STANDARD, **{k: v[0] for k, v in EXTRA.items()}}

    # the project came with "Main Building / Level 01"; that pair becomes Site / Site.
    # Short-lived connections only: holding the file open while the dev server writes locks it.
    def sql(statements):
        con = sqlite3.connect(a.db, timeout=10)
        try:
            for q, params in statements:
                con.execute(q, params)
            con.commit()
            return con
        finally:
            con.close()
    con = sqlite3.connect(a.db, timeout=10)
    first_b = con.execute("select id from buildings where project_id=? order by created_at limit 1", (project_id,)).fetchone()[0]
    first_f = con.execute("select id from floors where building_id=? order by created_at limit 1", (first_b,)).fetchone()[0]
    con.close()
    sql([("update buildings set name=?, building_type='wood_frame_residential' where id=?", (SITE, first_b)),
         ("update floors set name=? where id=?", (SITE, first_f))])
    ws = c.get("/api/workspace", params={"project": project_id}).json()
    building_ids = {b["name"]: b["id"] for b in ws["buildings"]}
    floor_ids = {(next(b["name"] for b in ws["buildings"] if b["id"] == f["buildingId"]), f["name"]): f["id"] for f in ws["floors"]}
    have = {d["sheetNumber"] for d in ws["drawings"]}
    order = [SITE, EXTERIOR, LEVELS, LEVELS_2]
    for s, b, f, number in plan:
        if b not in building_ids:
            building_ids[b] = act(action="create_building", name=b, buildingType="wood_frame_residential")["building"]["id"]
        if (b, f) not in floor_ids:
            floor_ids[(b, f)] = act(action="create_floor", buildingId=building_ids[b], name=f, sortOrder=order.index(f))["floor"]["id"]

    now = datetime.now(timezone.utc).isoformat()
    readings = []
    for s, b, f, number in plan:
        if number in have:
            print(f"  already there {number:>6}")
            continue
        pdf = pages[s["discipline"]][s["page"] - 1]
        for attempt in (1, 2, 3):
            r = c.post("/api/workspace/drawing", data={"projectId": project_id, "floorId": floor_ids[(b, f)], "disciplineId": disc_ids[code_of[s["discipline"]]],
                                                       "sheetNumber": number, "name": s["title"].title()},
                       files={"drawing": (f"{number}.pdf", pdf, "application/pdf")})
            if r.status_code == 201:
                break
            print(f"  retry {number} after {r.status_code}: {r.text[:120]}")
            time.sleep(1.5)
        else:
            raise SystemExit(f"upload of {number} failed three times")
        drawing_id = r.json()["drawing"]["id"]
        read = s.get("read")
        if read:
            readings.append(("insert or ignore into drawing_readings (id, project_id, drawing_id, page, summary, model_id, created_at) values (?,?,?,?,?,?,?)",
                             (str(uuid.uuid4()), project_id, drawing_id, 1, sheet_summary({**read, "sheet_number": number, "title": s["title"]}), read.get("model_id") or "intake", now)))
        print(f"  uploaded {number:>6} -> {b} / {f}")
    sql(readings)
    print(f"  {len(readings)} sheet readings stored")
    print(act(action="activate_project"))
    print(f"done: {a.app_url}/projects/{project_id}")


if __name__ == "__main__":
    main()
