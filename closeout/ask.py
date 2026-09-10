"""Ask Closeout: one model call that answers a question from the project's own records and, when it helps,
moves the screen. It reads; it never changes a record. Layer one of the in-app assistant."""

from __future__ import annotations

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
- Never mention tools, models, prompts or this system message."""


@dataclass
class AskContext:
    recorded: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class Facts:
    """Everything the tools can look at, computed once per question."""
    view: dict
    buildings: list[dict]
    items: list[dict]          # packet items: {"item", "completeness", "missing_slots", "evidence", "decision", ...}
    reviews: list[dict]
    documents: list[dict]
    drafts: list[dict]
    shares: list[dict]
    batches: list[dict]
    sheets: list[dict]

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
                 documents=view.get("documents") or [], drafts=store.all_drafts(project_id), shares=store.shares(project_id),
                 batches=store.batches(project_id), sheets=view.get("sheets") or [])


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
        rows = [f"{d.get('rel_path')} · {d.get('discipline') or '?'} · {d.get('dated') or 'undated'}" + (" · current" if d.get("is_current") else "")
                for d in facts.documents if not discipline or (d.get("discipline") or "").upper() == discipline.strip().upper()]
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

    return [list_items, item, documents, answer]


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
    lines.append(f"WITH THE CONTRACTOR: {len(facts.drafts)} messages drafted (none sent by the app), {links} contractor links active, "
                 f"{len(facts.batches)} evidence drops received ({via} through a link)")
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


def ask(store: Store, project_id: str, question: str, where: dict | None = None, settings: Settings = SETTINGS, model=None,
        office: str = "the engineer's office") -> dict:
    """Answer one question from the records. Raises ValueError for an empty question."""
    q = " ".join(str(question or "").split())
    if not q:
        raise ValueError("ask something first")
    facts = gather(store, project_id)
    ctx = AskContext()
    agent = Agent(model=model or make_model(settings, fast=True), tools=make_ask_tools(ctx, facts), system_prompt=ASK_SYSTEM,
                  callback_handler=None)
    result = agent([{"text": facts_text(facts, office)}, {"text": f"ENGINEER IS ON: {where_text(where)}"},
                    {"text": f"QUESTION: {q[:600]}"}, {"text": "Look up what you need, then call answer once."}])
    if not ctx.recorded:
        raise RuntimeError("no answer was recorded" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
    return {**ctx.recorded, "usage": _usage(result), "rejections": ctx.errors}
