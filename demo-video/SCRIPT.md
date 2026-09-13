# Closeout demo film

About 2 min 39 s, 1920×1080 at 30 fps. Keynote style: music and captions, no voice. Captions sit
beside the screen, never on top of it. Every screen is a real Closeout screen from two fictional
projects, **Cedar Row Townhomes** and **418 Alder Court**. No real project, address or person appears.
Everything Closeout writes on screen is its real output from the filming runs; nothing is typed in for it.

## How it is made

| Step | Command | Cost |
|---|---|---|
| 1. Film the site walk, office and contractor, with the AI steps | `demo-video/film-ai.sh` | about $0.30 (the last run: $0.28) |
| 1a. Rehearse that filming for free | `demo-video/serve.sh && python3 demo-video/capture.py --dry` | free (AI requests get a fake error) |
| 2. Film the new project: upload the Alder Court zip, let Closeout file and read it | `demo-video/film-new.sh` | well under $1 (the last run: $0.47 on the cost log) |
| 2a. Rehearse the upload for free | `DEMO_PORT=8778 DEMO_DATA=demo-video/data-new demo-video/serve.sh && python3 demo-video/capture_new.py` | free |
| 3. Music (once each; skipped if the file exists) | `python3 demo-video/gen_music.py` → `audio/score.mp3`; `python3 demo-video/gen_music.py 2` → `audio/score2.mp3` | ElevenLabs music |
| 4. Contact-sheet check | `python3 demo-video/assemble.py --preview` → `output/preview/` | free |
| 5. Render | `python3 demo-video/assemble.py` → `output/closeout-demo.mp4` (`--free` → `closeout-demo-free.mp4`) | free |

`film-ai.sh` resets Cedar Row, serves it with the real AWS credentials, films, prints what it cost
(`demo-video/cost.py`), then puts the server back in free mode. `capture.py --paid` lets exactly one
write-up, one contractor upload and one question through; every other paid request is blocked and
stops the script.

`film-new.sh` builds the Alder Court zip if needed (`make_project2.py`: three rowhomes, five PDFs, two
issues of the architectural set), serves a separate scratch copy on port 8778 from `data-new/`, and
`capture_new.py --paid` lets exactly one project upload through. Its stills land in `output/cap-new/`;
without that folder `assemble.py` uses the free rehearsal stills in `output/cap-new-dry/`.

Media (`audio/`, `output/`, `data/`, `data-new/`, `project/`, `project2/`) is not committed.

## Beats

Two scores: the calm one under the new project and the site walk, the brighter one from the office on,
crossfaded over 2 s. Scenes overlap by 0.6 s.

| # | Sec | Screen | Caption |
|---|---|---|---|
| 1 | 6.5 | Site photo | A site walk ends with a list. / Then the chasing starts. |
| 2 | 5.5 | Title card | Closeout / From the walk to the last item closed. |
| 3 | 12.5 | Projects → upload the zip → status line | Start a project. Upload the whole project folder as one zip. / Closeout sorts it. Every file by discipline and date. Then it reads each current sheet. |
| 4 | 10.5 | Drawings → Documents and revision log | It opens already filed. Current sheets by discipline. / Nothing is lost. Older issues stay in the revision log, newest first. |
| 5 | 6 | Projects, both projects | Every project in one place. |
| 6 | 6 | Phone: field tab | On site, open the field tab. Start the review. |
| 7 | 6.5 | Photo → where | Take a photo. Say which unit and floor. |
| 8 | 5 | Plan | The plan opens on your floor. |
| 9 | 6.5 | Tap on plan, "Write it up for me" | Tap the spot. Ask Closeout to write it up. |
| 10 | 7.8 | Write-up → form | It reads the photo and the drawing, and writes it up. / You check it. Nothing is saved until you save. |
| 11 | 5 | Typed form | Or type it yourself. |
| 12 | 6.5 | Saved items | AR-01. AR-02. AR-03. Numbered, pinned to the plan, filed by unit. |
| 13 | 7 | Finish | Finish. It asks which units you walked. |
| 14 | 4.8 | Drafting → finished | Done on site. Closeout drafts the message to the contractor. |
| 15 | 6 | Deficiencies (second score starts) | Every item, where it is, and what it needs. |
| 16 | 9 | Report scroll | The report is ready. Photos, plan pins, what closes each item. |
| 17 | 7.5 | Drafted message | The message is drafted. Nothing is sent until you send it. |
| 18 | 7 | Contractor link | The contractor gets one link. / No account. Just their items. |
| 19 | 7 | Contractor upload | They send a photo. Closeout files it to the right item. |
| 20 | 8.5 | Item with the filed photo | It says what it could not confirm. |
| 21 | 7 | Accept | The engineer decides what closes. |
| 22 | 8.4 | Ask Closeout | Ask Closeout anything about the project. |
| 23 | 7 | Office + phone | The office and the contractor see the same list. |
| 24 | 9 | End card | Closeout / Walk it. Send it. Close it. |
