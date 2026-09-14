"""Field review: the engineer stands in the building, taps a spot on a sheet, takes a photo, and the agent
turns that into a deficiency record with a location the contractor can find.

The agent only proposes. It reads the photo, the pinned crop of the sheet, the whole sheet with the pin drawn on
it, what the sheet-reading agent already found on that sheet (units, levels, rooms), and the engineer's own note.
It calls record_field_note once. Deterministic code validates the evidence slots and the wording; the engineer
edits and saves. Nothing here decides whether work is acceptable.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw
from strands import Agent, tool

from .agent import FORBIDDEN_WORDS, _usage
from .config import SETTINGS, Settings
from .ingest import MAX_MODEL_EDGE, image_bytes_for_model
from .pipeline import make_model
from .project import DISCIPLINES, sheet_text_for_agent
from .register import RegisterError, parse_slots
from .store import Store

PIN_CROP = 0.22   # crop half-width as a fraction of the sheet's longest edge
PIN_COLOUR = (232, 89, 12)  # matches the UI's --hot

FIELD_SYSTEM = """You are the field clerk for a consulting engineer's deficiency review.
The engineer is on site. They tapped a spot on a drawing sheet, took a photo of what they see, and may have typed a short note.
Your job: write the deficiency record the way the engineer would, so a contractor can find the spot and fix it,
and so a later evidence check knows what proof to ask for.

You will get:
1. The engineer's photo of the deficiency (if taken).
2. A close-up of the sheet around the pin. The pin is the orange circle.
3. The whole sheet with the same pin drawn on it, so you can see which unit / level / part of the plan it sits in.
4. What was already read off this sheet and the project (units, levels, rooms). This is document provenance: prefer it
   over guessing. Unit letters and level names must come from it.
5. The engineer's note, if any. The note wins over your own reading when they disagree.

Call record_field_note exactly once.
- location: one line a contractor can walk to. Unit, level, room, then the spot: "Unit C, Upper Floor, Bath 2: wall behind toilet".
  If the pin sits in a space you cannot name from the sheet, say what you can ("Unit C, Upper Floor, near stair") and set space "".
- unit / level / space: the parts of that line, using the exact names from the project context ("" when unknown).
- description: what is wrong, in plain engineer's words, 1-2 sentences, present tense. Describe what the photo shows. Do not guess causes you cannot see.
- evidence_required: what the contractor must send to close it, as "type: what" pairs separated by ";".
  Types are photo, report, letter, document. Examples: "photo: repaired penetration with fire stop label visible";
  "photo: completed; report: electrician's test sheet". One photo slot is the minimum.
