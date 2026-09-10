# What the app already does (as of 2026-09-10)

Closeout is the standalone app in this repo: a FastAPI server, one HTML file for the whole interface, SQLite, and Strands agents on Amazon Bedrock. The rule everywhere: **the agent proposes, plain code checks, the engineer decides.** Nothing is sent to anyone without him.

Each row is one module from the idea (`00-prompt.md`) and how far it exists today.

| Idea module | Today | Where |
|---|---|---|
| 1. Project creation + drawing ingestion | Built. A project folder is scanned; drawing sets are split into sheets; each sheet is read once by the agent (title, kind, building, levels, what it shows). Readings are stored, never re-bought. | `closeout/project.py`, `closeout/service.py`, sheet readings in SQLite |
| 2. AI field-review preparation | Not built. Ideas confirmed and parked: a readiness check per discipline before a review; checklists per review type seeded from the office's existing lists. Building-code text: not started (see plan, part D). | — |
| 3. Drawing-first site interface | Built. Field review tab → building → unit → floor → that floor's plan opens zoomed, other sheets of the building underneath. Pan, pinch, tap to pin. Where a discipline has no floor plans, the architectural plans stand in. | `web/index.html` viewer, `closeout/plans.py` (per-floor boxes on a sheet, one agent call per sheet, fixable by hand) |
| 4. Indoor position tracking | Not built. Only what a phone gives for free: GPS on https, used to pre-select the unit from the nearest earlier item. Nothing indoors. | — |
| 5. Photo → automatic deficiency pin | Half built. Photo first, then "where are you?", then the agent picks the sheet and writes the record. The **spot on the sheet is still tapped by the engineer**; the agent does not propose the pin yet, so no predicted-versus-corrected pairs are stored yet. | `closeout/review.py` (`locate_field_note`, `record_field_note`) |
| 6. Voice + image AI | Image: built (photo + sheet close-up + note → structured record with fixed slots, forbidden-words check, numbered items EL-01, AR-01 never reused). Voice: not built. | `closeout/review.py` |
| 7. Continuous video / 360 capture | Not built, not planned. | — |
| 8. Digital twin | Not built, not planned. | — |
| 9. Contractor remediation loop | Built for the desk side: contractor evidence is matched to items by the agent, the engineer accepts or rejects each match, decisions are recorded. Finishing a field review freezes a contractor package and the agent drafts the covering message. Sending, inbox reading and timed follow-ups: not built. | `closeout/api.py` (decisions, packages, drafts), Evidence and Messages tabs |
| 10. Closed-loop record | Built: every item carries its sheet, pin, photo, wording, status history, and the agent run that produced it (model, cost). | `closeout/store.py` (`runs`, `reviews`, findings, decisions) |
| 11. Data moat | Not started. The pieces that would feed it (photo, chosen sheet, tapped pin, edited wording) are already stored per item. | — |
| 12–13. Philosophy and constraints | Already how the app works: human-assisted, engineer signs off, deterministic checks around every agent call, cost per call recorded. | everywhere |

## Numbers that matter

| Thing | Value |
|---|---|
| Real project on file | one townhouse site, three buildings, AR + EL + PL sheets |
| Agent cost so far | about $6 of the $50 hackathon credit, most of it the one-time sheet readings |
| One field item (photo → record) | a few cents |
| Tests | 56 pass locally, 55 more need Bedrock |
