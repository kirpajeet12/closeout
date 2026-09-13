# Closeout design and demo teams

Local agent definitions for Claude Code (`.claude/agents/`). Invoke from the
repo with the Agent tool or by name in chat, e.g. "run closeout-ux-researcher
on the Documents tab".

| Agent | Job | Edits? |
|---|---|---|
| `closeout-ux-researcher` | walks a screen as the field reviewer / engineer / contractor, ranks what hurts | no |
| `closeout-copywriter` | every word on screen and in printed or mailed documents | copy in `web/index.html` only |
| `closeout-interaction-designer` | layout, hierarchy, phone-first behaviour | HTML/CSS/JS in `web/index.html` |
| `closeout-design-director` | final PASS/FAIL gate; rejects generic or leaky UI | no |

Order for a screen: researcher → copywriter + interaction designer → director.
The lead (the main Claude session) commits; agents never commit and never
call the paid document-reading route or save test data.

## Demo film team

| Agent | Job | Writes |
|---|---|---|
| `closeout-demo-director` | beat order and every caption, to the "real lines" standard | captions in `demo-video/assemble.py`, `demo-video/SCRIPT.md` |
| `closeout-film-critic` | read-only gate: rejects cringe, slogans, filler, anything not on screen | nothing |
| `closeout-demo-cameraman` | records each beat from the real app with Playwright | `demo-video/capture*.py`, stills |
| `closeout-demo-editor` | layout, cameras, music, render, share copy | `demo-video/assemble.py`, `demo-video/output/` |

Order: director → critic (loop until PASS) → editor → critic on the preview stills.
The film has no voice; the narrator was retired. Footage, audio and output are gitignored; the scripts
are committed. Nothing is uploaded or submitted by an agent; the lead prepares, the owner posts.
The critic also gates the README and Devpost text.
