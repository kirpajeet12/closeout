#!/usr/bin/env python3
"""Film the folder beat: the Documents tab of the uploaded Alder Court project, the folders Closeout set up, then the
engineer's own folders made inside them and a file filed in. Free: nothing here asks Closeout to read or write.

    rm -rf demo-video/data-folders && cp -R demo-video/data-new demo-video/data-folders
    CLOSEOUT_DATA_DIR=demo-video/data-folders .venv/bin/uvicorn closeout.api:app --port 8779   (fake AWS env, see SCRIPT.md)
    python3 demo-video/capture_folders.py

Works on the scratch copy only, so the upload stills in data-new stay as filmed. Stills are added to output/cap-new/.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8779"
HERE = Path(__file__).resolve().parent
OUT = HERE / "output" / "cap-new"
SLUG = "alder-court"
ALLOWED = ("/folders", "/filing")
OLD = "Alder Court - Architectural Set 2026-06-12.pdf"
blocked, meta = [], {}


def guard(route, req):
    path = urlparse(req.url).path
    if req.method != "GET" and not any(path.endswith(a) or (a + "/") in path for a in ALLOWED):
        blocked.append(req.url); return route.abort()
    route.continue_()


def main():
    meta.update(json.loads((OUT / "shots.json").read_text()))
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        pg.route("**/api/**", guard)
        pg.goto(BASE + f"/#/p/{SLUG}/docs"); pg.wait_for_timeout(2500)
        y0 = pg.evaluate("document.querySelector('.explorer').getBoundingClientRect().top + scrollY") - 150

        def top():
            pg.evaluate(f"scrollTo(0,{y0})"); pg.wait_for_timeout(300)

        def box(sel):
            b = pg.locator(sel).first.bounding_box()
            return [b["x"] + b["width"] / 2, b["y"] + b["height"] / 2] if b else None

        def shot(name, **info):
            pg.screenshot(path=str(OUT / f"{name}.png"))
            meta[name] = {"dsf": 2, **{k: v for k, v in info.items() if v}}

        def click(sel):
            pg.locator(sel).first.click(); pg.wait_for_timeout(700); top()

        # the drawings as the site is built: the site's own sheets, then the building's
        pg.goto(BASE + f"/#/p/{SLUG}/drawings"); pg.wait_for_timeout(2500)
        for name, sel in (("n05-site", ".tier.site"), ("n05-building", ".tier.bld")):
            pg.evaluate(f"scrollTo(0, document.querySelector('{sel}').getBoundingClientRect().top + scrollY - 40)")
            pg.wait_for_timeout(600); shot(name)
        pg.goto(BASE + f"/#/p/{SLUG}/docs"); pg.wait_for_timeout(2500)

        top(); shot("n06-folders1")
        for path in ("prj", "prj|site", "prj|site|site/AR"):
            click(f".treepane [data-path='{path}']")
        shot("n06-folders2", tap=box("[data-mkopen]"))
        # where every other file went
        for name, path in (("n06-site-el", "prj|site|site/EL"), ("n06-bld", "prj|b/418"), ("n06-bld-el", "prj|b/418|b/418/EL"),
                           ("n06-bld-other", "prj|b/418|b/418/OTHER"), ("n06-crp", "crp"), ("n06-crp-loa", "crp|crp/1")):
            click(f".treepane [data-path='{path}']"); shot(name)
        for path in ("prj", "prj|site", "prj|site|site/AR"):
            click(f".treepane [data-path='{path}']")

        # a folder of the engineer's own inside Architectural
        click("[data-mkopen]")
        pg.locator("form.mkfolder input").type("Older issues", delay=40); pg.wait_for_timeout(300)
        shot("n08-mkfolder", tap=box("form.mkfolder .primary"))
        pg.locator("form.mkfolder .primary").click(); pg.wait_for_timeout(1200); top()
        shot("n08-made", tap=box(".folderpane .xrow.folderrow:has-text('Older issues')"))

        # and one inside that
        click(".folderpane .xrow.folderrow:has-text('Older issues')")
        click("[data-mkopen]")
        pg.locator("form.mkfolder input").type("June 2026 issue", delay=40)
        pg.locator("form.mkfolder .primary").click(); pg.wait_for_timeout(1200); top()
        shot("n08-nested")

        # file the June set into the deepest folder
        click(".treepane [data-path='prj|site|site/AR']")
        pg.locator(f".folderpane .xrow.filerow:has-text('{OLD}') button[data-move]").click(); pg.wait_for_timeout(700)
        sel = pg.locator("form.mvpanel select[name=folder]")
        value = sel.locator("option", has_text="June 2026 issue").get_attribute("value")
        sel.select_option(value); pg.wait_for_timeout(300)
        pg.locator("form.mvpanel").scroll_into_view_if_needed(); pg.wait_for_timeout(300)
        shot("n08-move", tap=box("form.mvpanel .primary"))
        pg.locator("form.mvpanel .primary").click(); pg.wait_for_timeout(1200)
        ids = {f["name"]: f["id"] for f in pg.evaluate(f"fetch('/api/projects/{SLUG}').then(r=>r.json())")["folders"]}
        click(".folderpane .xrow.folderrow:has-text('Older issues')")
        click(f".folderpane [data-path='prj|site|site/AR|u/{ids['Older issues']}|u/{ids['June 2026 issue']}']")
        shot("n08-filed")
        br.close()
    (OUT / "shots.json").write_text(json.dumps(meta, indent=1, default=str))
    print("blocked:", blocked or "nothing")


if __name__ == "__main__":
    main()
