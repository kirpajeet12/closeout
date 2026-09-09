# Closeout

Turns a contractor's scattered photos and documents into an organized, source-linked evidence
packet that an engineer can review. Built for the AWS **Agents for Humans Hackathon**
(Professional Agents track) on the [Strands Agents SDK](https://strandsagents.com).

**What it does**

1. Import the deficiency register (CSV) from a field review.
2. Upload the contractor's evidence batch as the folder they sent it: JPEG/PNG/HEIC photos, text-based PDFs, text notes. Sub-folders are walked and remembered.
3. One Strands agent, one narrow job at a time, reads each file and records what it supports,
   with a match tier (explicit / strong / weak / ambiguous / unrelated / conflict), the evidence
   slot it fills, and the provenance of every claim.
4. Deterministic code (not the model) decides per-item completeness from the register's slots.
5. Follow-up requests are drafted for anything incomplete. They stay drafts.
6. A packet is saved that links every line to an exact file, and a page number for PDFs.
7. Uploading the same file again never creates a duplicate; reprocessing keeps history.

**What it never does**

- It never says work is acceptable, compliant, approved or closed. Those words are rejected at the tool boundary.
- It never picks one item when the evidence fits two; the file stays *ambiguous* for the engineer.
- It never infers a location that is not on the file or in the contractor's note about that file.
- It never presents hardcoded sample answers as agent output. Every packet comes from a real run.

## Quick start

```bash
python3.13 -m venv .venv && .venv/bin/pip3 install -r requirements.txt
cp .env.example .env          # Bedrock by default; needs AWS credentials in your environment
.venv/bin/python -m closeout.cli run --register samples/register/register.csv --batch samples/evidence/batch-01
```

Output lands in `data/runs/<run_id>/packet.md` and `packet.json`. `data/` is gitignored.

### Evidence Desk (web UI)

```bash
.venv/bin/uvicorn closeout.api:app --port 8765
```

Open <http://localhost:8765/>. Drop the register folder (`samples/register/`, CSV plus its
`photos/`) on the left of the feed column, then drop an evidence folder (`samples/evidence/batch-01/`).
The run streams job by job over server-sent events; when the packet is ready the ledger colours by
completeness, each item shows its evidence cards with source links, the follow-up draft is editable,
and Accept / Hold / Reject records the engineer's call in the packet. A failed run shows a
*Retry failed jobs* button that re-runs only the failed jobs. The API is documented at `/docs`.
Nothing on the desk sends email; the draft is copied by hand.

Tests:

```bash
.venv/bin/python -m pytest tests -q
```

`tests/test_batch01.py` checks the latest real run against `samples/expected/batch-01.json`,
a checklist written *before* the agent was first run. `tests/test_completeness.py` covers the
deterministic rules.

## Register CSV format

See [`samples/register/README.md`](samples/register/README.md). The `evidence_required` column
declares slots (`photo: ...; report: ...`); each slot is checked independently.

## How the agent is used

`closeout/agent.py` builds one `strands.Agent` per job with three tools:

| tool | purpose |
|---|---|
| `inspect_evidence` | opens the one file for this job; returns the downscaled image or the per-page PDF text |
| `get_deficiency` | returns one register entry with its slots |
| `record_finding` | writes a finding; the tool validates tier, slot, flags, provenance and language and rejects the call with a reason the agent can act on |

A draft job has a single `save_draft` tool with the same language guard. The pipeline
(`closeout/pipeline.py`) creates one job per evidence file and one per incomplete item, records
each job's status, attempts and error in SQLite, and can retry failed jobs:

```bash
.venv/bin/python -m closeout.cli retry <run_id>
.venv/bin/python -m closeout.cli status
```

## How a photo's location is established

A photo only fills a slot when its location is established. The agent never guesses one. Signals,
in the order it must use them:

1. **Text in the file** — a burned-in stamp, a filename like `IMG_2201_L2_corridor_firestop.jpg`,
   or a line in the contractor's note about that specific file (`file_metadata` / `contractor_claim`).
2. **The engineer's reference photo** — the register's optional `reference_photo` column points at the
   photo the reviewer took when the deficiency was written. The agent sees it next to the contractor's
   photo and may call the location established only if it names at least two fixed features that
   appear in both (flag `location_by_reference`; the validator rejects fewer than two).
3. **Capture sequence** — a photo taken within three minutes of a photo whose location is already
   established (flag `location_from_sequence`).
4. **GPS and altitude** — read from EXIF, compared against the reference photo's GPS. The pipeline
   reports distance, bearing and altitude difference and labels them against the phone's own accuracy
   figure ("within noise", "about a floor apart"). GPS can support or contradict a location; it can
   never establish one on its own, because indoor GPS drifts by tens of metres and barometric altitude
   drifts by metres between days (flag `location_by_gps`, support only).

If none of these applies the finding is `weak` with `location_unconfirmed`, the slot stays open, and
the follow-up asks where the photo was taken. Reviewer decisions can resolve it later.

## Provenance

Every finding and every observation carries one of:

- `register` — a fact from the deficiency list
- `contractor_claim` — anything the contractor wrote
- `file_metadata` — filename, burned-in stamp, EXIF, PDF text
- `model_observation` — what the model saw in a photo; always uncertain

## Sample data

Everything in `samples/` is synthetic. The photos in `samples/evidence/batch-01` are placeholders
generated by `scripts/make_placeholder_evidence.py` (drawn shapes plus a caption) so the pipeline
can be exercised before real, authorized photos are added with `scripts/adopt_inbox_photos.py`.
Company names, people and the project are invented.

## Disclosure

This repository was created for the hackathon submission period. The author runs an engineering
consultancy and has built unrelated field-review software before; no code from those projects is
used here. The Strands Agents SDK and the Python packages in `requirements.txt` are third-party.

## Status

Milestone 1 (pipeline on the sample batch, checklist-tested) and Milestone 2 (FastAPI + Evidence
Desk: folder drops, live SSE feed, evidence cards, editable drafts, decisions, retry) are done.
Next: packet download polish, README diagram, demo video.

License: MIT.
