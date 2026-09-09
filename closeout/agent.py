"""The Closeout agent: one Strands agent, narrow tools, one job per invocation.

Two job kinds:
  * match  - decide what ONE evidence file supports, record findings with provenance.
  * draft  - write a follow-up request for ONE incomplete deficiency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from strands import Agent, tool

from .config import SETTINGS, make_model
from .ingest import image_bytes_for_model
from .store import Store

STATUSES = {"matched", "ambiguous", "unrelated", "conflict"}
TIERS = {"explicit", "strong", "weak"}
PROVENANCE = {"register", "contractor_claim", "file_metadata", "model_observation"}
# location_* flags carry rules (see _validate_finding); the rest are free descriptors but must come from this list.
LOCATION_FLAGS = {"location_by_reference", "location_from_sequence", "location_by_gps", "location_unconfirmed", "conflicting_reference"}
OTHER_FLAGS = {"no_label_visible", "measurement_not_visible", "low_quality", "partial_view", "date_mismatch"}

FORBIDDEN_WORDS = ("compliant", "complies", "acceptable", "meets code", "closed", "approved", "certif", "passes")

MATCH_SYSTEM = """You are the evidence clerk for a consulting engineer's deficiency closeout.
You prepare; the engineer decides. You never judge whether work is acceptable, compliant, or complete.

You will be given ONE evidence file to process against the deficiency register below.
Use inspect_evidence to look at it, then call record_finding for what it supports.
Call record_finding at least once. For a multi-page document that supports several items, call it once per item with the page numbers.

## How to decide a match
- explicit: the file itself (filename, stamp, caption, text) or the contractor's note names the item ID, or names a location AND scope that fit exactly one item.
- strong: the file's own content or metadata gives both a location and a scope that fit exactly one item.
- weak: the scope fits exactly one item but the file gives no location, so location is unconfirmed. Add flag location_unconfirmed.
  The reverse also holds: whenever you flag location_unconfirmed the tier must be weak. Weak matches never fill a slot; the engineer resolves them.
- ambiguous: the scope fits two or more items and nothing in the file distinguishes them. List all candidates. Do NOT pick one.
- unrelated: nothing in the register fits.
- conflict: the file's own reference (stamp, filename, text) contradicts the register, e.g. it names a location where no item of that scope exists. Add flag conflicting_reference and list any candidates it might have meant.

A contractor note saying "photos for item X attached" does NOT identify which file is which.
Use it only when this file's own content or metadata also points at X.
Never invent a location. If the location is not on the file or in the note about this specific file, it is unconfirmed.

## Three more ways a location can be established (use them, in this order)
1. Reference photo. Some register items carry the engineer's own photo of the deficiency from the field review
   (register provenance, an established fact). get_deficiency returns it. Compare the contractor's photo with it:
   same wall, same pipe, same fixtures beside it, same crack pattern. If you can name at least TWO specific matching
   features, the location is established: use tier strong, add flag location_by_reference, and put each feature in
   observations. "Looks similar" is not a feature; then it stays weak with location_unconfirmed.
2. Capture sequence. The file context lists other photos in this batch taken within a few minutes of this one and
   what location they carry. A neighbour shot within 3 minutes that carries a location, when this file's scope fits
   that same place and no other item there, establishes the location: tier strong, flag location_from_sequence, and
   name the neighbour file in the rationale. A neighbour with a different scope tells you nothing.
3. GPS. The file context gives this photo's position relative to each item's reference photo. GPS separates sites
   and, outdoors, sides of a building. It never separates floors, rooms or stairs, and differences under 15 m are
   noise. Use it only to support or contradict an exterior location (e.g. north elevation), never alone to pick an
   interior item. Altitude: phones record one; between photos of the same site on the same day a difference of
   3 m or more suggests different floors, less means nothing. Support only, never alone.
   If used, add flag location_by_gps alongside the flag that actually established the location.
If none of these applies, the location is unconfirmed and the tier is weak.

