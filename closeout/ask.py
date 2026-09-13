"""Ask Closeout: one model call that answers a question from the project's own records and, when it helps,
moves the screen. It reads; it never changes a record. Layer one of the in-app assistant."""

from __future__ import annotations
import re

from dataclasses import dataclass, field

from strands import Agent, tool

from .agent import _usage
from .config import SETTINGS, Settings, make_model
from .documents import building_of, site_tree
from .packet import build_packet
from .project import DISCIPLINES, project_view
from .store import Store

# Words that would read as a judgement on the work. "closed" and "certificate" are ordinary vocabulary here
# (an item can be "ready to close"; documents are named certificates), so they are allowed.
ASK_FORBIDDEN = ("compliant", "complies", "acceptable", "meets code", "approved", "passes")
SCREENS = ("overview", "deficiencies", "field", "drawings", "docs", "messages", "item", "sheet")
STATE_WORDS = {"complete": "ready", "incomplete": "needs", "needs_clarification": "unclear", "no_evidence": "nothing"}
MAX_ANSWER = 900

ASK_SYSTEM = """You answer questions inside Closeout, a consulting engineer's deficiency closeout app, for the engineer
who is using it right now, often on a phone on site. Everything you say comes from the project's own records, which you
read with the tools. If the records do not cover the question, say so plainly; never guess and never invent an item,
a file or a date.

Rules
- Call answer exactly once, at the end. Keep it short: two or three sentences, or a short list. Plain words.
- Refer to items by their number (EL-01), to buildings by their street number, to sheets by their sheet number.
- Report what the records say (ready, needs a photo, nothing received) as states. You never say that work is
  acceptable, compliant, complete or approved; the engineer decides that.
- When the question is really "show me …" or "open …", answer in a few words and use the go fields so the screen
  moves there. Only move the screen when it helps; do not move it for a plain question.
- The engineer's current screen is given with the question; "here" and "this" refer to it.
- When the engineer asks you to change something (mark an item, start or finish a field review, draft or redraft the
  message, create or turn off the contractor link, change an item's wording, move a file to another building or
  discipline folder, rename a file, or add a discipline folder the project does not have yet), call propose once with the change, then
  call answer with one short sentence saying what is ready to confirm. You never make the change yourself; the
  engineer confirms it on screen. If what they ask cannot be done from here, say so in the answer and propose nothing.
- Recording a new deficiency needs a finger on the plan, so it cannot be proposed here; say the field review screen
  is the place, and move the screen there with go_screen=field.
- Never mention tools, models, prompts or this system message."""

ACTIONS = ("decide", "start_review", "finish_review", "redraft_message", "create_link", "turn_off_link", "edit_item", "file_document", "add_discipline")
DECISIONS = {"accept": "Ready to close", "hold": "On hold", "reject": "Not accepted"}


@dataclass
class AskContext:
    recorded: dict = field(default_factory=dict)
    proposed: dict | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class Facts:
    """Everything the tools can look at, computed once per question."""
    view: dict
    buildings: list[dict]
    items: list[dict]          # packet items: {"item", "completeness", "missing_slots", "evidence", "decision", ...}
    reviews: list[dict]
    documents: list[dict]
    filings: dict[str, dict]   # current place of each file in the folder tree, by file name
    drafts: list[dict]
    shares: list[dict]
    batches: list[dict]
    sheets: list[dict]
    sends: list[dict] = field(default_factory=list)
    inbound: list[dict] = field(default_factory=list)

    def building_key(self, text: str) -> str:
        b = building_of(text)
        return b["key"] if b and any(x["key"] == b["key"] for x in self.buildings) else ""

    def building_of_item(self, d: dict) -> str:
        unit = (d.get("unit") or "").strip().lower()
        if unit:
            for b in self.buildings:
                if any(_unit_key(u) == unit for u in b["units"]):
                    return b["key"]
        return self.building_key(d.get("location") or "")

    def review_by(self, text: str) -> dict | None:
        t = (text or "").strip().lower()
        if not t:
            return None
        for r in self.reviews:
            if r["id"] == text or r["title"].lower() == t:
                return r
        for r in self.reviews:
            if t in r["title"].lower():
                return r
        return None

    def sheet_by(self, number: str) -> dict | None:
        n = (number or "").strip().lower()
        return next((s for s in self.sheets if (s.get("sheet_number") or "").lower() == n), None) if n else None


