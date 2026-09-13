#!/usr/bin/env python3
"""Film stills for the Closeout demo from the fictional Cedar Row project. Free by default.

    demo-video/serve.sh && python3 demo-video/capture.py                 # free: no model call can run
    demo-video/serve.sh paid && python3 demo-video/capture.py --paid     # the AI beats, real output

Every step is a real press in the app. Free: routes that ask Closeout to read or write are blocked in
the browser, and the free server has fake cloud credentials, so the run costs nothing.
Paid: exactly one write-up from a photo, one drafted message (on Finish), one contractor drop filed and
one chat question are let through; anything past that is blocked and the run fails. What each call cost
is printed from the usage the app records.
Stills land in demo-video/output/cap/ (paid: cap-ai/) with shots.json (tap points for the edit).
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8777"
HERE = Path(__file__).resolve().parent
DRY = "--dry" in sys.argv   # with --paid: walk the paid script on the free server to check it, nothing let through
AI = "--paid" in sys.argv
OUT = HERE / "output" / ("cap-dry" if DRY else "cap-ai" if AI else "cap")
PHOTOS = Path.home() / "Documents/New project/PunchPilot/demo-assets/photos"
PAID = ("/findings/suggest", "/findings/locate", "/ask", "/views", "/message", "/batches")
ALLOW = {"/findings/suggest": 1, "/ask": 1, "/api/c/": 1} if AI else {}   # /api/c/…/batches is the contractor drop
QUESTION = "What does the contractor still need to send?"
ITEMS = [
    dict(photo="stud-nailing.jpg", unit="Unit 2", level="Upper Floor", x=0.352, y=0.62, space="Bedroom 3",
         desc="Stud split at the top plate where it was nailed. Replace the stud or add a full-length sister.",
         ev="photo: stud replaced or sistered"),
    dict(photo="subfloor-gap.jpg", unit="Unit 1", level="Main Floor", x=0.16, y=0.27, space="Living / Dining",
         desc="Open gap between subfloor panels over the joist. Panels are not fastened tight to the framing.",
         ev="photo: panels refastened, gap closed"),
    dict(photo="beam-bearing.jpg", unit="Unit 3", level="Main Floor", x=0.535, y=0.74, space="Garage",
         desc="Garage door header sits short on its post with less bearing than the framing plan shows. Add full bearing.",
         ev="photo: full bearing on the post"),
]
blocked, meta, spent = [], {}, {}


def guard(route, req):
    if req.method != "GET" and any(p in req.url for p in PAID):
        key = next((k for k in ALLOW if k in req.url), None)
        if key and DRY:
            spent[key] = spent.get(key, 0) + 1
            return route.fulfill(status=502, content_type="application/json", body='{"detail": "dry run"}')
        if key and spent.get(key, 0) < ALLOW[key]:
            spent[key] = spent.get(key, 0) + 1
            return route.continue_()
        blocked.append(req.url)
        return route.abort()
    route.continue_()


def fail(msg):
    if not DRY:
        raise SystemExit(msg)
    print("dry, would stop:", msg)


def shot(pg, name, full=False, **info):
    pg.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
    meta[name] = {"dsf": pg.evaluate("devicePixelRatio"), **info}


def nav(pg, hash_, wait):
    """A hash change alone keeps the old page state; load the page fresh so every still is current."""
    pg.goto(BASE + hash_); pg.reload(); pg.wait_for_timeout(wait)


def box(pg, sel):
    loc = pg.locator(sel).first if isinstance(sel, str) else sel
    b = loc.bounding_box() if loc.count() else None
    return None if not b else [b["x"] + b["width"] / 2, b["y"] + b["height"] / 2]


def main():
    for f in OUT.glob("*.png"):
        f.unlink()
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ph = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
        pg = ph.new_page()
        pg.route("**/api/**", guard)
        dk = br.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        dp = dk.new_page()
        dp.route("**/api/**", guard)

        nav(dp, "/#/", 1500); shot(dp, "d01-projects", tap=box(dp, dp.get_by_text("Cedar Row Townhomes").first))
        nav(dp, "/#/p/cedar-row/docs", 1800); shot(dp, "d02-docs")
        nav(dp, "/#/p/cedar-row", 1800); shot(dp, "d03-overview-empty")

        pg.goto(BASE + "/#/p/cedar-row/field"); pg.wait_for_timeout(2000); shot(pg, "p01-field", tap=box(pg, "#startRv"))
        pg.click("#startRv"); pg.wait_for_timeout(1500); shot(pg, "p02-walk", tap=box(pg, "label:has(#shotIn)"))
        for i, it in enumerate(ITEMS):
            n = i + 1
            if pg.locator("#walkOpen").count() and pg.locator("#walkOpen").is_visible():
                pg.click("#walkOpen"); pg.wait_for_timeout(600)
            pg.set_input_files("#shotIn", str(PHOTOS / it["photo"])); pg.wait_for_timeout(900)
            if n == 1: shot(pg, "p03-photo")
            pg.select_option("#wBld", index=1); pg.wait_for_timeout(300)
            pg.select_option("#wUnit", it["unit"]); pg.select_option("#wLevel", it["level"]); pg.wait_for_timeout(400)
            if n == 1: pg.locator("#wOpen").scroll_into_view_if_needed(); pg.wait_for_timeout(250); shot(pg, "p04-where", tap=box(pg, "#wOpen"))
            pg.click("#wOpen"); pg.wait_for_timeout(1600)
            if n == 1: shot(pg, "p05-plan")
            pg.evaluate(f"(() => {{ const sh = state.p.project.sheets.find(s => s.id === state.viewer.sheetId); tapSheet(sh, {it['x']}, {it['y']}); }})()")
            pg.wait_for_timeout(700)
            if n == 1: shot(pg, "p06-tapped", tap=box(pg, ".pin.draft"), manual=box(pg, "#manual"), ask=box(pg, "#ask"))
            if AI and n == 1:
                with pg.expect_response(lambda r: "/findings/suggest" in r.url, timeout=180000) as got:
                    pg.click("#ask"); pg.wait_for_timeout(700); shot(pg, "p07-thinking")
                if not got.value.ok:
                    fail(f"write-up failed: {got.value.status} {got.value.text()[:300]}")
                body = got.value.json() if got.value.ok else {}
                sug = body.get("suggestion", {})
                pg.wait_for_timeout(1200)
                pg.evaluate("window.scrollTo(0,0)"); pg.locator("#panel").scroll_into_view_if_needed(); pg.wait_for_timeout(300)
                shot(pg, "p07-suggested")
                fixed = {}
                for sel, want in (("#fUnit", it["unit"]), ("#fLevel", it["level"])):   # the walk already chose these
                    if not pg.input_value(sel):
                        pg.select_option(sel, want); fixed[sel] = want
                meta["p07-suggested"]["filled_by_capture"] = fixed
                meta["p07-suggested"].update(usage=sug.get("usage"), model_id=body.get("model_id"))
                meta["p07-suggested"]["suggestion"] = {k: sug.get(k) for k in ("location", "description", "evidence_required", "unit", "level", "space")}
                pg.locator("#fDesc").scroll_into_view_if_needed(); pg.wait_for_timeout(250); shot(pg, "p07-suggested-desc")
                pg.locator("#save").scroll_into_view_if_needed(); pg.wait_for_timeout(250)
                shot(pg, "p08-form", tap=box(pg, "#save"))
                pg.click("#save"); pg.wait_for_timeout(1800)
                if not pg.evaluate("fetch('/api/projects/cedar-row').then(r=>r.json()).then(j=>j.register.length)"):
                    fail("the written-up item did not save")
                shot(pg, f"p09-saved{n}")
                pg.evaluate("location.hash='#/p/cedar-row/field'"); pg.wait_for_timeout(1200)
                continue
            pg.click("#manual"); pg.wait_for_timeout(600)
            pg.fill("#fLoc", f'{it["unit"]}, {it["level"]}, {it["space"]}'); pg.fill("#fSpace", it["space"])
            if n == (2 if AI else 1):
                pg.locator("#fDesc").scroll_into_view_if_needed()
                words = it["desc"].split(" ")
                for k, cut in enumerate((0.25, 0.5, 0.75, 1.0)):
                    pg.fill("#fDesc", " ".join(words[: max(1, round(len(words) * cut))])); pg.wait_for_timeout(150)
                    shot(pg, f"p07-type{k + 1}")
            pg.fill("#fDesc", it["desc"]); pg.fill("#fEv", it["ev"])
            if n == (2 if AI else 1):
                pg.locator("#save").scroll_into_view_if_needed(); pg.wait_for_timeout(200)
                shot(pg, "p08-typed" if AI else "p08-form", tap=box(pg, "#save"))
            pg.click("#save"); pg.wait_for_timeout(1800)
            shot(pg, f"p09-saved{n}")
            pg.evaluate("location.hash='#/p/cedar-row/field'"); pg.wait_for_timeout(1200)

        pg.evaluate("window.scrollTo(0,0)"); pg.wait_for_timeout(300)
        if pg.locator("#walkClose").count() and pg.locator("#walkClose").is_visible():
            pg.click("#walkClose"); pg.wait_for_timeout(800)
        pg.click("#walkOpen"); pg.wait_for_timeout(900)
        pg.locator("#finishRv").scroll_into_view_if_needed(); pg.wait_for_timeout(200)
        shot(pg, "p10-list", tap=box(pg, "#finishRv"))
        pg.click("#finishRv"); pg.wait_for_timeout(900)
        pg.locator("#finishGo").scroll_into_view_if_needed(); pg.wait_for_timeout(200)
        shot(pg, "p11-finish-ask", tap=box(pg, "#finishGo"))
        if AI:
            with pg.expect_response(lambda r: r.url.endswith("/finish") and r.request.method == "POST", timeout=240000) as got:
                pg.click("#finishGo"); pg.wait_for_timeout(700); shot(pg, "p11-drafting")
            fin = got.value.json() if got.value.ok else {}
            if fin.get("error") or not fin.get("message"):
                fail(f"message draft failed: {fin.get('error')}")
            pg.wait_for_timeout(2500)
        else:
            pg.click("#finishGo"); pg.wait_for_timeout(5000)
        pg.evaluate("window.scrollTo(0,0)"); pg.wait_for_timeout(300); shot(pg, "p12-finished")

        j = dp.evaluate("fetch('/api/projects/cedar-row').then(r=>r.json()).then(j=>j.reviews.map(r=>r.id))")
        rid = j[0]
        nav(dp, "/#/p/cedar-row", 1800); shot(dp, "d04-overview")
        nav(dp, "/#/p/cedar-row/deficiencies", 1800); shot(dp, "d05-deficiencies")
        dp.goto(f"{BASE}/api/projects/cedar-row/reviews/{rid}/report"); dp.wait_for_timeout(2200); shot(dp, "d06-report", full=True)
        share = dp.request.post(f"{BASE}/api/projects/cedar-row/reviews/{rid}/share").json()
        tok = share["share"]["id"]
        nav(dp, "/#/p/cedar-row/messages", 1800); shot(dp, "d07-messages")
        if AI and dp.locator(".mopen .link").count():
            dp.locator(".mopen .link").first.scroll_into_view_if_needed(); dp.wait_for_timeout(300); shot(dp, "d07-messages-link")

        pg.goto(f"{BASE}/c/{tok}"); pg.wait_for_timeout(2200); shot(pg, "p13-contractor", tap=box(pg, "#dropEv"))
        pg.evaluate("window.scrollTo(0,420)"); pg.wait_for_timeout(300); shot(pg, "p14-contractor-list")

        if AI:
            # The contractor sends the repair photo through their link, named with the item number as the page asks.
            up = HERE / "output" / "upload"; up.mkdir(parents=True, exist_ok=True)
            (up / "AR-01.jpg").write_bytes((PHOTOS / "stud-nailing-fixed.jpg").read_bytes())
            pg.evaluate("window.scrollTo(0,0)"); pg.wait_for_timeout(300)
            with pg.expect_response(lambda r: "/batches" in r.url and r.request.method == "POST", timeout=60000) as got:
                pg.set_input_files("#dropEv input[type=file]", str(up / "AR-01.jpg"))
            if not got.value.ok:
                fail(f"contractor drop failed: {got.value.status}")
            pg.wait_for_timeout(1800); shot(pg, "p16-contractor-filing")
            done = None
            for _ in range(0 if DRY else 60):
                done = dp.evaluate("fetch('/api/projects/cedar-row').then(r=>r.json()).then(j=>j.batches.filter(b=>b.via && !b.via.startsWith('email:')).map(b=>(b.runs.at(-1)||{}).status))")
                if done and done[-1] in ("done", "failed"):
                    break
                pg.wait_for_timeout(5000)
            if not done or done[-1] != "done":
                fail(f"contractor drop not filed: {done}")
            pg.goto(f"{BASE}/c/{tok}"); pg.reload(); pg.wait_for_timeout(2200)
            pg.evaluate("window.scrollTo(0,420)"); pg.wait_for_timeout(300); shot(pg, "p17-contractor-filed")
            nav(dp, "/#/p/cedar-row/item/AR-01", 2000); shot(dp, "d08-item")
            if dp.locator(".card").count(): dp.locator(".card").first.scroll_into_view_if_needed(); dp.wait_for_timeout(300); shot(dp, "d08-item-filed")
        else:
            nav(dp, "/#/p/cedar-row/item/AR-01", 1800)
            shot(dp, "d08-item", tap=box(dp, "label:has(#sentIn)"))
            dp.set_input_files("#sentIn", str(PHOTOS / "stud-nailing-fixed.jpg")); dp.wait_for_timeout(3500)
        nav(dp, "/#/p/cedar-row/item/AR-01", 1800)
        dp.locator("button[data-d=accept]").scroll_into_view_if_needed(); dp.wait_for_timeout(300)
        shot(dp, "d09-item-evidence", tap=box(dp, "button[data-d=accept]"))
        dp.click("button[data-d=accept]"); dp.wait_for_timeout(1500); shot(dp, "d10-item-closed")
        nav(dp, "/#/p/cedar-row", 1800); shot(dp, "d11-overview-closed")
        pg.goto(f"{BASE}/c/{tok}"); pg.wait_for_timeout(2200); pg.evaluate("window.scrollTo(0,420)"); pg.wait_for_timeout(300)
        shot(pg, "p15-contractor-closed")
        if AI:
            nav(dp, "/#/p/cedar-row", 1800)
            dp.click("#askfab"); dp.wait_for_timeout(800)
            dp.fill("#askq", QUESTION); dp.wait_for_timeout(300)
            shot(dp, "d12-ask", tap=box(dp, "#askgo"))
            with dp.expect_response(lambda r: r.url.endswith("/ask") and r.request.method == "POST", timeout=180000) as got:
                dp.click("#askgo"); dp.wait_for_timeout(900); shot(dp, "d12-ask-thinking")
            if not got.value.ok:
                fail(f"question failed: {got.value.status}")
            dp.wait_for_timeout(2500); shot(dp, "d13-answer")
        br.close()
    (OUT / "shots.json").write_text(json.dumps(meta, indent=1))
    print(len(meta), "stills in", OUT)
    print("blocked:", blocked or "none")
    if AI:
        print("let through:", spent)
        if not DRY:
            import subprocess
            print("what it cost:")
            subprocess.run([str(HERE.parent / ".venv/bin/python"), str(HERE / "cost.py")], cwd=HERE.parent)
    return 1 if blocked and not DRY else 0


if __name__ == "__main__":
    sys.exit(main())
