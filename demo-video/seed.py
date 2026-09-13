#!/usr/bin/env python3
"""Build the fictional Cedar Row project in a scratch data folder for the demo film. No model calls.

    CLOSEOUT_DATA_DIR=demo-video/data .venv/bin/python demo-video/seed.py

Everything seeded is a fact of the drawings made by make_plans.py: the units, the floors, the sheet numbers and where
the floor plan sits on each sheet. Nothing is written into the real data folder.
"""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = Path(os.environ.get("CLOSEOUT_DATA_DIR", "")).resolve()
if not os.environ.get("CLOSEOUT_DATA_DIR") or DATA == (ROOT / "data").resolve():
    sys.exit("set CLOSEOUT_DATA_DIR to the scratch folder (demo-video/data); never the real data folder")
sys.path.insert(0, str(ROOT))

from closeout import project as project_mod  # noqa: E402
from closeout.config import Settings  # noqa: E402
from closeout.store import Store  # noqa: E402

SLUG = "cedar-row"
if DATA.exists():
    shutil.rmtree(DATA)
DATA.mkdir(parents=True)
settings = Settings()
store = Store(DATA / "closeout.db")
res = project_mod.import_project(store, HERE / "project", SLUG, settings, read_with_model=False)
pid = res["project_id"]
facts = json.loads((HERE / "plans.json").read_text())

levels = ["Main Floor", "Upper Floor"]
units = [{"label": f"Unit {i}", "address": f"#{i} 2150 Cedar Row", "levels": levels} for i in range(1, 5)]
spaces = {"Main Floor": ["Living / Dining", "Kitchen", "Powder", "Entry", "Garage"],
          "Upper Floor": ["Primary Bed", "Ensuite", "Bath 2", "Laundry", "Bedroom 2", "Bedroom 3"]}
prj = store.project(pid)
model = dict(prj["model"])
model.update({
    "name": "Cedar Row Townhomes", "address": "2150 Cedar Row", "city": "", "building_type": "Townhouses",
    "description": "Four townhouse units in one row, each over a main and an upper floor with an attached garage.",
    "units": units, "levels": [{"name": lv, "elevation": ""} for lv in levels],
    "parties": [], "key_facts": [], "provenance": "document",
})
store.set_project_model(pid, model)
store.conn.execute("UPDATE projects SET name=? WHERE id=?", ("Cedar Row Townhomes", pid))
store.conn.commit()

by_doc = {d["id"]: d for d in store.documents(pid)}
for sh in store.sheets(pid):
    f = next(x for x in facts[sh["discipline"]] if x["page"] == int(sh["page"]))
    lv = f["level"]
    read = {"sheet_kind": "floor_plan" if lv else "site_plan", "levels": [lv] if lv else [],
            "units": [u["label"] for u in units],
            "spaces": [{"name": s, "unit": u["label"], "level": lv} for u in units for s in spaces[lv]] if lv else [],
            "provenance": "document"}
    store.sheet_read(sh["id"], read, sheet_number=f["sheet_number"], title=f["title"])
    if lv:
        store.set_sheet_views(sh["id"], [{"title": f["title"], "level": lv, "unit": "", "unit_text": "", **f["view"],
                                          "cells": "", "source": "document"}])
print("seeded", SLUG, "in", DATA, "·", len(store.sheets(pid)), "sheets")