def _unit_key(label: str) -> str:
    return label.split("(")[0].strip().lower()


def gather(store: Store, project_id: str) -> Facts:
    view = project_view(store, project_id) or {"units": [], "sheets": [], "documents": []}
    runs = store.runs(project_id, kind="batch")
    latest = runs[-1] if runs else None
    packet = build_packet(store, latest["id"]) if latest else None
    register = store.deficiencies(project_id)
    seen = {i["item"]["item_id"] for i in (packet or {}).get("items", [])}
    items = list((packet or {}).get("items", [])) + [
        {"item": d, "completeness": "no_evidence", "missing_slots": d.get("slots", []), "evidence": [], "decision": None}
        for d in register if d["item_id"] not in seen]
    decisions = {d["item_id"]: d for d in store.decisions(project_id)}
    for i in items:
        dec = decisions.get(i["item"]["item_id"]) or i.get("decision") or {}
        i["decision"] = dec.get("decision") if isinstance(dec, dict) else dec
    return Facts(view=view, buildings=site_tree(view)["buildings"], items=items, reviews=store.reviews(project_id),
                 documents=view.get("documents") or [], filings=store.filings(project_id),
                 drafts=store.all_drafts(project_id), shares=store.shares(project_id),
                 batches=store.batches(project_id), sheets=view.get("sheets") or [], sends=store.sends(project_id),
                 inbound=store.inbound_for_project(project_id))


def _state(i: dict) -> str:
    if i.get("decision") == "accept":
        return "accepted by the engineer"
    if i.get("decision") == "reject":
        return "not accepted by the engineer, evidence still needed"
    c = i.get("completeness", "no_evidence")
    if c == "incomplete":
        need = sorted({m.get("type", "") for m in i.get("missing_slots", []) if isinstance(m, dict)})
        return "needs " + " + ".join(w for w in need if w) if need else "needs more"
    return {"complete": "ready to close", "needs_clarification": "unclear, the office has to look", "no_evidence": "nothing received yet"}[c]


def _reject(ctx: AskContext, why: str) -> str:
    ctx.errors.append(why)
    return f"REJECTED: {why}"