## Provenance, always
- register: facts from the deficiency list.
- contractor_claim: anything the contractor wrote (notes, logs, letters, captions written by them).
- file_metadata: filename, stamps burned into the image, EXIF dates, PDF page text.
- model_observation: what you see in the image. Always uncertain. Describe, do not conclude.
Set the finding's provenance to the STRONGEST kind of evidence that ties the file to the item.

## Slots
Each item lists evidence slots by index. Say which slot this file fills (slot_index). A photo cannot fill a report/letter slot.
A file that supports an item without clearly filling a slot (e.g. a daily log entry) is still recorded, with slot_index -1.

## Language rules
Do not write "compliant", "acceptable", "meets code", "closed", "approved", "certified" or "passes".
Write what is present, what is missing, and what could not be established.

## The project, as read from its drawings (document + model_observation provenance)
Use it to resolve where a location is: which building, unit and level a room belongs to, and which sheet shows it.
Room names alone (Bath, Mech, Kitchen) repeat in every unit; a room name never establishes a location by itself.
{project}

## Deficiency register
{register}

## Contractor notes in this batch (contractor_claim provenance)
{notes}

## Other files in this batch (names only)
{filenames}
"""

DRAFT_SYSTEM = """You draft follow-up requests from a consulting engineer's office to a contractor about deficiency evidence.
The engineer will edit and send it; you only draft. Be specific: name the item ID, the location, exactly what is missing or unclear, and what would resolve it.
Ask only for what the brief lists as missing or unresolved. Do not invent requests.
If a received file carries a flag (for example low_quality or measurement_not_visible), you may mention it under a separate
"optional" line, quoting the flag, but the main request is the missing evidence.
Do not say work is acceptable, compliant, approved or closed. Do not thank them for completing work you cannot verify.
Plain, courteous, short. No greeting name, no signature name; sign as "Voltas Engineering".
Call save_draft exactly once with a subject line and the body.
"""


@dataclass
class JobContext:
    store: Store
    run_id: str
    job_id: str
    recorded: list[str] = field(default_factory=list)
    saved_draft: str | None = None
    errors: list[str] = field(default_factory=list)
    neighbours: list[str] = field(default_factory=list)   # filenames captured within 3 min of this job's photo


def _validate_finding(ctx: JobContext, evidence_id: str, item_id, status, tier, slot_index, candidates, flags, rationale, sources, provenance, observations) -> str | None:
    if status not in STATUSES:
        return f"status must be one of {sorted(STATUSES)}"
    if provenance not in PROVENANCE:
        return f"provenance must be one of {sorted(PROVENANCE)}"
    for fl in flags or []:
        if fl not in LOCATION_FLAGS | OTHER_FLAGS:
            return f"unknown flag '{fl}'; use only {sorted(LOCATION_FLAGS | OTHER_FLAGS)} (a location flag must be spelled exactly)"
    if status == "matched":
        if not item_id or not ctx.store.deficiency(item_id):
            return "matched findings need a valid item_id from the register"
        if tier not in TIERS:
            return f"matched findings need tier in {sorted(TIERS)}"
        d = ctx.store.deficiency(item_id)
        if slot_index is not None and slot_index >= 0 and slot_index >= len(d["slots"]):
            return f"{item_id} has slots 0..{len(d['slots'])-1}"
        if "location_unconfirmed" in (flags or []) and tier != "weak":
            return "a finding flagged location_unconfirmed must use tier 'weak' (location is part of what explicit/strong mean)"
        if "location_by_reference" in (flags or []):
            if not d.get("reference_photo"):
                return f"{item_id} has no reference photo; location_by_reference cannot apply"
            if len([o for o in (observations or []) if o.get("text")]) < 2:
                return "location_by_reference needs at least two specific matching features listed in observations"
        if "location_from_sequence" in (flags or []) and not ctx.neighbours:
            return "location_from_sequence cannot apply: no other photo was taken within 3 minutes of this one"
        if slot_index is not None and slot_index >= 0:
            slot_type = d["slots"][slot_index]["type"]
            ev = ctx.store.evidence(evidence_id)
            if ev and ev["kind"] == "image" and slot_type != "photo":
                return f"slot {slot_index} of {item_id} is a {slot_type} slot; a photo cannot fill it (use slot_index -1 for supporting)"
            if ev and ev["kind"] == "pdf" and slot_type == "photo":
                return f"slot {slot_index} of {item_id} is a photo slot; a PDF cannot fill it (use slot_index -1 for supporting)"
    else:
        if item_id:
            return f"status {status} must not carry an item_id; use candidates instead"
    for c in candidates or []:
        if not ctx.store.deficiency(c):
            return f"candidate {c} is not in the register"
    if not ctx.store.evidence(evidence_id):
        return f"unknown evidence_id {evidence_id}"
    lowered = (rationale + " " + " ".join(o.get("text", "") for o in observations or [])).lower()
    for w in FORBIDDEN_WORDS:
        if w in lowered:
            return f"do not use the word '{w}'; describe what is present or missing instead"
    for o in observations or []:
        if o.get("provenance") not in PROVENANCE:
            return "every observation needs a provenance"
    return None


def make_match_tools(ctx: JobContext, evidence_id: str):
    @tool
    def inspect_evidence(evidence_id: str) -> dict:
        """Open one evidence file. Returns its metadata and extracted text, plus the image itself for photos.

        Args:
            evidence_id: the evidence id to open.
        """
        ev = ctx.store.evidence(evidence_id)
        if not ev:
            return {"status": "error", "content": [{"text": f"unknown evidence_id {evidence_id}"}]}
        header = {
            "evidence_id": ev["id"], "filename": ev["filename"], "kind": ev["kind"], "pages": ev["pages"],
            "metadata": ev["metadata"],
        }
        content = [{"text": "FILE (file_metadata provenance): " + json.dumps(header)}]
        if ev["kind"] == "image":
            data, fmt = image_bytes_for_model(Path(ev["stored_path"]))
            content.append({"image": {"format": fmt, "source": {"bytes": data}}})
        else:
            for i, page in enumerate(ev["text"], start=1):
                content.append({"text": f"--- page {i} ---\n{page or '(no extractable text)'}"})
        return {"status": "success", "content": content}

    @tool
    def get_deficiency(item_id: str) -> dict:
        """Return the full register entry for one deficiency, including its evidence slots.

        Args:
            item_id: e.g. D-03
        """
        d = ctx.store.deficiency(item_id)
        if not d:
            return {"status": "error", "content": [{"text": f"no such item {item_id}"}]}
        ref = d.get("reference_photo")
        entry = {k: d[k] for k in ("item_id", "location", "description", "slots", "review_date", "discipline")}
        content = [{"text": "REGISTER ENTRY (register provenance): " + json.dumps(entry)}]
        if ref and Path(ref).is_file():
            content.append({"text": f"REFERENCE PHOTO for {item_id}: the engineer's own photo of this deficiency taken at the field review "
                                    f"(register provenance). Metadata: {json.dumps(d.get('ref_meta') or {})}"})
            data, fmt = image_bytes_for_model(Path(ref))
            content.append({"image": {"format": fmt, "source": {"bytes": data}}})
        else:
            content.append({"text": f"{item_id} has no reference photo."})
        return {"status": "success", "content": content}

    @tool
    def record_finding(
        item_id: str | None,
        status: str,
        rationale: str,
        provenance: str,
        tier: str | None = None,
        slot_index: int | None = None,
        candidates: list[str] | None = None,
        flags: list[str] | None = None,
        pages: list[int] | None = None,
        observations: list[dict] | None = None,
    ) -> str:
        """Record what the current evidence file supports. Call at least once per file.

        Args:
            item_id: register item the file supports; null unless status is "matched".
            status: matched | ambiguous | unrelated | conflict
            rationale: one or two sentences on why, naming the specific signals used.
            provenance: register | contractor_claim | file_metadata | model_observation (strongest signal tying file to item)
            tier: explicit | strong | weak (required when matched)
            slot_index: which evidence slot of the item this fills; -1 if it supports the item without filling a slot.
            candidates: item ids considered plausible (required for ambiguous and conflict)
            flags: only from location_by_reference, location_from_sequence, location_by_gps, location_unconfirmed, conflicting_reference, no_label_visible, measurement_not_visible, low_quality, partial_view, date_mismatch
            pages: for PDFs, the page numbers that support this finding (1-based)
            observations: list of {"text": ..., "provenance": ...} describing what is present or missing
        """
        err = _validate_finding(ctx, evidence_id, item_id, status, tier, slot_index, candidates, flags, rationale, pages, provenance, observations)
        if err:
            ctx.errors.append(err)
            return f"REJECTED: {err}. Fix and call record_finding again."
        ev = ctx.store.evidence(evidence_id)
        sources = [{"evidence_id": evidence_id, "page": p} for p in (pages or [])] or [{"evidence_id": evidence_id, "page": None if ev["kind"] != "pdf" else 1}]
        fid = ctx.store.add_finding(
            run_id=ctx.run_id, job_id=ctx.job_id, evidence_id=evidence_id, item_id=item_id if status == "matched" else None,
            status=status, tier=tier if status == "matched" else None, slot_index=slot_index if status == "matched" else None,
            candidates=candidates or ([item_id] if item_id else []), flags=flags or [], rationale=rationale,
            sources=sources, provenance=provenance, observations=observations or [],
        )
        ctx.recorded.append(fid)
        return f"recorded {fid}"

    return [inspect_evidence, get_deficiency, record_finding]


def make_draft_tools(ctx: JobContext, item_id: str):
    @tool
    def save_draft(subject: str, body: str) -> str:
        """Save the follow-up draft for the current item. Call exactly once.

        Args:
            subject: email subject line
            body: the message body, plain text
        """
        lowered = (subject + " " + body).lower()
        for w in FORBIDDEN_WORDS:
            if w in lowered:
                ctx.errors.append(w)
                return f"REJECTED: do not use the word '{w}'. Rewrite and call save_draft again."
        did = ctx.store.upsert_draft(ctx.run_id, item_id, subject.strip(), body.strip())
        ctx.saved_draft = did
        return f"saved {did}"

    return [save_draft]


def _usage(result) -> dict:
    try:
        u = dict(result.metrics.accumulated_usage)
        return {k: int(v) for k, v in u.items() if isinstance(v, (int, float))}
    except Exception:
        return {}


def run_match_job(store: Store, run_id: str, job_id: str, evidence_id: str, register_text: str, notes_text: str,
                  filenames: list[str], model=None, file_context: str = "", neighbours: list[str] | None = None,
                  project_text: str = "") -> dict:
    """Run the agent on one evidence file. Raises on failure so the pipeline can mark the job failed."""
    ctx = JobContext(store=store, run_id=run_id, job_id=job_id, neighbours=list(neighbours or []))
    ev = store.evidence(evidence_id)
    system = MATCH_SYSTEM.format(register=register_text, notes=notes_text or "(none)", filenames="\n".join(filenames),
                                 project=project_text or "(no project drawings imported)")
    agent = Agent(model=model or make_model(SETTINGS), tools=make_match_tools(ctx, evidence_id),
                  system_prompt=system, callback_handler=None)
    result = agent(
        f"Process evidence_id {evidence_id} (filename: {ev['filename']}, kind: {ev['kind']}). "
        f"Inspect it, then record your finding(s). Finish with one line summarising what you recorded."
        + (f"\n\nFILE CONTEXT (file_metadata provenance):\n{file_context}" if file_context else "")
    )
    if not ctx.recorded:
        raise RuntimeError("agent finished without recording a finding" + (f"; last rejection: {ctx.errors[-1]}" if ctx.errors else ""))
    return {"findings": ctx.recorded, "usage": _usage(result), "summary": str(result).strip()[:500]}


def run_draft_job(store: Store, run_id: str, job_id: str, item_id: str, item_brief: str, model=None) -> dict:
    ctx = JobContext(store=store, run_id=run_id, job_id=job_id)
    agent = Agent(model=model or make_model(SETTINGS), tools=make_draft_tools(ctx, item_id),
                  system_prompt=DRAFT_SYSTEM, callback_handler=None)
    result = agent(f"Draft the follow-up for this item.\n\n{item_brief}")
    if not ctx.saved_draft:
        raise RuntimeError("agent finished without saving a draft")
    return {"draft_id": ctx.saved_draft, "usage": _usage(result)}
