# The plan (first draft, 2026-09-10 night — to be argued over together)

Short version: **most of the idea is already the app.** The pieces that are not yet built fall into two very different groups: office workflow (cheap, certain, worth doing next) and indoor spatial tracking (expensive, uncertain, and not what a contractor needs from a deficiency record). The plan is to finish the first group on real reviews, collect real data while doing it, and only then decide about the second.

## 0. Guardrail

Until the hackathon is submitted (Mon 2026-09-14, 5 pm PT) nothing from this file goes into the app. Thu: real walk + credit form. Fri: AgentCore deploy. Sat: README, diagram, Devpost draft. Sun: video. Mon: buffer, he submits.

## A. What can be built now (weeks, not months, on what exists)

1. **Use it on real reviews.** Five real field reviews at the townhouse site before any new feature. Every gap found on site outranks anything below.
2. **Sending and follow-ups.** Send the contractor package by email from the app, read replies into the evidence loop, remind after a set number of days. All the drafting already exists; this is plumbing.
3. **Readiness check** before a review: per discipline, which sheets are current, which floors are boxed, which items from the last review are still open, so the engineer walks in knowing what to look at.
4. **Checklists per review type**, seeded from the office's existing lists, ticked on the phone, unticked items become the "not seen" list in the package.
5. **Agent proposes the pin.** Given the photo, the chosen floor, and the sheet reading, the agent proposes a spot; the engineer drags it. Store both the proposed and the final spot on every item. This is the single most valuable data the platform can collect and it costs nothing extra to store.
6. **Voice note → record.** Transcribe on the phone or with Amazon Transcribe, feed the text through the same record path as the typed note. Same checks, same slots.
7. **"What the drawing calls for here"** line in the record, taken from the sheet reading and the tapped spot, so the contractor sees the requirement next to the observation.
8. **Audit trail as a feature.** Every agent run is already stored with model and cost; show it per item, export it with the package.

## B. Hard but achievable (a quarter each, after A has run on real work)

1. **Room-level location from a photo** (not wall-level). Match what is in the photo (panel, fixture, stair, window layout) against the sheet reading and the floor's plan to name the room. Measure against the stored proposed-versus-final pins from A.5 before promising anything. Expect it to be right about the room most of the time and about the wall rarely.
2. **QR or printed anchors per unit door.** A sticker at each unit entrance that the phone scans sets building, unit and floor in one gesture. Cheap, robust, no drift, works in a wood-frame site with no power and no network. This replaces most of what "indoor positioning" would do for deficiency recording.
3. **Per-room 360 photo** attached to the room, not aligned to the plan. Gives the office the context around a deficiency without any SLAM. A consumer 360 camera and the existing photo path.
4. **Multi-discipline overlay**: show the electrical layout over the architectural plan for the same floor when the electrical set has no floor plan of its own.
5. **Training-data export.** A nightly job that writes every item as one record: photo, sheet, floor, proposed pin, final pin, agent wording, final wording, contractor evidence, decision. Nothing is trained yet; the export is what makes training possible later.

## C. Requires R&D (do not schedule; write a research note first)

1. **Continuous indoor positioning by sensor fusion** (ARKit visual-inertial odometry, LiDAR, SLAM). Drift of a few percent of distance walked, relocalisation needs a stable environment, and a construction site changes daily. OpenSpace and Cupix solved a different problem (progress capture with a 360 camera on a hard hat) with years of work and per-site setup. For a deficiency record, room-level is enough, and B.2 gives that for the price of a sticker.
2. **Video → floor-plan alignment.** Same problem as C.1 with more data. Only worth it if the office starts selling progress capture as a product, which is a different business.
3. **Custom vision models** for deficiency recognition. There is no labelled data yet. A.5 and B.5 create it. Revisit when there are a few thousand corrected items, not before.
4. **Building-code references with retrieval.** The technical part is easy; the hard parts are the licence to reproduce code text and the liability of an agent citing the wrong clause. Needs a decision with the P.Eng, not a prototype. Until then the record says what the drawing calls for (A.7), which the office owns.