def make_ask_tools(ctx: AskContext, facts: Facts):
    discs = {s["discipline"] for s in facts.sheets if s.get("discipline")} | {(d["item"].get("discipline") or d["item"]["item_id"].split("-")[0]) for d in facts.items}

    @tool
    def list_items(building: str = "", discipline: str = "", review: str = "", state: str = "") -> str:
        """The deficiencies, one line each, filtered. building: a street number such as 6895. discipline: a code such as EL,
        PL, AR, ME. review: a field review title such as "Field review 2". state: ready, needs, unclear or nothing."""
        bkey = facts.building_key(building) if building else ""
        rv = facts.review_by(review) if review else None
        rows = []
        for i in facts.items:
            d = i["item"]
            if building and facts.building_of_item(d) != bkey:
                continue
            if discipline and (d.get("discipline") or d["item_id"].split("-")[0]).upper() != discipline.strip().upper():
                continue
            if review and (not rv or d.get("review_id") != rv["id"]):
                continue
            if state and STATE_WORDS.get(i.get("completeness", "no_evidence")) != state.strip().lower():
                continue
            rows.append(f"{d['item_id']} · sheet {d.get('sheet') or '?'} · {d.get('location', '')} · {d.get('description', '')} · state: {_state(i)}")
        if building and not bkey:
            return f"no building {building!r} on this project; buildings are " + ", ".join(b["name"] for b in facts.buildings)
        if review and not rv:
            return f"no field review called {review!r}; reviews are " + ", ".join(r["title"] for r in facts.reviews)
        return "\n".join(rows) if rows else "no items match"

    @tool
    def item(item_id: str) -> str:
        """Everything on record for one deficiency: where, what, what closes it, what has been received, the engineer's call."""
        i = next((x for x in facts.items if x["item"]["item_id"].lower() == item_id.strip().lower()), None)
        if not i:
            return f"no item {item_id!r}"
        d = i["item"]
        got = [f"  received: {e.get('filename') or '?'} ({e.get('provenance') or ''}) — {e.get('rationale') or ''}" for e in i.get("evidence", [])]
        return "\n".join([f"{d['item_id']} · sheet {d.get('sheet') or '?'} · unit {d.get('unit') or '?'} · {d.get('level') or ''}",
                          f"  where: {d.get('location', '')}", f"  what: {d.get('description', '')}",
                          f"  send to close: {d.get('evidence_required', '')}", f"  state: {_state(i)}",
                          f"  recorded in: {next((r['title'] for r in facts.reviews if r['id'] == d.get('review_id')), 'imported list')}"] + got)

    @tool
    def documents(discipline: str = "") -> str:
        """The files in the project folder, one line each, optionally for one discipline code."""
        rows = []
        for d in facts.documents:
            if discipline and (d.get("discipline") or "").upper() != discipline.strip().upper():
                continue
            f = facts.filings.get((d.get("rel_path") or "").split("/")[-1]) or {}
            where = " · ".join(x for x in (f.get("building"), f.get("discipline")) if x)
            rows.append(f"{d.get('rel_path')} · {d.get('discipline') or '?'} · {d.get('dated') or 'undated'}"
                        + (" · current" if d.get("is_current") else "")
                        + (f" · filed under {where} by {'Closeout' if f.get('who') == 'closeout' else 'the engineer'}" if where else "")
                        + (f" · shown as {f['name']!r}" if f.get("name") else ""))
        return "\n".join(rows) if rows else "no documents match"

    @tool
    def answer(text: str, go_screen: str = "", building: str = "", discipline: str = "", review: str = "", item_id: str = "", sheet: str = "") -> str:
        """Give the answer, once. go_screen moves the engineer's screen: overview, deficiencies (filters: building,
        discipline, review), field (review), drawings, docs, messages, item (item_id), sheet (sheet number). Leave it empty
        for a plain answer."""
        text = " ".join(str(text or "").split())
        if not text:
            return _reject(ctx, "the answer is empty")
        if len(text) > MAX_ANSWER:
            return _reject(ctx, f"too long: keep the answer under {MAX_ANSWER} characters")
        low = text.lower()
        bad = [w for w in ASK_FORBIDDEN if w in low]
        if bad:
            return _reject(ctx, f"do not judge the work; drop {', '.join(bad)} and state what the records show")
        go: dict | None = None
        if go_screen:
            gs = go_screen.strip().lower()
            if gs not in SCREENS:
                return _reject(ctx, f"go_screen must be one of {', '.join(SCREENS)}")
            go = {"screen": gs}
            if building:
                key = facts.building_key(building)
                if not key:
                    return _reject(ctx, f"no building {building!r}; buildings are " + ", ".join(b["name"] for b in facts.buildings))
                go["building"] = key
            if discipline:
                code = discipline.strip().upper()
                if code not in discs:
                    return _reject(ctx, f"no discipline {discipline!r}; codes are " + ", ".join(sorted(discs)))
                go["discipline"] = code
            if review:
                rv = facts.review_by(review)
                if not rv:
                    return _reject(ctx, f"no field review {review!r}")
                go["review"] = rv["id"]
                go.setdefault("discipline", rv["discipline"])
            if gs == "item":
                iid = item_id.strip()
                if not any(x["item"]["item_id"].lower() == iid.lower() for x in facts.items):
                    return _reject(ctx, f"no item {item_id!r}")
                go["item_id"] = next(x["item"]["item_id"] for x in facts.items if x["item"]["item_id"].lower() == iid.lower())
            if gs == "sheet":
                sh = facts.sheet_by(sheet)
                if not sh:
                    return _reject(ctx, f"no sheet {sheet!r}")
                go["sheet_id"] = sh["id"]
                go["sheet"] = sh.get("sheet_number")
        ctx.recorded = {"text": text, "go": go}
        return "recorded"


    @tool
    def propose(kind: str, item_id: str = "", decision: str = "", note: str = "", review: str = "", discipline: str = "",
                location: str = "", description: str = "", evidence_required: str = "", file: str = "", building: str = "",
                name: str = "") -> str:
        """Prepare one change for the engineer to confirm on screen. kind: decide (item_id + decision accept|hold|reject,
        optional note), start_review (discipline code), finish_review (review), redraft_message (review), create_link (review),
        turn_off_link (review), edit_item (item_id + the fields to change: location, description, evidence_required),
        file_document (file = a file name from documents, plus what changes: building = building name or "site" for none,
        discipline = discipline code, name = the new display name; leave the others empty to keep them),
        add_discipline (discipline = a short code such as SP, name = what the folder is called, such as Sprinkler).
        The change is not made until the engineer confirms it."""
        k = kind.strip().lower()
        if k not in ACTIONS:
            return _reject(ctx, f"kind must be one of {', '.join(ACTIONS)}")
        it = next((x for x in facts.items if x["item"]["item_id"].lower() == item_id.strip().lower()), None) if item_id else None
        rv = facts.review_by(review) if review else None
        a: dict = {"kind": k}
        if k == "decide":
            if not it:
                return _reject(ctx, f"no item {item_id!r}")
            d = decision.strip().lower()
            if d not in DECISIONS:
                return _reject(ctx, "decision must be accept, hold or reject")
            a.update(item_id=it["item"]["item_id"], decision=d, note=" ".join(note.split())[:300],
                     label=f"Mark {it['item']['item_id']} as {DECISIONS[d].lower()}" + (f', with the note "{note.strip()}"' if note.strip() else ""),
                     method="POST", path=f"/items/{it['item']['item_id']}/decision", body={"decision": d, "note": " ".join(note.split())[:300] or None},
                     then={"screen": "item", "item_id": it["item"]["item_id"]})
        elif k == "start_review":
            code = discipline.strip().upper()
            folders = {x.get("code"): x.get("name") for x in facts.view.get("disciplines") or []}
            if code not in DISCIPLINES and code not in folders:
                return _reject(ctx, f"no discipline {discipline!r}; codes are " + ", ".join(sorted(set(DISCIPLINES) | set(folders))))
            dname = DISCIPLINES.get(code) or folders.get(code) or code
            live = next((r for r in facts.reviews if r["discipline"] == code and r["status"] == "active"), None)
            if live:
                return _reject(ctx, f"{live['title']} ({dname}) is already in progress; open it instead of starting another")
            a.update(discipline=code, label=f"Start a new {dname.lower()} field review", method="POST", path="/reviews",
                     body={"discipline": code}, then={"screen": "field", "discipline": code})
        elif k in ("finish_review", "redraft_message", "create_link", "turn_off_link"):
            if not rv:
                return _reject(ctx, f"no field review {review!r}; reviews are " + ", ".join(r["title"] for r in facts.reviews))
            title = f"{rv['title']} ({DISCIPLINES.get(rv['discipline'], rv['discipline'])})"
            if k == "finish_review":
                if rv["status"] != "active":
                    return _reject(ctx, f"{title} is already finished")
                a.update(review=rv["id"], label=f"Finish {title} and draft the message to the contractor", method="POST",
                         path=f"/reviews/{rv['id']}/finish", body=None, then={"screen": "messages"}, slow=True)
            elif k == "redraft_message":
                if rv["status"] != "finished":
                    return _reject(ctx, f"{title} is still in progress; finish it first")
                a.update(review=rv["id"], label=f"Draft a fresh message for {title}", method="POST",
                         path=f"/reviews/{rv['id']}/message", body=None, then={"screen": "messages"}, slow=True)
            elif k == "create_link":
                if rv["status"] != "finished":
                    return _reject(ctx, f"{title} is still in progress; the link comes after it is finished")
                if any(s_["review_id"] == rv["id"] and not s_.get("revoked_at") for s_ in facts.shares):
                    return _reject(ctx, f"{title} already has an active contractor link")
                a.update(review=rv["id"], label=f"Create the contractor link for {title}", method="POST",
                         path=f"/reviews/{rv['id']}/share", body=None, then={"screen": "messages"})
            else:
                live = next((s_ for s_ in facts.shares if s_["review_id"] == rv["id"] and not s_.get("revoked_at")), None)
                if not live:
                    return _reject(ctx, f"{title} has no active contractor link")
                a.update(review=rv["id"], token=live["id"], label=f"Turn off the contractor link for {title}", method="DELETE",
                         path=f"/shares/{live['id']}", body=None, then={"screen": "messages"})
        elif k == "edit_item":
            if not it:
                return _reject(ctx, f"no item {item_id!r}")
            fields = {f: " ".join(v.split()) for f, v in (("location", location), ("description", description),
                                                          ("evidence_required", evidence_required)) if v.strip()}
            if not fields:
                return _reject(ctx, "say what to change: location, description or evidence_required")
            if any(len(v) < 4 for v in fields.values()):
                return _reject(ctx, "the new wording is too short")
            names = {"location": "where it is", "description": "the wording", "evidence_required": "what closes it"}
            a.update(item_id=it["item"]["item_id"], fields=fields, label=f"Change {' and '.join(names[f] for f in fields)} on {it['item']['item_id']}",
                     method="PATCH", path=f"/findings/{it['item']['item_id']}", body=fields, then={"screen": "item", "item_id": it["item"]["item_id"]})
        elif k == "file_document":
            f = file.strip()
            names_ = {(d.get("rel_path") or "").split("/")[-1] for d in facts.documents}
            match = [n for n in names_ if n == f] or [n for n in names_ if f and f.lower() in n.lower()]
            if len(match) != 1:
                return _reject(ctx, "say which file: " + ("no file matches" if not match else "several match: " + ", ".join(sorted(match)[:6])))
            f = match[0]
            body: dict = {"file": f}
            parts = []
            if building.strip():
                b = building.strip()
                if b.lower() in ("site", "none", "no building"):
                    body["building"] = ""
                    parts.append("to the site")
                else:
                    hit = [x["name"] for x in facts.buildings if x["name"].lower() == b.lower() or x["key"].lower() == b.lower()
                           or b.lower() in x["name"].lower()]
                    if len(hit) != 1:
                        return _reject(ctx, "unknown building; the project has: " + ", ".join(x["name"] for x in facts.buildings))
                    body["building"] = hit[0]
                    parts.append(f"to {hit[0]}")
            if discipline.strip():
                d = discipline.strip().upper()
                codes = {x.get("code") for x in facts.view.get("disciplines") or []} | {s_.get("discipline") for s_ in facts.sheets}
                by_name = {DISCIPLINES.get(c, c).lower(): c for c in codes if c}
                d = d if d in codes else by_name.get(d.lower(), d)
                if d not in codes:
                    return _reject(ctx, "unknown discipline; the project has: " + ", ".join(sorted(c for c in codes if c)))
                body["discipline"] = d
                parts.append(f"under {DISCIPLINES.get(d, d)}")
            if name.strip():
                body["name"] = " ".join(name.split())[:120]
                parts.append(f"shown as “{body['name']}”")
            if len(body) == 1:
                return _reject(ctx, "say what changes: the building, the discipline or the name")
            a.update(file=f, label=f"File {f} {' '.join(parts)}", method="POST", path="/filing", body=body,
                     then={"screen": "docs"})
        elif k == "add_discipline":
            code = re.sub(r"[^A-Z0-9]", "", discipline.strip().upper())[:4]
            if not code:
                return _reject(ctx, "give the folder a short code, such as SP")
            have = {x.get("code") for x in facts.view.get("disciplines") or []} | {s_.get("discipline") for s_ in facts.sheets}
            if code in have:
                return _reject(ctx, f"{code} is already a folder on this project: Documents › Site › {DISCIPLINES.get(code, code)}, and it is on the Field review tab. "
                                    "Do not offer to add it; tell the engineer where it is")
            label_name = " ".join(name.split())[:60] or DISCIPLINES.get(code, "")
            if not label_name:
                return _reject(ctx, "say what the folder is called, such as Sprinkler")
            a.update(discipline=code, name=label_name, label=f"Add a {label_name} folder ({code}) to the project", method="POST",
                     path="/disciplines", body={"code": code, "name": label_name}, then={"screen": "docs", "folder": ["prj", "site", "site/" + code]})
        ctx.proposed = a
        return "prepared; now call answer with one sentence saying it is ready to confirm"

    return [list_items, item, documents, answer, propose]


