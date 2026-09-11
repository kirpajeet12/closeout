"""Documents: what the project folder holds, arranged as the site is built (site → building → discipline → sheets), and one
agent call that reasons about what is still missing.

The tree is plain code over the sheet readings. The agent only gets text (no images): the project facts, the buildings, every
current sheet with its kind and one-line summary, the file names in the folder and the office's occupancy checklist. It answers
through tools whose arguments are checked against those lists, so it cannot name a discipline, a building, a checklist row or
a file that does not exist. Nothing it records is an engineering determination: the engineer reads the list and decides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from strands import Agent, tool

from .agent import FORBIDDEN_WORDS, _usage
from .config import SETTINGS, Settings
from .pipeline import make_model
from .project import DISCIPLINES
from .store import Store

# The office's "Final Occupancy Documents Checklist" (docs/vision/research/occupancy-documents-checklist.md), one row per
# document: (group, name, file-name pattern). Served to the web app so there is one copy.
OCCUPANCY_DOCS: list[tuple[str, str, str]] = [
    ("Builder's own items", "Occupancy permit application", r"occupancy permit"),
    ("Builder's own items", "Demonstration test protocol", r"demonstration test"),
    ("Builder's own items", "Fire safety plan", r"fire safety plan"),
    ("Builder's own items", "Non-encroachment survey", r"encroachment"),
    ("Builder's own items", "Confirmation of development permit compliance", r"development permit compliance|dp compliance"),
    ("Letters of assurance", "Schedule C-A and C-B, coordinating professional", r"sched(ule)?[\s_-]*c[\s_-]*a\b|\bc-?a[\s_-]+crp"),
    ("Letters of assurance", "Schedule C-B, civil", r"(sched(ule)?[\s_-]*c[\s_-]*b|\bc-b\b).*civil|civil.*\bc[\s_-]*b\b"),
    ("Letters of assurance", "Schedule C-B, geotechnical", r"(sched(ule)?[\s_-]*c[\s_-]*b|\bc-b\b).*geotech|geotech.*\bc[\s_-]*b\b"),
    ("Letters of assurance", "Schedule C-B, structural", r"(sched(ule)?[\s_-]*c[\s_-]*b|\bc-b\b).*struct|struct.*\bc[\s_-]*b\b"),
    ("Letters of assurance", "Schedule C-B, mechanical", r"(sched(ule)?[\s_-]*c[\s_-]*b|\bc-b\b).*mech|mech.*\bc[\s_-]*b\b"),
    ("Letters of assurance", "Schedule C-B, electrical", r"(sched(ule)?[\s_-]*c[\s_-]*b|\bc-b\b).*elec|elec.*\bc[\s_-]*b\b"),
    ("Letters of assurance", "Schedule C-B, plumbing", r"(sched(ule)?[\s_-]*c[\s_-]*b|\bc-b\b).*plumb|plumb.*\bc[\s_-]*b\b"),
    ("Letters of assurance", "Schedule C-B, fire protection (sprinkler)", r"(sched(ule)?[\s_-]*c[\s_-]*b|\bc-b\b).*(fire|sprink)|(fire|sprink).*\bc[\s_-]*b\b"),
    ("Schedule B, electrical", "Fire alarm verification certificate and report", r"fire alarm verif|s537"),
    ("Schedule B, electrical", "Appendix C of CAN/ULC-S537", r"appendix c\b|s537"),
    ("Schedule B, electrical", "ULC certificate for the monitoring station, site specific", r"monitoring station|ulc cert"),
    ("Schedule B, electrical", "CAN/ULC-S1001 certificate", r"s1001"),
    ("Schedule B, electrical", "Bi-directional amplification, design and installation confirmation", r"bi-?directional|\bbda\b"),
    ("Schedule S-B, during construction", "Envelope", r"s[\s_-]*b\b.*envelope|envelope.*s[\s_-]*b\b"),
    ("Schedule S-B, during construction", "Windows", r"s[\s_-]*b\b.*window|window.*s[\s_-]*b\b"),
    ("Schedule S-B, during construction", "Guards", r"s[\s_-]*b\b.*guard|guard.*s[\s_-]*b\b"),
    ("Schedule S-B, during construction", "Mechanical seismic", r"s[\s_-]*b\b.*mech.*seismic|mech.*seismic.*s[\s_-]*b\b"),
    ("Schedule S-B, during construction", "Plumbing seismic", r"s[\s_-]*b\b.*plumb.*seismic|plumb.*seismic.*s[\s_-]*b\b"),
    ("Schedule S-B, during construction", "Electrical seismic", r"s[\s_-]*b\b.*elec.*seismic|elec.*seismic.*s[\s_-]*b\b"),
    ("Schedule S-B, during construction", "Fire sprinkler seismic", r"s[\s_-]*b\b.*sprink.*seismic|sprink.*seismic.*s[\s_-]*b\b"),
    ("Schedule S-C, at completion", "Envelope", r"s[\s_-]*c\b.*envelope|envelope.*s[\s_-]*c\b"),
    ("Schedule S-C, at completion", "Windows", r"s[\s_-]*c\b.*window|window.*s[\s_-]*c\b"),
    ("Schedule S-C, at completion", "Guards", r"s[\s_-]*c\b.*guard|guard.*s[\s_-]*c\b"),
    ("Schedule S-C, at completion", "Mechanical seismic", r"s[\s_-]*c\b.*mech.*seismic|mech.*seismic.*s[\s_-]*c\b"),
    ("Schedule S-C, at completion", "Plumbing seismic", r"s[\s_-]*c\b.*plumb.*seismic|plumb.*seismic.*s[\s_-]*c\b"),
    ("Schedule S-C, at completion", "Electrical seismic", r"s[\s_-]*c\b.*elec.*seismic|elec.*seismic.*s[\s_-]*c\b"),
    ("Schedule S-C, at completion", "Fire sprinkler seismic", r"s[\s_-]*c\b.*sprink.*seismic|sprink.*seismic.*s[\s_-]*c\b"),
    ("Trade certificates and reports", "Sprinkler material test certificate, underground piping", r"sprink.*(material|test).*underground|underground.*sprink"),
    ("Trade certificates and reports", "Sprinkler material test certificate, above-ground piping", r"sprink.*(material|test).*above|above.?ground.*sprink"),
    ("Trade certificates and reports", "Standpipe material test certificate, underground (NFPA 14)", r"standpipe.*underground|underground.*standpipe"),
    ("Trade certificates and reports", "Standpipe material test certificate, above ground (NFPA 14)", r"standpipe.*above|above.?ground.*standpipe"),
    ("Trade certificates and reports", "Backflow preventer test report", r"backflow"),
    ("Trade certificates and reports", "Chlorination certificate", r"chlorinat|disinfect"),
    ("Trade certificates and reports", "Heat trace confirmation letter", r"heat.?trac"),
    ("Trade certificates and reports", "Parkade CO detector calibration certificate", r"\bco\b.*(detector|calibrat)|carbon monoxide"),
    ("Trade certificates and reports", "HVAC balancing report, life-safety fans", r"balanc"),
]
CHECKLIST_NAMES = {name.lower(): name for _, name, _ in OCCUPANCY_DOCS}

# Sheet kinds that belong to one building; everything else (site plan, notes, details, schedules, renderings…) is site-wide.
BUILDING_KINDS = {"floor_plan", "plan", "elevation", "section", "roof_plan"}
_BUILDING_RE = re.compile(r"(\d{2,6})\s+([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3})")
MAX_MISSING = 25
MAX_QUESTIONS = 5   # the agent may leave this many blanks for the engineer to fill
# "certificate" is a document name here, not a judgement; the other judgement words stay banned.
DOC_FORBIDDEN = tuple(w for w in FORBIDDEN_WORDS if w != "certif")


def building_of(text: str) -> dict | None:
    """'#1 6895 Elm St' → {key: '6895', name: '6895 Elm St', street: 'laurel st'}."""
    m = _BUILDING_RE.search(str(text or ""))
    if not m:
        return None
    street = re.sub(r"[\s,]+$", "", m.group(2))
    return {"key": m.group(1), "name": f"{m.group(1)} {street}", "street": street.lower()}


def sheet_mentions(sh: dict, b: dict) -> bool:
    r = sh.get("read") or {}
    texts = [sh.get("title") or "", *(r.get("units") or []), *[s.get("unit") or "" for s in (r.get("spaces") or [])]]
    if b["key"].isdigit():
        rx = re.compile(r"(^|\D)" + re.escape(b["key"]) + r"(\D|$)")
        return any(rx.search(str(t)) for t in texts)
    return any(b["key"].lower() in str(t).lower() for t in texts)


def sheet_owners(sh: dict, blds: list[dict]) -> list[dict]:
    """The buildings a plan, elevation or section belongs to. The sheet TITLE decides when it names exactly one building;
    the reading's unit list is only the fallback, because title blocks carry the project's civic address on every sheet
    and that address can also be one of the buildings."""
    if ((sh.get("read") or {}).get("sheet_kind") or "") not in BUILDING_KINDS:
        return []
    by_title = [b for b in blds if sheet_mentions({"title": sh.get("title") or ""}, b)]
    return by_title if len(by_title) == 1 else [b for b in blds if sheet_mentions(sh, b)]


def site_tree(view: dict) -> dict:
    """The folder arranged as the site is built. Buildings come from the unit addresses first; a building that only the
    drawings name (an elevation or plan titled with its street number, same street as the others) is added with no units,
    so every building in the set has a place. Sheets that name several buildings, or are not building kinds, stay on the site."""
    blds: list[dict] = []
    for u in view.get("units") or []:
        label = u.get("label") or u.get("name") or str(u)
        b = building_of(u.get("address") or label) or {"key": label, "name": label, "street": ""}
        g = next((x for x in blds if x["key"] == b["key"]), None)
        if not g:
            g = {**b, "units": [], "levels": [], "from_drawings": False}
            blds.append(g)
        g["units"].append(label)
        for lv in u.get("levels") or []:
            name = lv.get("name") if isinstance(lv, dict) else lv
            if name and name not in g["levels"]:
                g["levels"].append(name)
    streets = {b["street"] for b in blds if b["street"]}
    kind = lambda sh: (sh.get("read") or {}).get("sheet_kind") or ""  # noqa: E731
    for sh in view.get("sheets") or []:
        if kind(sh) not in BUILDING_KINDS:
            continue
        b = building_of(sh.get("title") or "")
        if not b or b["street"] not in streets:
            continue
        g = next((x for x in blds if x["key"] == b["key"]), None)
        if g is None:
            g = {**b, "units": [], "levels": [], "from_drawings": True}
            blds.append(g)
        if g["from_drawings"] and kind(sh) in ("floor_plan", "plan"):   # floor names come from a plan, not an elevation's datum labels
            for lv in (sh.get("read") or {}).get("levels") or []:
                if lv and lv not in g["levels"]:
                    g["levels"].append(lv)
    ref = lambda sh: sh.get("sheet_number") or f"p.{sh.get('page')}"  # noqa: E731
    site: dict[str, list] = {}
    for b in blds:
        b["disciplines"] = {}
    for sh in view.get("sheets") or []:
        owners = sheet_owners(sh, blds)
        entry = {"id": sh.get("id"), "ref": ref(sh), "title": sh.get("title") or "", "kind": kind(sh),
                 "summary": ((sh.get("read") or {}).get("summary") or "")[:160]}
        if len(owners) == 1:
            owners[0]["disciplines"].setdefault(sh["discipline"], []).append(entry)
        else:
            site.setdefault(sh["discipline"], []).append(entry)
    site_plans = [e for lst in site.values() for e in lst if e["kind"] == "site_plan"]
    return {"site": {"disciplines": site, "site_plans": site_plans}, "buildings": blds}


DOCS_SYSTEM = """You help a consulting engineer's office check whether a construction project folder is complete. You are given the
project facts, the buildings on the site, every current drawing sheet (discipline, number, title, kind, one-line summary), the
names of every file in the folder, the office's occupancy-documents checklist with what already matched by file name, and the
gaps that plain rules have already listed.