## D. Should not be built yet

1. **Digital twin / site simulation.** No BIM exists for wood-frame townhouses; there is nothing to twin. A floor plan with pins is the twin this market can use.
2. **Beacons (BLE / UWB) infrastructure.** Hardware to install and maintain on every site for a gain that a QR sticker gives for free.
3. **Proprietary models before data.** See C.3.
4. **A native app.** The web app runs on the phone today; the only native feature that matters (indoor positioning) is in C. Wrap it in a native shell only when something native is actually needed.
5. **Any feature before the hackathon is submitted.**

## Weak assumptions in the idea, challenged

- *"The phone knows approximately where it is indoors."* It does not, on a construction site, without infrastructure. Everything downstream of that sentence needs a cheaper anchor (B.2).
- *"A pin must be wall-accurate."* The contractor needs the room, the photo and the wording. The app already proves that; three days of real use will show whether anyone asks for more.
- *"Store corrections and a moat appears."* One office does a limited number of reviews a year. The moat is real only across many offices; the schema should be designed for that from day one (A.5, B.5), but the moat is a five-year story, not a feature.
- *"Compete with OpenSpace / Cupix / Matterport."* They sell progress documentation to general contractors. This sells deficiency closeout to engineers. Different buyer, different job; do not borrow their roadmap.
- *"The AI drafts the deficiency."* Already true, and the deterministic checks around it are the part that makes it trustworthy. Keep that shape for every new agent call.

## Phases (the idea asked for five)

| Phase | Content | Done when |
|---|---|---|
| 1 | Hackathon build as is | Submitted 2026-09-14 |
| 2 | A.1–A.4: real use, sending, readiness, checklists | Five real reviews closed out through the app |
| 3 | A.5–A.8: proposed pin, voice, drawing requirement, audit | Proposed-versus-final pins stored on every item for a month |
| 4 | B.1–B.5: room-level location, QR anchors, 360 per room, overlays, export | Room named correctly on most items, measured |
| 5 | C decisions, with data in hand | A written research note per item, then decide |

## 90 days (Sept 11 – Dec 10)

- Sept 11–14: hackathon.
- Sept 15–30: the app on real reviews; fix what the site finds. No features.
- October: sending, follow-ups, readiness check, checklists.
- November: agent-proposed pin with both spots stored; voice notes; the drawing-requirement line.
- Early December: read the numbers; pick one B item; write the first two research notes.

## Build or buy

| Need | Choice | Why |
|---|---|---|
| Agent runtime | Strands on Bedrock (have) | Already proven, cost tracked per call |
| Transcription | Amazon Transcribe or on-device | Not a differentiator |
| Email in and out | Amazon SES (used on other projects) | Known path |
| 360 capture | Consumer camera, plain photo path | Any alignment work is C |
| Indoor positioning | Do not buy; QR anchors | See C.1 |
| Building-code text | Do not scrape; decide with the P.Eng | Licence and liability |

## What the data model still needs (for `specs/` later)

Rooms per floor; anchors (QR → building/unit/floor); a proposed pin next to the final pin on every item; a "requirement at this spot" text with its source sheet; contractor threads and reminders; a training export. Everything else in the nineteen-table list in the original prompt already has an equivalent in `closeout/store.py` or is a C/D item.

## Legal and liability, in one paragraph

The engineer signs the record; the agent never does. Every agent contribution is stored with the model and the run, and the engineer's edit is stored beside it. Code references are the engineer's, not the agent's, until the P.Eng decides otherwise. Photos and drawings stay in the office's own account. No person's name or phone number is ever in a file name, a commit, or a message signature.

## Open questions for tomorrow

1. Does he agree that room-level location is enough for the record, or does the office need wall-level for some discipline?
2. Which office lists become the first checklists?
3. Who is the first contractor to receive a package from the app rather than by hand?