def facts_text(facts: Facts, office: str) -> str:
    v = facts.view
    lines = [f"PROJECT: {v.get('name') or '(unnamed)'}" + (f" — {v['building_type']}" if v.get("building_type") else ""),
             f"OFFICE: {office}"]
    if facts.buildings:
        lines.append("BUILDINGS:")
        for b in facts.buildings:
            units = ", ".join(_unit_key(u).title() for u in b["units"]) or ("named only on the drawings" if b.get("from_drawings") else "no units")
            lines.append(f"- {b['name']}: {units}" + (f"; floors {', '.join(b['levels'])}" if b.get("levels") else ""))
    by_disc: dict[str, int] = {}
    for s in facts.sheets:
        by_disc[s.get("discipline") or "?"] = by_disc.get(s.get("discipline") or "?", 0) + 1
    if by_disc:
        lines.append("DRAWINGS: " + ", ".join(f"{DISCIPLINES.get(k, k)} ({k}) {n} sheets" for k, n in sorted(by_disc.items())))
    lines.append(f"DOCUMENTS ON FILE: {len(facts.documents)}")
    folders = v.get("disciplines") or []
    if folders:
        lines.append("FOLDERS ON THE PROJECT (Documents › Site, and each is on the Field review tab): " + ", ".join(
            f"{f.get('name') or DISCIPLINES.get(f.get('code'), f.get('code'))} ({f.get('code')})"
            + (" — added by the engineer, no files yet" if f.get("added_by") == "engineer" and not f.get("sheets") else "") for f in folders))
    if facts.reviews:
        lines.append("FIELD REVIEWS:")
        for r in facts.reviews:
            n = sum(1 for i in facts.items if i["item"].get("review_id") == r["id"])
            lines.append(f"- {r['title']} ({DISCIPLINES.get(r['discipline'], r['discipline'])}): {r['status']}, {n} items, started {str(r.get('started_at') or '')[:10]}")
    counts: dict[str, int] = {}
    for i in facts.items:
        counts[_state(i)] = counts.get(_state(i), 0) + 1
    lines.append(f"DEFICIENCIES: {len(facts.items)} in total" + ("; " + ", ".join(f"{n} {k}" for k, n in counts.items()) if counts else ""))
    links = sum(1 for s in facts.shares if not s.get("revoked_at"))
    via = sum(1 for b in facts.batches if b.get("via"))
    lines.append(f"WITH THE CONTRACTOR: {len(facts.drafts)} messages drafted, {len(facts.sends)} sent by email on the engineer's press, "
                 f"{links} contractor links active, {len(facts.batches)} evidence drops received ({via} through a link)")
    for s_ in facts.sends[-5:]:
        rv = next((r for r in facts.reviews if r["id"] == s_["review_id"]), None)
        lines.append(f"- sent {str(s_['at'])[:10]} to {s_['to_addr']} for {rv['title'] if rv else s_['review_id']}"
                     f" ({'from the office Gmail' if s_['via'] == 'gmail' else 'from the app' if s_['via'] == 'ses' else 'from the engineer\'s mail app'}); replies come back through the link")
    emails = [i for i in facts.inbound if i.get("status") != "unplaced"]
    if emails:
        lines.append(f"EMAILS RECEIVED: {len(emails)} read from the office mailbox and filed to their reviews")
        for i in emails[-5:]:
            rv = next((r for r in facts.reviews if r["id"] == i["review_id"]), None)
            lines.append(f"- {str(i['at'])[:10]} from {i['from_addr']} for {rv['title'] if rv else i['review_id']}: {i['subject'][:80]}"
                         f" ({i['files']} files){'; text: ' + i['text'][:200].replace(chr(10), ' ') if i.get('text') else ''}")
    return "\n".join(lines)


