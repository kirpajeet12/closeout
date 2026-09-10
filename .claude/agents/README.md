# Closeout design team

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
