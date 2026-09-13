# Closeout demo film

About 2 min 27 s, 1920×1080 at 30 fps. Keynote style: music and on-screen captions, no voice.
Every screen is a real Closeout screen from the fictional **Cedar Row Townhomes** project. No real
project, address or person appears. Everything Closeout writes on screen is its real output from the
filming run; nothing is typed in for it.

## How it is made

| Step | Command | Cost |
|---|---|---|
| 1. Film every screen, with the AI steps | `demo-video/film-ai.sh` | about $0.30 (the last run: $0.28) |
| 1a. Rehearse the AI filming for free | `demo-video/serve.sh && python3 demo-video/capture.py --dry` | free (AI requests are answered with a fake error) |
| 1b. Film the cut without AI | `demo-video/serve.sh && python3 demo-video/capture.py` | free |
| 2. Music (once; skipped if `audio/score.mp3` exists) | `python3 demo-video/gen_music.py` | ElevenLabs music |
| 3. Contact-sheet check | `python3 demo-video/assemble.py --preview` | free |
| 4. Render | `python3 demo-video/assemble.py` → `output/closeout-demo.mp4` (`--free` → `closeout-demo-free.mp4`) | free |

`film-ai.sh` resets the project, serves it with the real AWS credentials, films, prints what it cost
(`demo-video/cost.py`), then puts the server back in free mode. `capture.py --paid` lets exactly one
write-up, one contractor upload and one question through; every other paid request is blocked and
stops the script. Media (`audio/`, `output/`, `data/`, `project/`) is not committed.

## Beats

| # | Sec | Screen | Caption |
|---|---|---|---|
| 1 | 6.5 | Site photo | A site walk ends with a list. / Then the chasing starts. |
| 2 | 6 | Title card | Closeout / From the walk to the last item closed. |
| 3 | 8 | Projects → drawings | One project. Every drawing, filed. |
| 4 | 6 | Phone: field tab | On site, open the field tab. Start the review. |
| 5 | 6.5 | Photo → where | Take a photo. Say which unit and floor. |
| 6 | 5.5 | Plan | The plan opens on your floor. |
| 7 | 6.5 | Tap on plan, "Write it up for me" | Tap the spot. Ask Closeout to write it up. |
| 8 | 9 | Write-up → form | It reads the photo and the drawing, and writes it up. / You check it. Nothing is saved until you save. |
| 9 | 5 | Typed form | Or type it yourself. |
| 10 | 6.5 | Saved items | AR-01. AR-02. AR-03. Numbered, pinned to the plan, filed by unit. |
| 11 | 7 | Finish | Finish. It asks which units you walked. |
| 12 | 6 | Drafting → finished | The list is frozen. Closeout drafts the message to the contractor. |
| 13 | 6 | Deficiencies | Every item, where it is, and what it needs. |
| 14 | 9.5 | Report scroll | The report is ready. Photos, plan pins, what closes each item. |
| 15 | 8 | Drafted message | The message to the contractor is drafted. Nothing is sent until you send it. |
| 16 | 7 | Contractor link | The contractor gets one link. No account. Just their items. |
| 17 | 8 | Contractor upload | They send a photo. Closeout files it to the right item. |
| 18 | 9 | Item with the filed photo | It says what it could not confirm. |
| 19 | 7 | Accept | The engineer decides what closes. |
| 20 | 9 | Ask Closeout | Ask Closeout anything about the project. |
| 21 | 7 | Office + phone | The office and the contractor see the same list. |
| 22 | 11 | End card | Closeout / Walk it. Send it. Close it. / Built with Strands Agents SDK on Amazon Bedrock |

Scenes overlap by 0.6 s. The score is 152 s, so the film must stay under about 150 s.