def where_text(where: dict | None) -> str:
    w = where or {}
    parts = [f"screen: {w.get('tab') or 'overview'}"]
    f = w.get("filters") or {}
    if f.get("bld"):
        parts.append(f"building filter: {f['bld']}")
    if f.get("disc"):
        parts.append(f"discipline filter: {f['disc']}")
    if f.get("rv"):
        parts.append(f"field review filter: {f['rv']}")
    if w.get("item_id"):
        parts.append(f"open item: {w['item_id']}")
    return "; ".join(parts)


SPOKEN_STYLE = ("SPOKEN: this answer is read aloud to the engineer, who is walking the site. One or two short sentences "
                "that sound natural when spoken. No lists, no headings, no symbols. Say item numbers as they are (EL-01).")
HISTORY_TURNS = 6


def history_text(history: list[dict] | None) -> str:
    """The last few exchanges, so a follow-up like "and the second floor?" has something to follow."""
    turns = [h for h in (history or []) if isinstance(h, dict) and str(h.get("q") or "").strip()][-HISTORY_TURNS:]
    if not turns:
        return ""
    lines = ["EARLIER IN THIS CONVERSATION (oldest first):"]
    for h in turns:
        lines.append(f"- Engineer: {' '.join(str(h['q']).split())[:300]}")
        lines.append(f"  Closeout: {' '.join(str(h.get('a') or '').split())[:300]}")
    return "\n".join(lines)


