#!/usr/bin/env python3
"""Film the "new project" beat: the Alder Court zip is uploaded on the home page and Closeout files and reads it.

    DEMO_PORT=8778 DEMO_DATA=demo-video/data-new demo-video/serve.sh && python3 demo-video/capture_new.py      # free rehearsal
    demo-video/film-new.sh                                                                                   # paid, real reading

Free: the server has fake cloud credentials, so every reading fails at no cost and the stills show the failure;
it checks the steps and the framing. Paid: the one upload is let through and Closeout reads the five current sheets,
writes the project summary and files the folder. Anything else that would ask Closeout to read or write is blocked.
Stills land in demo-video/output/cap-new/ (free: cap-new-dry/) with shots.json (the status line under each still).
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8778"
HERE = Path(__file__).resolve().parent
AI = "--paid" in sys.argv
OUT = HERE / "output" / ("cap-new" if AI else "cap-new-dry")
ZIP = HERE / "project2" / "Alder Court.zip"
SLUG = "alder-court"
PAID = ("/findings/suggest", "/findings/locate", "/ask", "/views", "/message", "/batches", "/drawings", "/docs")
blocked, meta, uploads = [], {}, []


def guard(route, req):
    path = urlparse(req.url).path
    if req.method == "POST" and path == "/api/projects":
        if uploads:
            blocked.append(req.url); return route.abort()
        uploads.append(req.url); return route.continue_()
    if req.method != "GET" and any(p in path for p in PAID):
        blocked.append(req.url); return route.abort()
    route.continue_()


def shot(pg, name, full=False, **info):
    pg.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
    meta[name] = {"dsf": pg.evaluate("devicePixelRatio"), **info}


def box(pg, sel):
    loc = pg.locator(sel).first
    b = loc.bounding_box() if loc.count() else None
    return None if not b else [b["x"] + b["width"] / 2, b["y"] + b["height"] / 2]


def nav(pg, hash_, wait):
    pg.goto(BASE + hash_); pg.reload(); pg.wait_for_timeout(wait)


def main():
    if not ZIP.exists():
        raise SystemExit("run demo-video/make_project2.py first")
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.png"):
        f.unlink()
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        dk = br.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        dp = dk.new_page()
        dp.route("**/api/**", guard)

        nav(dp, "/#/", 1500)
        dp.locator("#newp").scroll_into_view_if_needed(); dp.wait_for_timeout(300)
        shot(dp, "n01-home", tap=box(dp, "label.np-zip"), card=box(dp, "#newp"))
        dp.set_input_files("label.np-zip input[type=file]", str(ZIP))

        # One still each time the status line changes, until the page opens the new project.
        seen, k, t0, ready = "", 0, time.time(), False
        while time.time() - t0 < (900 if AI else 180):
            dp.wait_for_timeout(250)
            if f"/p/{SLUG}" in dp.url:
                ready = True; break
            el = dp.locator("#status")
            txt = el.inner_text().strip().replace("\n", " ") if el.count() and el.is_visible() else ""
            toast = dp.locator(".toast").inner_text().strip() if dp.locator(".toast").count() and dp.locator(".toast").is_visible() else ""
            now = txt or toast
            if now and now != seen:
                k += 1; seen = now
                dp.evaluate("window.scrollTo(0,0)")
                shot(dp, f"n02-progress{k:02d}", status=txt, toast=toast, t=round(time.time() - t0, 1))
                print(f"  {time.time() - t0:6.1f}s  {now}")
            if not AI and txt.startswith(("Finished, but", "Something went wrong", "Lost the connection")):
                break
        print("opened the project" if ready else "did not open the project")
        dp.wait_for_timeout(2500)
        shot(dp, "n03-project")
        for tab, name in (("", "n04-overview"), ("/drawings", "n05-drawings"), ("/docs", "n06-docs")):
            nav(dp, f"/#/p/{SLUG}{tab}", 2200); shot(dp, name)
            dp.evaluate("window.scrollTo(0, 700)"); dp.wait_for_timeout(400); shot(dp, name + "-lower")
        nav(dp, "/#/", 2000); shot(dp, "n07-home-after", cedar=box(dp, "text=Cedar Row Townhomes"))
        view = dp.evaluate(f"fetch('/api/projects/{SLUG}').then(r=>r.json())")
        br.close()
    prj = view.get("project") or view
    meta["project"] = {k: prj.get(k) for k in ("name", "address", "city", "building_type", "units", "levels")} if isinstance(prj, dict) else {}
    (OUT / "shots.json").write_text(json.dumps(meta, indent=1, default=str))
    print(len(meta) - 1, "stills in", OUT)
    print("blocked:", blocked or "none", "| uploads:", len(uploads))
    if AI:
        import subprocess, os
        print("what it cost:")
        subprocess.run([str(HERE.parent / ".venv/bin/python"), str(HERE / "cost.py")], cwd=HERE.parent,
                       env={**os.environ, "CLOSEOUT_DATA_DIR": str(HERE / "data-new")})
    return 0 if ready and not blocked else 1


if __name__ == "__main__":
    sys.exit(main())