Think like the engineer who will walk the site next week and then has to close the project with the city:
- The SITE PLAN is the anchor: every discipline should have one, and it should name every building.
- Each building needs, per discipline that applies, the plans and drawings the field review will be pinned on.
- Sets that are undated, superseded, or missing notes, schedules, single-line diagrams, specifications are gaps.
- Letters, forms and certificates in the folder that look like a checklist row the file-name match missed: confirm them.

Use the tools:
- record_missing(what, why, discipline, building, checklist): one gap per call. `what` is the document to ask for, `why` says
  what in the folder tells you it is missing (name the sheets or files you looked at). `discipline` is a code from the list or
  "" for the whole project; `building` is a building name from the list or "" for the whole site; `checklist` is the exact
  checklist row name when the gap is one of those rows, else "".
- record_on_file(checklist, file): a checklist row that a file in the folder plainly satisfies, by its exact file name.
- record_file(file, building, discipline): a file in the folder (exact file name) that belongs to ONE building's folder rather
  than the site: a letter, form or report that names that building only. Files that cover the whole project stay where they are.
- record_question(question, discipline, building): when the folder does not tell you whether something is missing or needed
  (a document that depends on the site, the contract or the city), do not guess: ask the engineer in one plain sentence and
  they fill in the answer. At most five questions.