def ask(store: Store, project_id: str, question: str, where: dict | None = None, settings: Settings = SETTINGS, model=None,
        office: str = "the engineer's office", history: list[dict] | None = None, spoken: bool = False) -> dict:
    """Answer one question from the records. Raises ValueError for an empty question."""
    q = " ".join(str(question or "").split())
    if not q:
        raise ValueError("ask something first")
    facts = gather(store, project_id)
    ctx = AskContext()
    agent = Agent(model=model or make_model(settings, fast=True), tools=make_ask_tools(ctx, facts), system_prompt=ASK_SYSTEM,
                  callback_handler=None)
    blocks = [{"text": facts_text(facts, office)}, {"text": f"ENGINEER IS ON: {where_text(where)}"}]
    if history_text(history):
        blocks.append({"text": history_text(history)})
    if spoken:
        blocks.append({"text": SPOKEN_STYLE})
    blocks += [{"text": f"QUESTION: {q[:600]}"}, {"text": "Look up what you need, then call answer once."}]
    result = agent(blocks)
    if not ctx.recorded:
        # Now and then the model replies in plain text instead of calling answer. Use that text rather than
        # failing the question; it stays a read-only answer with no screen move.
        plain = str(result).strip() if result is not None else ""
        if not plain or len(plain) > 1200:
            raise RuntimeError("no answer was recorded" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
        ctx.recorded = {"text": plain, "go": None}
    return {**ctx.recorded, "action": ctx.proposed, "usage": _usage(result), "rejections": ctx.errors}
