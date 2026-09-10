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
| `closeout-demo-director` | beat sheet for the five-minute Devpost film | `docs/demo/BEATS.md` |
| `closeout-demo-narrator` | narration, card lines, voice (ElevenLabs), music choice + licence | `docs/demo/SCRIPT.md`, `demo-video/audio/`, `demo-video/MUSIC.md` |
| `closeout-demo-cameraman` | records each beat from the real app with Playwright | `demo-video/capture.py`, `demo-video/footage/` |
| `closeout-demo-editor` | cards, lower-thirds, device frame, ducked music, loudness, export | `demo-video/assemble.py`, `demo-video/output/` |

Order: director → narrator + cameraman (in parallel) → editor → `closeout-design-director`
as the gate on the cut. Footage, audio and output are gitignored; the scripts are committed.
Nothing is uploaded or submitted by an agent; the lead prepares, the owner posts.