- record_summary(summary): two or three plain sentences on the state of the folder, once, at the end.
Do not repeat gaps the rules already listed. Do not invent documents the project type does not need; when unsure, ask.
Never state that anything complies, is approved or is acceptable: you list what is missing, the engineer decides."""


@dataclass
class DocsContext:
    missing: list[dict] = field(default_factory=list)
    on_file: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    placed: list[dict] = field(default_factory=list)
    summary: str = ""
    errors: list[str] = field(default_factory=list)


def _reject(ctx: DocsContext, why: str) -> str:
    ctx.errors.append(why)
    return "REJECTED: " + why


def _clean(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


def make_docs_tools(ctx: DocsContext, disciplines: set[str], buildings: set[str], files: set[str], already: list[str]):
    seen = {a.lower() for a in already}

    @tool
    def record_missing(what: str, why: str, discipline: str = "", building: str = "", checklist: str = "") -> str:
        """Record one document, drawing or sheet the folder should contain but does not. discipline: a code from the list or "".
        building: a building name from the list or "" for the whole site. checklist: the exact occupancy-checklist row or ""."""
        what, why = _clean(what, 120), _clean(why, 400)
        if len(what) < 4 or len(why) < 12:
            return _reject(ctx, "what and why must both be written out")
        low = (what + " " + why).lower()
        bad = [w for w in DOC_FORBIDDEN if w in low]
        if bad:
            return _reject(ctx, f"judgement words are not allowed: {', '.join(bad)}")
        code = _clean(discipline, 8).upper()
        if code and code not in disciplines:
            return _reject(ctx, f"discipline '{discipline}' is not in this project (use one of: {', '.join(sorted(disciplines))} or \"\")")
        bld = _clean(building, 60)
        if bld and bld not in buildings:
            return _reject(ctx, f"building '{building}' is not on this site (use one of: {', '.join(sorted(buildings))} or \"\")")
        chk = _clean(checklist, 120)
        if chk and chk.lower() not in CHECKLIST_NAMES:
            return _reject(ctx, f"'{checklist}' is not a checklist row; use the exact row name or \"\"")
        if what.lower() in seen:
            return _reject(ctx, f"'{what}' is already listed")
        if len(ctx.missing) >= MAX_MISSING:
            return _reject(ctx, f"{MAX_MISSING} gaps recorded already; call record_summary")
        seen.add(what.lower())
        ctx.missing.append({"what": what, "why": why, "discipline": code, "building": bld, "checklist": CHECKLIST_NAMES.get(chk.lower(), "")})
        return "recorded"

    @tool
    def record_on_file(checklist: str, file: str) -> str:
        """Record that a file in the folder is one of the occupancy-checklist documents: the exact row name and the exact file name."""
        chk, name = _clean(checklist, 120), _clean(file, 200)
        if chk.lower() not in CHECKLIST_NAMES:
            return _reject(ctx, f"'{checklist}' is not a checklist row; use the exact row name")
        if name not in files:
            return _reject(ctx, f"'{file}' is not a file in the folder; use the exact file name")
        row = {"checklist": CHECKLIST_NAMES[chk.lower()], "file": name}
        if row not in ctx.on_file:
            ctx.on_file.append(row)
        return "recorded"

    @tool
    def record_summary(summary: str) -> str:
        """Two or three plain sentences on the state of the folder. Call once, last."""
        s = _clean(summary, 600)
        if len(s) < 20:
            return _reject(ctx, "write the summary out")
        bad = [w for w in DOC_FORBIDDEN if w in s.lower()]
        if bad:
            return _reject(ctx, f"judgement words are not allowed: {', '.join(bad)}")
        ctx.summary = s
        return "recorded"

    @tool
    def record_question(question: str, discipline: str = "", building: str = "") -> str:
        """Ask the engineer one plain question the folder cannot answer. They fill in the answer; nothing is assumed.

        Args:
            question: One sentence, ending with a question mark.
            discipline: Discipline code from the project, or "" for the whole project.
            building: Building name from the list, or "" for the whole site.
        """
        q = " ".join((question or "").split())
        if len(q) < 12 or len(q) > 240:
            return _reject(ctx, f"question too short or too long: {q[:60]!r}")
        if not q.endswith("?"):
            return _reject(ctx, f"not a question: {q[:60]!r}")
        if any(w in q.lower() for w in DOC_FORBIDDEN):
            return _reject(ctx, f"judgement word in question: {q[:60]!r}")
        if discipline and discipline not in disciplines:
            return _reject(ctx, f"unknown discipline {discipline!r}")
        if building and building not in buildings:
            return _reject(ctx, f"unknown building {building!r}")
        if any(x["question"].lower() == q.lower() for x in ctx.questions):
            return _reject(ctx, f"already asked: {q[:60]!r}")
        if len(ctx.questions) >= MAX_QUESTIONS:
            return _reject(ctx, "enough questions; record what you know")
        ctx.questions.append({"question": q, "discipline": discipline, "building": building, "answer": ""})
        return f"recorded question {len(ctx.questions)}"

    @tool
    def record_file(file: str, building: str, discipline: str = "") -> str:
        """File one document from the folder under one building, because it names that building only.

        Args:
            file: The exact file name as listed in FILES IN THE FOLDER.
            building: A building name from the list.
            discipline: Discipline code from the project, or "" to keep the file's own discipline.
        """
        f = (file or "").strip()
        if f not in files:
            return _reject(ctx, f"no such file {f[:60]!r}")
        if building not in buildings:
            return _reject(ctx, f"unknown building {building!r}")
        if discipline and discipline not in disciplines:
            return _reject(ctx, f"unknown discipline {discipline!r}")
        if any(x["file"] == f for x in ctx.placed):
            return _reject(ctx, f"already filed: {f[:60]!r}")
        ctx.placed.append({"file": f, "building": building, "discipline": discipline})
        return f"filed {f} under {building}"

    return [record_missing, record_on_file, record_file, record_question, record_summary]


def folder_text(view: dict, tree: dict, already: list[str]) -> str:
    """Everything the agent gets: text only, no images."""
    docs = view.get("documents") or []
    names = sorted({d["rel_path"].split("/")[-1] for d in docs})
    lines = [f"PROJECT: {view.get('name') or '(unnamed)'} · {', '.join(x for x in (view.get('address'), view.get('city')) if x)}",
             f"TYPE: {view.get('building_type') or 'not stated'}", f"ABOUT: {(view.get('description') or '')[:400]}", "",
             f"BUILDINGS ON THE SITE ({len(tree['buildings'])}):"]
    for b in tree["buildings"]:
        lines.append(f"- {b['name']} · units: {', '.join(b['units']) or 'none named in the project model'} · floors: {', '.join(b['levels']) or '?'}"
                     + (" · named only on the drawings" if b.get("from_drawings") else ""))
        for code, shs in b["disciplines"].items():
            lines.append(f"    {code}: " + "; ".join(f"{e['ref']} {e['title']} [{e['kind'] or '?'}]" for e in shs))
    lines += ["", "DISCIPLINES (current set, folder date, sheets read):"]
    for d in view.get("disciplines") or []:
        lines.append(f"- {d['code']} {DISCIPLINES.get(d['code'], d.get('name', ''))}: dated {d.get('dated') or 'UNDATED'}, {d.get('sheets', 0)} sheets")
    lines += ["", "SITE-WIDE SHEETS (not tied to one building):"]
    for code, shs in tree["site"]["disciplines"].items():
        for e in shs:
            lines.append(f"- {code} {e['ref']} · {e['title']} [{e['kind'] or '?'}] · {e['summary']}")
    lines += ["", f"FILES IN THE FOLDER ({len(names)} distinct names):", *[f"- {n}" for n in names]]
    matched = []
    for grp, name, rx in OCCUPANCY_DOCS:
        hit = [n for n in names if re.search(rx, n, re.I)]
        if hit:
            matched.append(f"- {name}: {', '.join(hit[:3])}")
    lines += ["", "OCCUPANCY CHECKLIST ROWS (exact names):", *[f"- {grp} / {name}" for grp, name, _ in OCCUPANCY_DOCS],
              "", "ALREADY MATCHED BY FILE NAME:", *(matched or ["- none"]),
              "", "GAPS THE RULES ALREADY LISTED (do not repeat):", *([f"- {a}" for a in already] or ["- none"])]
    return "\n".join(lines)


def record_placements(store: Store, project_id: str, placed: list[dict]) -> None:
    """Closeout's own placements become filings marked 'closeout'. A file the engineer has already moved or renamed is left
    alone: their word wins, and a review never overwrites it. A placement that matches the current one adds no row."""
    current = store.filings(project_id)
    for p in placed:
        cur = current.get(p["file"])
        if cur and cur["who"] == "engineer":
            continue
        if cur and (cur["building"], cur["discipline"]) == (p.get("building") or "", p.get("discipline") or ""):
            continue
        store.file_document(project_id, p["file"], p.get("building") or "", p.get("discipline") or "", "", who="closeout")


def file_by_engineer(store: Store, project_id: str, view: dict, file: str, building: str | None, discipline: str | None,
                     name: str | None) -> dict:
    """The engineer's move or rename, checked against the real file names, buildings and disciplines before it is kept.
    None for a field means 'leave as it is'. Raises ValueError with a plain reason."""
    files = {d["rel_path"].split("/")[-1] for d in view.get("documents") or []}
    f = (file or "").strip()
    if f not in files:
        raise ValueError(f"no such file in the project folder: {f[:80]!r}")
    tree = site_tree(view)
    buildings = {b["name"] for b in tree["buildings"]}
    disciplines = {d["code"] for d in view.get("disciplines") or []} | {sh["discipline"] for sh in view.get("sheets") or []}
    cur = store.filings(project_id).get(f) or {"building": "", "discipline": "", "name": ""}
    b = cur["building"] if building is None else building.strip()
    if b and b not in buildings:
        raise ValueError(f"unknown building {b!r}; the project has: " + (", ".join(sorted(buildings)) or "none"))
    d = cur["discipline"] if discipline is None else discipline.strip().upper()
    if d and d not in disciplines:
        raise ValueError(f"unknown discipline {d!r}; the project has: " + (", ".join(sorted(disciplines)) or "none"))
    n = cur["name"] if name is None else " ".join(name.split())[:120]
    if (b, d, n) == (cur["building"], cur["discipline"], cur["name"]):
        raise ValueError("that is where the file already is")
    return store.file_document(project_id, f, b, d, n, who="engineer")


def review_documents(store: Store, project_id: str, view: dict, already: list[str] | None = None, settings: Settings = SETTINGS,
                     model=None) -> dict:
    """One model call over the folder. Returns the validated claims; raises RuntimeError when the agent recorded nothing."""
    already = [_clean(a, 160) for a in (already or []) if _clean(a, 160)][:60]
    tree = site_tree(view)
    disciplines = {d["code"] for d in view.get("disciplines") or []} | {sh["discipline"] for sh in view.get("sheets") or []}
    buildings = {b["name"] for b in tree["buildings"]}
    files = {d["rel_path"].split("/")[-1] for d in view.get("documents") or []}
    ctx = DocsContext()
    agent = Agent(model=model or make_model(settings), tools=make_docs_tools(ctx, disciplines, buildings, files, already),
                  system_prompt=DOCS_SYSTEM, callback_handler=None)
    result = agent([{"text": folder_text(view, tree, already)},
                    {"text": "Record each gap with record_missing, confirm any checklist rows with record_on_file, then call record_summary once."}])
    if not ctx.missing and not ctx.on_file and not ctx.questions and not ctx.summary:
        raise RuntimeError("agent finished without recording anything" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
    return {"summary": ctx.summary, "missing": ctx.missing, "on_file": ctx.on_file, "buildings": [b["name"] for b in tree["buildings"]],
            "questions": ctx.questions, "placed": ctx.placed, "usage": _usage(result), "rejections": ctx.errors}
