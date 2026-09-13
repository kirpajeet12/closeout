# Closeout demo film

About 2 min 21 s, 1920×1080 at 30 fps. Keynote style: music and on-screen captions, no voice.
Every screen is a real Closeout screen from the fictional **Cedar Row Townhomes** project. No real
project, address or person appears.

## How it is made

| Step | Command | Cost |
|---|---|---|
| 1. Reset the fictional project and serve it on 127.0.0.1:8777 | `demo-video/serve.sh` | free (fake cloud credentials, so no model call can run) |
| 2. Film every screen | `python3 demo-video/capture.py` | free (the script blocks every paid route and exits if one was tried) |
| 3. Music (once; skipped if `audio/score.mp3` exists) | `python3 demo-video/gen_music.py` | ElevenLabs music |
| 4. Contact-sheet check | `python3 demo-video/assemble.py --preview` | free |
| 5. Render | `python3 demo-video/assemble.py` → `output/closeout-demo.mp4` | free |

Media (`audio/`, `output/`, `data/`, `project/`) is not committed.

## Beats

| # | Sec | Screen | Caption |
|---|---|---|---|
| 1 | 7 | Site photo | A site walk ends with a list. / Then the chasing starts. |
| 2 | 6.5 | Title card | Closeout / From the walk to the last item closed. |
| 3 | 9 | Projects → drawings | One project. Every drawing, filed. |
| 4 | 6.5 | Phone: field tab | On site, open the field tab. Start the review. |
| 5 | 7 | Photo → where | Take a photo. Say which unit and floor. |
| 6 | 6.5 | Plan | The plan opens on your floor. |
| 7 | 7.5 | Tap on plan | Tap the spot. Closeout can write it up from the photo, or you type it yourself. |
| 8 | 7 | Typed form | Nothing is saved until you save it. |
| 9 | 7.5 | Saved items | AR-01. AR-02. AR-03. Numbered, pinned to the plan, filed by unit. |
| 10 | 8 | Finish | Finish. It asks which units you walked. |
| 11 | 5.5 | Finished | The list is frozen for the contractor. |
| 12 | 7 | Overview | The overview always shows the next step. |
| 13 | 7 | Deficiencies | Every item, where it is, and what it needs. |
| 14 | 12 | Report scroll | The report is ready. Photos, plan pins, what closes each item. |
| 15 | 11 | Contractor link | The contractor gets one link. No account. Just their items, and what closes each one. |
| 16 | 9.5 | Item + upload | What they send lands on the item it belongs to. |
| 17 | 7.5 | Accept | The engineer decides what closes. |
| 18 | 8 | Office + phone | The office and the contractor see the same list. |
| 19 | 12 | End card | Closeout / Walk it. Send it. Close it. / Built with Strands Agents SDK on Amazon Bedrock |

## What is not in this cut

The free pass shows no model output, so these real features are only mentioned, never shown:
the write-up from a photo, the drafted message to the contractor, and Closeout filing a contractor
upload onto the right item. To add them, the owner starts `demo-video/serve.sh paid` by hand;
nothing paid runs without that.