- discipline: the two-letter code of the trade that owns the fix (AR architectural, EL electrical, PL plumbing, ME mechanical, ST structural).
You never judge whether work is acceptable, compliant, or complete. Never use those words.
If the photo shows nothing wrong and there is no note, still record what is at the pin and set description to what you see, prefixed "To confirm:".
"""


@dataclass
class FieldContext:
    recorded: dict | None = None
    errors: list[str] = field(default_factory=list)


def _clean(s) -> str:
    return " ".join(str(s or "").split())


def make_field_tools(ctx: FieldContext, allowed_units: set[str], allowed_levels: set[str], allowed_sheets: set[str] | None = None):
    @tool
    def record_field_note(location: str, description: str, evidence_required: str, discipline: str,
                          unit: str = "", level: str = "", space: str = "", sheet: str = "") -> str:
        """Record the proposed deficiency for the pinned spot. Call exactly once.

        Args:
            location: one line a contractor can walk to (unit, level, room, spot).
            description: what is wrong, 1-2 sentences, present tense.
            evidence_required: "photo: ...; report: ..." pairs; types photo, report, letter, document.
            discipline: two-letter trade code (AR, EL, PL, ME, ST).
            unit: unit label exactly as in the project context, or "".
            level: level name exactly as in the project context, or "".
            space: room / space name, or "".
            sheet: only when asked to choose a sheet: the sheet number from the list you were given.
        """
        location, description, evidence_required = _clean(location), _clean(description), _clean(evidence_required)
        unit, level, space, discipline, sheet = _clean(unit), _clean(level), _clean(space), _clean(discipline).upper(), _clean(sheet)
        if allowed_sheets is not None and sheet not in allowed_sheets:
            return _reject(ctx, f"sheet must be one of {', '.join(sorted(allowed_sheets))}")
        if len(location) < 8:
            return _reject(ctx, "location too short; name unit, level and the spot")
        if len(description) < 12:
            return _reject(ctx, "description too short")
        low = (location + " " + description).lower()
        bad = [w for w in FORBIDDEN_WORDS if w in low]
        if bad:
            return _reject(ctx, f"forbidden judgement words: {', '.join(bad)}")
        try:
            slots = parse_slots(evidence_required)
        except RegisterError as e:
            return _reject(ctx, f"evidence_required: {e}")
        if not any(s.type == "photo" for s in slots):
            return _reject(ctx, "evidence_required needs at least one photo slot")
        if discipline not in DISCIPLINES:
            return _reject(ctx, f"discipline must be one of {', '.join(sorted(DISCIPLINES))}")
        if unit and allowed_units and unit not in allowed_units:
            return _reject(ctx, f"unit must be one of {', '.join(sorted(allowed_units))} or empty")
        if level and allowed_levels and level not in allowed_levels:
            return _reject(ctx, f"level must be one of {', '.join(sorted(allowed_levels))} or empty")
        ctx.recorded = {"location": location, "description": description, "evidence_required": evidence_required,
                        "slots": [s.__dict__ for s in slots], "discipline": discipline, "unit": unit, "level": level, "space": space,
                        "sheet": sheet}
        return "recorded"

    return [record_field_note]


def _reject(ctx: FieldContext, why: str) -> str:
    ctx.errors.append(why)
    return "REJECTED: " + why


def pin_images(image_path: Path, pin_x: float, pin_y: float) -> tuple[bytes, bytes]:
    """(close-up around the pin, whole sheet) as JPEG bytes, both with the pin drawn on. pin_x/pin_y are 0..1."""
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        px, py = int(pin_x * w), int(pin_y * h)
        half = int(max(w, h) * PIN_CROP / 2)
        box = (max(0, px - half), max(0, py - half), min(w, px + half), min(h, py + half))
        crop = im.crop(box)
        _draw_pin(crop, px - box[0], py - box[1], max(12, crop.width // 40))
        crop.thumbnail((MAX_MODEL_EDGE, MAX_MODEL_EDGE))
        whole = im.copy()
        _draw_pin(whole, px, py, max(18, max(w, h) // 60))
        whole.thumbnail((MAX_MODEL_EDGE, MAX_MODEL_EDGE))
    return _jpeg(crop), _jpeg(whole)


def _draw_pin(im: Image.Image, x: int, y: int, r: int) -> None:
    d = ImageDraw.Draw(im)
    width = max(3, r // 4)
    d.ellipse((x - r, y - r, x + r, y + r), outline=PIN_COLOUR, width=width)
    d.ellipse((x - width, y - width, x + width, y + width), fill=PIN_COLOUR)


def _jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _sheet_context(store: Store, project_id: str, sheet: dict) -> tuple[str, set[str], set[str]]:
    prj = store.project(project_id) or {}
    model = prj.get("model", {}) if prj else {}
    units = {u.get("label", "") for u in model.get("units", []) if u.get("label")}
    levels = {l.get("name", "") for l in model.get("levels", []) if l.get("name")}
    for u in model.get("units", []):
        for l in u.get("levels", []) if isinstance(u.get("levels"), list) else []:
            if isinstance(l, str):
                levels.add(l)
            elif isinstance(l, dict) and l.get("name"):
                levels.add(l["name"])
    read = sheet.get("read") or {}
    parts = [sheet_text_for_agent(store, project_id)]
    ref = sheet.get("sheet_number") or f"{sheet['discipline']} p.{sheet['page']}"
    parts.append(f"\nTHIS SHEET: {ref} — {sheet.get('title', '')} ({DISCIPLINES.get(sheet['discipline'], sheet['discipline'])})")
    if read:
        summary = {k: read[k] for k in ("sheet_kind", "summary", "units", "levels", "spaces", "notes") if k in read}
        parts.append("What the sheet-reading agent found on it (document provenance):\n" + json.dumps(summary)[:5000])
    return "\n".join(parts), units, levels


def suggest_field_note(store: Store, project_id: str, sheet_id: str, pin_x: float, pin_y: float,
                       photo_bytes: bytes | None, note: str = "", settings: Settings = SETTINGS, model=None,
                       discipline_hint: str = "", tidy: bool = False, place: bool = False) -> dict:
    """One synchronous model call. Returns the proposal plus usage; raises RuntimeError if the agent recorded nothing.
    With place, only the spot is read off the sheet (unit, level, room, location) the moment the pin goes down."""
    sheet = store.sheet(sheet_id)
    if not sheet or sheet["project_id"] != project_id:
        raise ValueError("sheet not in this project")
    context, units, levels = _sheet_context(store, project_id, sheet)
    ctx = FieldContext()
    agent = Agent(model=model or make_model(settings), tools=make_field_tools(ctx, units, levels),
                  system_prompt=TIDY_SYSTEM if tidy else PLACE_SYSTEM if place else FIELD_SYSTEM, callback_handler=None)
    crop, whole = pin_images(Path(sheet["image_path"]), pin_x, pin_y)
    if tidy or place:
        photo_bytes = None
    content: list[dict] = []
    if photo_bytes:
        content += [{"text": "Engineer's photo at the pin:"}, {"image": {"format": "jpeg", "source": {"bytes": photo_bytes}}}]
    elif not tidy and not place:
        content.append({"text": "No photo was taken; work from the sheet and the note."})
    content += [
        {"text": "Sheet close-up around the pin (orange circle):"}, {"image": {"format": "jpeg", "source": {"bytes": crop}}},
        {"text": "Whole sheet with the pin:"}, {"image": {"format": "jpeg", "source": {"bytes": whole}}},
        {"text": "PROJECT AND SHEET CONTEXT (document provenance):\n" + context},
        {"text": f"Review discipline: {DISCIPLINES.get(discipline_hint, discipline_hint) or '(not stated)'}. "
                 f"Pin at x={pin_x:.3f}, y={pin_y:.3f} of the sheet (0,0 is top-left)."},
        {"text": "ENGINEER'S NOTE: " + (note.strip() or "(none)")},
        {"text": "Call record_field_note once."},
    ]
    result = agent(content)
    if not ctx.recorded:
        raise RuntimeError("agent finished without a record" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
    return {**ctx.recorded, "usage": _usage(result), "rejections": ctx.errors}


TIDY_SYSTEM = FIELD_SYSTEM + """
THIS TIME THE ENGINEER HAS ALREADY WRITTEN IT. Their words (the ENGINEER'S NOTE) are the record; your job is only to put them in order.
- description: their words as a clean record: correct spelling and grammar, full sentences, the trade's usual terms, present tense.
  Keep every fact they gave. Add no fact, defect, cause, size or count that is not in their words. If they said something is fine,
  or are only logging what they saw, keep it that way: do not turn it into a problem and do not add "To confirm:".
- location / unit / level / space: from the pin and the sheet context, as usual; their words win when they name the place.
- evidence_required: only what their words ask for; otherwise the minimum "photo: the work at this spot".
There is no photo in this request; do not describe one.
"""


PLACE_SYSTEM = FIELD_SYSTEM + """
THIS TIME THE ENGINEER HAS ONLY PUT THE PIN DOWN. Nothing has been written yet and there is no photo.
Your job is only the place: location, unit, level and space, read from the pin, the sheet and the project context.
- location: the line a contractor can walk to, ending with what is drawn at the pin ("Unit 2, Upper Floor, Bath: wall beside the basin").
- description: "Location only: " followed by what is drawn at the pin, in a few words. Do not describe any problem.
- evidence_required: "photo: the work at this spot".
"""


LOCATE_SYSTEM = FIELD_SYSTEM + """
THIS TIME THERE IS NO PIN YET. The engineer took the photo first and told you which unit and level they are standing in.
You also get the list of sheets for this discipline with what each one shows. Choose the ONE sheet the deficiency belongs on
(the plan that shows that unit and level and the trade concerned) and pass its sheet number as `sheet`. The engineer will tap the
exact spot on that sheet afterwards. Use the unit and level the engineer gave you; do not change them.
"""


def _sheet_list(store: Store, project_id: str, discipline: str) -> tuple[str, dict[str, str]]:
    """Compact text list of a discipline's sheets (number, title, kind, spaces) and number -> sheet id."""
    lines, ids = [], {}
    for sh in store.sheets(project_id):
        if sh["discipline"] != discipline.upper():
            continue
        ref = sh.get("sheet_number") or f"{sh['discipline']} p.{sh['page']}"
        ids[ref] = sh["id"]
        read = sh.get("read") or {}
        spaces = read.get("spaces") or []
        where = sorted({f"{s.get('unit', '')} / {s.get('level', '')}".strip(" /") for s in spaces if isinstance(s, dict)})
        rooms = [s.get("name", "") for s in spaces if isinstance(s, dict) and s.get("name")]
        lines.append(f"- {ref}: {sh.get('title', '')} [{read.get('sheet_kind', '?')}]"
                     + (f"; shows {'; '.join(where)[:300]}" if where else "")
                     + (f"; rooms {', '.join(rooms)[:300]}" if rooms else ""))
    return "\n".join(lines), ids


def locate_field_note(store: Store, project_id: str, discipline: str, photo_bytes: bytes, unit: str = "", level: str = "",
                      note: str = "", gps: str = "", settings: Settings = SETTINGS, model=None) -> dict:
    """Photo first: the agent picks the sheet and writes the record; the engineer pins the spot afterwards."""
    prj = store.project(project_id) or {}
    model_ = prj.get("model", {}) if prj else {}
    units = {u.get("label", "") for u in model_.get("units", []) if u.get("label")}
    levels = {l.get("name", "") for l in model_.get("levels", []) if l.get("name")}
    for u in model_.get("units", []):
        for l in u.get("levels", []) if isinstance(u.get("levels"), list) else []:
            levels.add(l if isinstance(l, str) else l.get("name", ""))
    levels.discard("")
    listing, ids = _sheet_list(store, project_id, discipline)
    if not ids:
        raise ValueError("no sheets for that discipline")
    ctx = FieldContext()
    agent = Agent(model=model or make_model(settings), tools=make_field_tools(ctx, units, levels, set(ids)),
                  system_prompt=LOCATE_SYSTEM, callback_handler=None)
    content = [
        {"text": "Engineer's photo of the deficiency:"}, {"image": {"format": "jpeg", "source": {"bytes": photo_bytes}}},
        {"text": "PROJECT CONTEXT (document provenance):\n" + sheet_text_for_agent(store, project_id)},
        {"text": f"SHEETS FOR {DISCIPLINES.get(discipline.upper(), discipline)} (choose one by its number):\n" + listing},
        {"text": f"ENGINEER IS STANDING IN: unit {unit or '(not given)'}, level {level or '(not given)'}."
                 + (f" Phone GPS: {gps} (outdoor accuracy only; it cannot tell buildings or floors apart)." if gps else "")},
        {"text": "ENGINEER'S NOTE: " + (note.strip() or "(none)")},
        {"text": "Call record_field_note once, with the sheet number."},
    ]
    result = agent(content)
    if not ctx.recorded:
        raise RuntimeError("agent finished without a record" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
    return {**ctx.recorded, "sheet_id": ids[ctx.recorded["sheet"]], "usage": _usage(result), "rejections": ctx.errors}


# ---- finishing a review: the package for the contractor and the covering message ------------------------

MESSAGE_SYSTEM = """You write the covering message that goes from a consulting engineer's office to the contractor
after a field review. The engineer walked the site today and recorded the deficiencies listed below. Each one already has
the wording the engineer approved; do not rewrite, soften or judge it, and do not add deficiencies of your own.

Call record_message exactly once.
- subject: short, e.g. "Field review 2 (Electrical): 3 items to close at Laurel Street Townhouses".
- body: plain text, courteous and brief. Open with one or two sentences saying what was reviewed and when.
  Then list EVERY item, one block each, in this shape:
    EL-01 · Unit C, Upper Floor, Bath 2: wall behind toilet
    Receptacle beside the basin has no cover plate.
    Send to close: photo: cover plate installed
  Then say what happens next: for each item, reply with the evidence named above; the engineer's office checks it and
  confirms or asks for more. Ask for it by [date]. Write exactly "[date]" as a placeholder; the engineer fills it in.
  Sign off as "{office}". No personal names, no phone numbers, no street addresses beyond the project name.
You never say that work is acceptable, compliant, complete, approved or closed. Never use those words.
"""


@dataclass
class MessageContext:
    recorded: dict | None = None
    errors: list[str] = field(default_factory=list)


def make_message_tools(ctx: MessageContext, item_ids: list[str], quoted: tuple[str, ...] | list[str] = ()):
    """quoted: the engineer's own item wording. The message repeats it word for word, so a word like "closed" in it
    ("gap closed") is the engineer's, not a judgement the model added; only the rest of the message is checked for those."""
    quoted_low = sorted({" ".join(q.lower().split()) for q in quoted if q and q.strip()}, key=len, reverse=True)
    @tool
    def record_message(subject: str, body: str) -> str:
        """Record the covering message to the contractor. Call exactly once.

        Args:
            subject: one line, under 120 characters.
            body: the full plain-text message; every deficiency number must appear in it.
        """
        subject, body = _clean(subject), str(body or "").strip()
        if not 8 <= len(subject) <= 120:
            return _reject(ctx, "subject must be 8 to 120 characters")
        if len(body) < 40:
            return _reject(ctx, "body too short")
        missing = [i for i in item_ids if i not in body]
        if missing:
            return _reject(ctx, f"body must mention every item; missing {', '.join(missing)}")
        low = " ".join((subject + " " + body).lower().split())
        for q in quoted_low:
            low = low.replace(q, " | ")
        bad = [w for w in FORBIDDEN_WORDS if w in low]
        if bad:
            return _reject(ctx, f"forbidden judgement words: {', '.join(bad)}")
        if "[date]" not in body:
            return _reject(ctx, 'body must ask for the evidence by "[date]" (the engineer fills the date in)')
        ctx.recorded = {"subject": subject, "body": body}
        return "recorded"

    return [record_message]


def review_package(store: Store, project_id: str, review_id: str) -> dict:
    """What goes to the contractor for one field review: every item with its photo, sheet and pin. Deterministic."""
    rv = store.review(review_id)
    if not rv or rv["project_id"] != project_id:
        raise ValueError("no such review")
    prj = store.project(project_id) or {}
    sheets = {s["id"]: s for s in store.sheets(project_id)}
    items = []
    for d in store.review_items(project_id, review_id):
        sh = sheets.get(d.get("sheet_id") or "")
        items.append({
            "item_id": d["item_id"], "location": d["location"], "description": d["description"],
            "evidence_required": d["evidence_required"], "slots": d["slots"],
            "unit": d.get("unit", ""), "level": d.get("level", ""), "space": d.get("space", ""),
            "sheet": d.get("sheet", ""), "sheet_title": (sh or {}).get("title", ""), "sheet_id": d.get("sheet_id", ""),
            "pin": [d["pin_x"], d["pin_y"]] if d.get("pin_x") is not None else None,
            "photo": bool(d.get("reference_photo")), "taken_at": (d.get("ref_meta") or {}).get("taken_at", ""),
        })
    return {"review_id": review_id, "title": rv["title"], "discipline": rv["discipline"],
            "discipline_name": DISCIPLINES.get(rv["discipline"], rv["discipline"]), "sequence": rv["sequence"],
            "project": prj.get("name", ""), "started_at": rv["started_at"], "finished_at": rv.get("finished_at"),
            "items": items, "count": len(items), "photos": sum(1 for i in items if i["photo"])}


def draft_review_message(store: Store, project_id: str, review_id: str, settings: Settings = SETTINGS, model=None,
                         office: str = "the engineer's office") -> dict:
    """One model call: the covering message for a finished review. Raises ValueError when there is nothing to send."""
    pkg = review_package(store, project_id, review_id)
    if not pkg["items"]:
        raise ValueError("this review has no deficiencies, so there is nothing to send")
    ctx = MessageContext()
    agent = Agent(model=model or make_model(settings), tools=make_message_tools(ctx, [i["item_id"] for i in pkg["items"]],
                                                                         [i[k] for i in pkg["items"] for k in ("location", "description", "evidence_required")]),
                  system_prompt=MESSAGE_SYSTEM.replace("{office}", office), callback_handler=None)
    lines = [f"PROJECT: {pkg['project'] or '(unnamed)'}", f"REVIEW: {pkg['title']} ({pkg['discipline_name']}), walked {pkg['started_at'][:10]}",
             f"ITEMS ({pkg['count']}):"]
    for i in pkg["items"]:
        lines += [f"- {i['item_id']} | sheet {i['sheet'] or '?'} | {i['location']}", f"  What is wrong: {i['description']}",
                  f"  Send to close: {i['evidence_required']}"]
    result = agent([{"text": "\n".join(lines)}, {"text": "Call record_message once."}])
    if not ctx.recorded:
        raise RuntimeError("agent finished without a message" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
    return {**ctx.recorded, "package": pkg, "usage": _usage(result), "rejections": ctx.errors}
