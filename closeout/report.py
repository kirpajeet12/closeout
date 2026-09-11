"""The field review report: one printable document per review, built only from what was recorded on the walk.

Deterministic. No model call, nothing sent. The engineer prints it (or saves it as PDF) from the browser after
checking every item; the sign-off block at the end is theirs to fill.
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import revisions
from .review import DISCIPLINES
from .store import Store

CALLS = {"accept": "Ready to close", "hold": "On hold", "reject": "Not accepted"}
OPEN = "Open"
TZ = ZoneInfo(os.environ.get("CLOSEOUT_TZ", "America/Vancouver"))


def review_report(store: Store, project_id: str, review_id: str, office: str = "the engineer's office") -> dict:
    """Everything the printed report shows, as plain data."""
    rv = store.review(review_id)
    if not rv or rv["project_id"] != project_id:
        raise ValueError("no such review")
    prj = store.project(project_id) or {}
    slug = prj.get("slug", "")
    sheets = {s["id"]: s for s in store.sheets(project_id)}
    latest: dict[str, dict] = {}
    for d in store.decisions(project_id):            # ordered by created_at, so the last one per item wins
        latest[d["item_id"]] = d
    items = []
    for d in store.review_items(project_id, review_id):
        sh = sheets.get(d.get("sheet_id") or "") or {}
        call = latest.get(d["item_id"])
        meta = d.get("ref_meta") or {}
        items.append({
            "item_id": d["item_id"], "location": d["location"], "description": d["description"],
            "evidence_required": d["evidence_required"], "note": d.get("note", ""),
            "unit": d.get("unit", ""), "level": d.get("level", ""), "space": d.get("space", ""),
            "sheet": d.get("sheet", ""), "sheet_title": sh.get("title", ""), "sheet_id": d.get("sheet_id", ""),
            "pin": [d["pin_x"], d["pin_y"]] if d.get("pin_x") is not None else None,
            "photo_url": f"/api/projects/{slug}/register/{d['item_id']}/reference" if d.get("reference_photo") else "",
            "plan_url": f"/api/projects/{slug}/items/{d['item_id']}/pin.jpg" if d.get("pin_x") is not None and sh else "",
            "taken_at": meta.get("taken_at", ""),
            "status": CALLS.get((call or {}).get("decision", ""), OPEN),
            "status_note": (call or {}).get("note") or "", "status_at": (call or {}).get("created_at", ""),
        })
    all_docs = store.documents(project_id)
    members = revisions.set_members(all_docs, rv["discipline"])
    docs = [x for x in all_docs if x.get("kind") == "drawing" and x.get("discipline") == rv["discipline"]]
    seen: dict[tuple, dict] = {}                       # the same issue filed in two folders is one drawing
    for x in docs:
        key = (x["rel_path"].rsplit("/", 1)[-1].lower(), x.get("dated") or "")
        row = seen.setdefault(key, {"file": x["rel_path"].rsplit("/", 1)[-1], "dated": x.get("dated") or "", "current": False,
                                    "pages": x.get("pages") or 0, "in_set": False})
        row["current"] = row["current"] or bool(x.get("is_current"))
        row["in_set"] = row["in_set"] or x["id"] in members
    drawings = list(seen.values())
    drawings = ([d for d in drawings if d["current"]] + sorted([d for d in drawings if not d["current"] and d["in_set"]], key=lambda d: d["dated"], reverse=True)
                + sorted([d for d in drawings if not d["current"] and not d["in_set"]], key=lambda d: d["dated"], reverse=True))
    current = next((x for x in drawings if x["current"]), None)
    counts: dict[str, int] = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    walked = rv["started_at"]
    stamp = (rv.get("finished_at") or walked or "")[:10].replace("-", "")
    return {
        "project": {"name": prj.get("name", ""), "slug": slug, "type": (prj.get("model") or {}).get("building_type", "")},
        "review": {"id": review_id, "title": rv["title"], "discipline": rv["discipline"], "sequence": rv["sequence"],
                   "discipline_name": DISCIPLINES.get(rv["discipline"], rv["discipline"]), "status": rv["status"],
                   "started_at": walked, "finished_at": rv.get("finished_at")},
        "office": office, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "name": f"FieldReview_{_safe(prj.get('name') or slug)}_{stamp}_{rv['discipline']}{rv['sequence']}",
        "drawings": drawings, "current_set": current,
        "items": items, "count": len(items), "photos": sum(1 for i in items if i["photo_url"]),
        "units": sorted({i["unit"] for i in items if i["unit"]}), "sheets_used": sorted({i["sheet"] for i in items if i["sheet"]}),
        "status_counts": counts,
    }


def report_html(data: dict) -> str:
    e = html.escape
    rv, pr = data["review"], data["project"]
    live = rv["status"] != "finished"
    meta = [("Discipline", rv["discipline_name"]), ("Review", rv["title"]), ("Walked", _day(rv["started_at"])),
            ("Finished", _day(rv["finished_at"]) if rv["finished_at"] else "In progress"),
            ("Drawings reviewed", f"{data['current_set']['file']} · issued {data['current_set']['dated'] or 'undated'}" if data["current_set"] else "No current set on file"),
            ("Report date", _day(data["generated_at"]))]
    cards = []
    for i in data["items"]:
        where = " · ".join(x for x in (i["unit"], i["level"], i["space"]) if x) or i["location"]
        pics = ""
        if i["photo_url"] or i["plan_url"]:
            pics = '<div class="pics">'
            if i["photo_url"]:
                pics += f'<figure><img src="{e(i["photo_url"])}" alt=""><figcaption>Photo on site{(" · " + _when(i["taken_at"])) if i["taken_at"] else ""}</figcaption></figure>'
            if i["plan_url"]:
                pics += f'<figure><img src="{e(i["plan_url"])}" alt=""><figcaption>Sheet {e(i["sheet"])}{(" · " + e(i["sheet_title"].title())) if i["sheet_title"] else ""}</figcaption></figure>'
            pics += "</div>"
        st_cls = {"Ready to close": "ok", "On hold": "hold", "Not accepted": "no"}.get(i["status"], "open")
        cards.append(f'''<article class="item">
  <header><span class="num">{e(i["item_id"])}</span><div><h3>{e(i["description"])}</h3><p class="where">{e(where)}{(" · sheet " + e(i["sheet"])) if i["sheet"] else ""}</p></div><span class="st {st_cls}">{e(i["status"])}</span></header>
  {pics}
  <dl><dt>Where</dt><dd>{e(i["location"])}</dd><dt>To close</dt><dd>{e(i["evidence_required"])}</dd>{("<dt>Note</dt><dd>" + e(i["note"]) + "</dd>") if i["note"] else ""}{("<dt>Decision</dt><dd>" + e(i["status"]) + (" · " + e(i["status_note"]) if i["status_note"] else "") + (" · " + _day(i["status_at"]) if i["status_at"] else "") + "</dd>") if i["status"] != OPEN else ""}</dl>
</article>''')
    n = data["count"]
    summary = [f'{n} deficienc{"y" if n == 1 else "ies"}', f'{data["photos"]} photo{"" if data["photos"] == 1 else "s"}']
    if data["units"]:
        summary.append("Units " + ", ".join(u.replace("Unit ", "") for u in data["units"]))
    if data["sheets_used"]:
        summary.append("Sheets " + ", ".join(data["sheets_used"]))
    for k, v in data["status_counts"].items():
        if k != OPEN:
            summary.append(f"{v} {k.lower()}")
    drawings = "".join(f'<tr><td>{e(d["file"])}</td><td>{e(d["dated"] or "undated")}</td><td>{d["pages"] or ""}</td><td>{"Current issue" if d["current"] else "Superseded" if d["in_set"] else "On file, not an issue of the set"}</td></tr>' for d in data["drawings"])
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(data["name"])}</title>
<style>{CSS}</style></head><body class="{"draft" if live else ""}">
<div class="bar"><a href="/#/p/{e(pr["slug"])}/field">← Back to the field review</a><span>{"In progress · this is the report so far" if live else "Check every item, then print or save as PDF"}</span><button onclick="window.print()">Print / Save as PDF</button></div>
<main>
  <header class="title">
    <p class="office">{e(data["office"])}</p>
    <h1>Field review report{(' <span class="tag">draft</span>' if live else "")}</h1>
    <p class="project">{e(pr["name"])}</p>
    <dl class="meta">{"".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in meta)}</dl>
    <p class="sum">{" · ".join(e(s) for s in summary)}</p>
  </header>
  <section class="items">{"".join(cards) if cards else '<p class="none">Nothing was recorded during this walk.</p>'}</section>
  <section class="docs"><h2>Drawings on file for {e(rv["discipline_name"].lower())}</h2>
    {("<table><thead><tr><th>File</th><th>Issued</th><th>Pages</th><th></th></tr></thead><tbody>" + drawings + "</tbody></table>") if drawings else "<p class='none'>No drawings on file for this discipline.</p>"}
  </section>
  <section class="sign">
    <div><span>Reviewed by</span><i></i><small>Name, P.Eng.</small></div>
    <div><span>Signature</span><i></i><small></small></div>
    <div><span>Date</span><i></i><small></small></div>
    <div class="seal"><span>Seal</span></div>
  </section>
  <p class="disclaimer">Prepared by {e(data["office"])} in Closeout from the notes and photos recorded during the walk. Nothing in this report is issued until the reviewing engineer has checked every item and signed above.</p>
  <footer><span>{e(data["office"])}</span><span>{e(pr["name"])} · {e(rv["title"])} · {e(rv["discipline_name"])}</span><span>{e(data["name"])}</span></footer>
</main></body></html>'''


CSS = """
:root{--ink:#14161a;--ink2:#4a4f58;--mute:#8a8f98;--line:#e3e5ea;--paper:#fff;--wash:#f5f6f8;--ok:#1d7a4a;--hold:#9a6b00;--no:#b3261e}
*{box-sizing:border-box}body{margin:0;background:#ecedf0;color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",Inter,Arial,sans-serif;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.bar{position:sticky;top:0;display:flex;align-items:center;gap:16px;padding:12px 24px;background:var(--ink);color:#fff;font-size:14px;z-index:2}
.bar a{color:#fff;text-decoration:none;opacity:.85}.bar span{flex:1;opacity:.7}.bar button{background:#fff;color:var(--ink);border:0;border-radius:999px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer}
main{max-width:860px;margin:28px auto 60px;background:var(--paper);padding:56px 64px;box-shadow:0 20px 60px rgba(0,0,0,.08)}
.title{border-bottom:2px solid var(--ink);padding-bottom:24px;margin-bottom:28px}
.office{margin:0 0 18px;letter-spacing:.14em;text-transform:uppercase;font-size:12px;color:var(--ink2)}
h1{margin:0;font-size:34px;letter-spacing:-.02em;line-height:1.05}h1 .tag{display:inline-block;vertical-align:middle;margin-left:12px;padding:3px 10px;border:1.5px solid var(--hold);color:var(--hold);border-radius:999px;font-size:12px;letter-spacing:.1em;text-transform:uppercase;font-weight:600}
.project{margin:8px 0 22px;font-size:20px;color:var(--ink2)}
.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:14px 24px;margin:0}.meta div{min-width:0}.meta dt{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}.meta dd{margin:2px 0 0;font-weight:500;overflow-wrap:anywhere}
.sum{margin:22px 0 0;font-size:14px;color:var(--ink2)}
.items{display:flex;flex-direction:column;gap:22px}
.item{border:1px solid var(--line);border-radius:14px;padding:20px 22px;break-inside:avoid;page-break-inside:avoid}
.item header{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:start}
.num{display:inline-block;padding:5px 10px;border-radius:8px;background:var(--ink);color:#fff;font-weight:700;font-size:13px;letter-spacing:.04em;white-space:nowrap}
.item h3{margin:0;font-size:17px;line-height:1.3}.where{margin:4px 0 0;color:var(--ink2);font-size:14px}
.st{font-size:12px;font-weight:600;padding:5px 10px;border-radius:999px;background:var(--wash);color:var(--ink2);white-space:nowrap}.st.ok{background:#e6f4ec;color:var(--ok)}.st.hold{background:#fbf1d6;color:var(--hold)}.st.no{background:#fbe5e3;color:var(--no)}
.pics{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:16px 0 4px}.pics figure{margin:0}.pics img{width:100%;height:240px;object-fit:cover;border-radius:10px;background:var(--wash);border:1px solid var(--line)}
figcaption{font-size:12px;color:var(--mute);margin-top:6px}
.item dl{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:14px 0 0;font-size:14px}.item dt{color:var(--mute);white-space:nowrap}.item dd{margin:0}
.docs{margin-top:36px}.docs h2,.sign h2{font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--mute);margin:0 0 10px}
table{width:100%;border-collapse:collapse;font-size:14px}td:nth-child(2),td:nth-child(3),td:nth-child(4){white-space:nowrap}th{text-align:left;font-weight:600;color:var(--ink2);border-bottom:1px solid var(--line);padding:6px 8px 6px 0}td{padding:8px 8px 8px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.none{color:var(--mute)}
.sign{display:grid;grid-template-columns:1.4fr 1fr .8fr auto;gap:22px;margin-top:44px;padding-top:20px;border-top:1px solid var(--line);break-inside:avoid;page-break-inside:avoid}
.sign span{display:block;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}.sign i{display:block;height:38px;border-bottom:1px solid var(--ink);margin-top:10px}.sign small{display:block;color:var(--mute);font-size:12px;margin-top:4px}
.seal{width:120px;height:120px;border:1px dashed var(--mute);border-radius:50%;display:flex;align-items:center;justify-content:center}
.disclaimer{margin:36px 0 0;font-size:12px;color:var(--mute);line-height:1.5}
footer{display:flex;justify-content:space-between;gap:12px;margin-top:16px;padding-top:12px;border-top:1px solid var(--line);font-size:11px;color:var(--mute)}
@page{margin:16mm 14mm}
@media print{body{background:#fff}.bar{display:none}main{max-width:none;margin:0;padding:0;box-shadow:none}.pics img{height:200px}}
@media (max-width:720px){main{padding:28px 20px;margin:0}.meta{grid-template-columns:1fr 1fr}.pics{grid-template-columns:1fr}.sign{grid-template-columns:1fr 1fr}.bar{padding:10px 14px}.bar span{display:none}}
"""


def _safe(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s[:40] or "project"


def _dt(iso: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(TZ)


def _day(iso: str | None) -> str:
    d = _dt(iso or "")
    return d.strftime("%a %-d %b %Y") if d else ""


def _when(iso: str | None) -> str:
    d = _dt(iso or "")
    return d.strftime("%-d %b %Y, %-I:%M %p").replace("AM", "am").replace("PM", "pm") if d else ""
