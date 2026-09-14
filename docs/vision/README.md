# Closeout — the bigger idea

This folder is where the full Field Review platform idea lives, separate from the hackathon build.
It exists so a new chat can pick the idea up from a few short files instead of a 14,000-character paste.

## Read in this order

| File | What it is | When to read it |
|---|---|---|
| `02-plan.md` | The plan: what to build now, what is hard, what is R&D, what to leave alone, and the order | **Start here.** One read gives the whole picture |
| `01-what-exists.md` | What the app already does today, mapped to the idea's modules | Before proposing any feature, so nothing gets rebuilt |
| `00-prompt.md` | The original idea, verbatim | Only when a detail of the original wording matters |
| `decisions/` | One short file per decision that was made (why, and what was rejected) | Before reopening a settled question |
| `research/` | Notes on technology choices (indoor positioning, 360 capture, code sources) | When a decision in `02-plan.md` needs evidence |
| `specs/` | One file per module once it is actually scheduled to be built | When building |

## Rules for this folder

- The hackathon build (deadline Mon 2026-09-14, 5 pm PT) does not take anything from this folder. Nothing new goes into the app before then.
- Everything here is written for a reader who is not a programmer. Plain words, short files.
- No project folder names, no people's names or phone numbers, ever. Unit letters only.
- A decision goes in `decisions/` the day it is made, as `YYYY-MM-DD-short-name.md`, using `decisions/TEMPLATE.md`.
- Keep files small. A new chat should be able to read `02-plan.md` and `01-what-exists.md` for well under 5,000 tokens.
