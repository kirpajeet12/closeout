"""Assemble the review packet: JSON for the app, Markdown for humans. Every line links to a source."""
from __future__ import annotations

import json
from pathlib import Path

from .store import Store

LABEL = {
    "complete": "Evidence complete, ready for engineer review",
    "incomplete": "Evidence incomplete",
    "needs_clarification": "Needs clarification from contractor",
    "no_evidence": "No evidence received",
}


def _src(store: Store, s: dict) -> str:
    ev = store.evidence(s["evidence_id"])
    name = ev["filename"] if ev else s["evidence_id"]
    return f"{name}" + (f" p.{s['page']}" if s.get("page") else "")


def build_packet(store: Store, run_id: str) -> dict:
    run = store.run(run_id)
    items = store.deficiencies()
    findings = store.current_findings()
    status = store.item_status_for_run(run_id)
    drafts = {d["item_id"]: d for d in store.drafts_for_run(run_id)}
    by_id = {f["id"]: f for f in findings}
    decisions = {}
    for d in store.decisions():
        decisions[d["item_id"]] = d

    packet_items = []
    for d in items:
        st = status.get(d["item_id"]) or {"completeness": "no_evidence", "missing_slots": d["slots"], "filled_slots": [], "unresolved": []}
        linked = [f for f in findings if f["status"] == "matched" and f["item_id"] == d["item_id"]]
        unresolved = [dict(by_id[u["finding_id"]], unresolved_kind=u["kind"]) for u in st["unresolved"] if u["finding_id"] in by_id]
        packet_items.append({
            "item": d,
            "completeness": st["completeness"],
            "completeness_label": LABEL[st["completeness"]],
            "missing_slots": st["missing_slots"],
            "evidence": [{
                "finding_id": f["id"], "evidence_id": f["evidence_id"], "filename": (store.evidence(f["evidence_id"]) or {}).get("filename"),
                "tier": f["tier"], "slot_index": f["slot_index"], "provenance": f["provenance"], "rationale": f["rationale"],
                "flags": f["flags"], "sources": f["sources"], "observations": f["observations"],
            } for f in linked],
            "unresolved": [{
                "finding_id": f["id"], "evidence_id": f["evidence_id"], "filename": (store.evidence(f["evidence_id"]) or {}).get("filename"),
                "kind": f["unresolved_kind"], "candidates": f["candidates"], "flags": f["flags"], "rationale": f["rationale"],
                "sources": f["sources"], "tier": f.get("tier"),
            } for f in unresolved],
            "followup_draft": drafts.get(d["item_id"]),
            "decision": decisions.get(d["item_id"]),
            "history": store.item_history(d["item_id"]),
        })

    unmatched = [f for f in findings if f["status"] in ("unrelated", "conflict") or (f["status"] == "ambiguous")]
    notes = [f for f in findings if f["status"] == "note"]
    return {
        "run": run,
        "generated_from": "Closeout agent output; nothing in this packet is an engineering determination.",
        "items": packet_items,
        "unmatched": [{
            "finding_id": f["id"], "evidence_id": f["evidence_id"], "filename": (store.evidence(f["evidence_id"]) or {}).get("filename"),
            "status": f["status"], "candidates": f["candidates"], "flags": f["flags"], "rationale": f["rationale"], "sources": f["sources"],
        } for f in unmatched],
        "notes": [{
            "finding_id": f["id"], "evidence_id": f["evidence_id"], "filename": (store.evidence(f["evidence_id"]) or {}).get("filename"),
            "provenance": f["provenance"], "rationale": f["rationale"], "sources": f["sources"],
        } for f in notes],
        "evidence_index": [{k: e[k] for k in ("id", "filename", "kind", "pages", "size", "sha256", "stored_path")} | {"metadata": e["metadata"]} for e in store.all_evidence()],
    }


def packet_markdown(store: Store, packet: dict) -> str:
    out = ["# Closeout draft review packet", "",
           f"Run `{packet['run']['id']}` on model `{packet['run']['model_id']}` started {packet['run']['started_at']}.",
           "", f"> {packet['generated_from']}", ""]
    for it in packet["items"]:
        d = it["item"]
        out += [f"## {d['item_id']} — {d['description']}", f"*Location:* {d['location']}", f"**Status:** {it['completeness_label']}", ""]
        if it["evidence"]:
            out.append("**Linked evidence**")
            for e in it["evidence"]:
                srcs = ", ".join(_src(store, s) for s in e["sources"])
                slot = f"slot {e['slot_index']}" if e["slot_index"] is not None and e["slot_index"] >= 0 else "supporting"
                out.append(f"- `{srcs}` — {slot}, {e['tier']} match ({e['provenance']}). {e['rationale']}")
                for o in e["observations"]:
                    out.append(f"    - {o['text']} *({o['provenance']})*")
            out.append("")
        if it["missing_slots"]:
            out.append("**Missing**")
            for m in it["missing_slots"]:
                out.append(f"- [{m['type']}] {m['description']}")
            out.append("")
        if it["unresolved"]:
            out.append("**Unresolved**")
            for u in it["unresolved"]:
                srcs = ", ".join(_src(store, s) for s in u["sources"])
                out.append(f"- `{srcs}` — {u['kind']}; candidates {', '.join(u['candidates']) or '-'}; flags {', '.join(u['flags']) or '-'}. {u['rationale']}")
            out.append("")
        if it["followup_draft"]:
            out += ["**Follow-up draft**", "", f"*Subject:* {it['followup_draft']['subject']}", "", it["followup_draft"]["body"], ""]
    if packet["unmatched"]:
        out += ["## Files not linked to an item", ""]
        for u in packet["unmatched"]:
            out.append(f"- `{u['filename']}` — {u['status']}; candidates {', '.join(u['candidates']) or '-'}; flags {', '.join(u['flags']) or '-'}. {u['rationale']}")
        out.append("")
    return "\n".join(out)


def save_packet(store: Store, run_id: str, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    packet = build_packet(store, run_id)
    pj = out_dir / "packet.json"
    pm = out_dir / "packet.md"
    pj.write_text(json.dumps(packet, indent=2, default=str))
    pm.write_text(packet_markdown(store, packet))
    return pj, pm
