# Closeout demo film

About 1 min 40 s, 1920×1080 at 30 fps. Narrated: one voice says what each screen is doing, over a low
score. It shows Closeout as project management — upload, drawings, documents, the overview, the contractor
and closing items — with the field review as one short part. Captions sit beside the screen, never on top of it. Every screen is a real Closeout screen from two fictional
projects, **Cedar Row Townhomes** and **418 Alder Court**. No real project, address or person appears.
Everything Closeout writes on screen is its real output from the filming runs; nothing is typed in for it.

## How it is made

| Step | Command | Cost |
|---|---|---|
| 1. Film the site walk, office and contractor, with the AI steps | `demo-video/film-ai.sh` | about $0.30 (the last run: $0.28) |
| 1a. Rehearse that filming for free | `demo-video/serve.sh && python3 demo-video/capture.py --dry` | free (AI requests get a fake error) |
| 2. Film the new project: upload the Alder Court zip, let Closeout file and read it | `demo-video/film-new.sh` | well under $1 (the last run: $0.47 on the cost log) |
| 2a. Rehearse the upload for free | `DEMO_PORT=8778 DEMO_DATA=demo-video/data-new demo-video/serve.sh && python3 demo-video/capture_new.py` | free |
| 2b. Film the folders: where every drawing sheet and document was filed, then your own folders and a set filed into one | `rm -rf demo-video/data-folders && cp -R demo-video/data-new demo-video/data-folders`, serve it on port 8779, then `python3 demo-video/capture_folders.py` | free |
| 3. Music (once each; skipped if the file exists) | `python3 demo-video/gen_music.py` → `audio/score.mp3`; `python3 demo-video/gen_music.py 2` → `audio/score2.mp3` | ElevenLabs music |
| 3a. Narration (only changed lines are voiced again) | `python3 demo-video/gen_voice.py` → `audio/vo/<scene>.mp3` + `.json` word timings | ElevenLabs, about 1,300 characters |
| 4. Contact-sheet check | `python3 demo-video/assemble.py --preview` → `output/preview/` | free |
| 5. Render | `python3 demo-video/assemble.py` → `output/closeout-demo.mp4` (`--captions` → the earlier captions-only cut; `--free` → `closeout-demo-free.mp4`) | free |

`film-ai.sh` resets Cedar Row, serves it with the real AWS credentials, films, prints what it cost
(`demo-video/cost.py`), then puts the server back in free mode. `capture.py --paid` lets exactly one
write-up, one contractor upload and one question through; every other paid request is blocked and
stops the script.

`film-new.sh` builds the Alder Court zip if needed (`make_project2.py`: three rowhomes, five PDFs, two
issues of the architectural set), serves a separate scratch copy on port 8778 from `data-new/`, and
`capture_new.py --paid` lets exactly one project upload through. Its stills land in `output/cap-new/`;
without that folder `assemble.py` uses the free rehearsal stills in `output/cap-new-dry/`.

Media (`audio/`, `output/`, `data/`, `data-new/`, `data-folders/`, `project/`, `project2/`) is not committed.

## Narrated beats (the default cut)

Each scene is as long as its line plus a beat; highlights and still changes are timed to the words
(`vo(key)` in `assemble.py` reads the word timings). One low score under the voice. Scenes overlap by 0.6 s.

| # | Clip | Screen | Voice | Captions |
|---|---|---|---|---|
| 1 | title | Black title card | Closeout keeps a building project in one place: the drawings, the documents, the field reviews, and the contractor. | Closeout / Drawings, documents, field reviews and the contractor, in one project. |
| 2 | upload | New project, zip upload and progress | Start a new project by uploading the project folder as one zip. Closeout reads every file, and files it by building and discipline. | New project / Closeout reads every file |
| 3 | drawings | Drawings › Site, then › 418 Alder Court | Drawings land where they belong. The site plan and the site electrical under Site. The floor plans under the building. | A-101, E-201, E-202 / A-201, A-202 |
| 4 | documents | Documents: Site › Electrical, building Electrical, Other files | Documents get the same folders. The electrical set, the letter of assurance, and the building permit, each in its place. | Electrical Set / Electrical Letter of Assurance / Building Permit |
| 5 | occupancy | Documents before occupancy, Letters of assurance | Documents needed before occupancy are kept as checklists. Here, one of eight letters of assurance is on file. | 6 checklists / 1 of 8 on file |
| 6 | folders | Own folder, Move…, Filed | Make folders of your own, and move files into them. Every move can be undone. | Older issues › June 2026 issue / Every move can be undone. |
| 7 | overview | Projects → Cedar Row overview | Every project opens on its overview: how many items are ready to close, and what to do next. | 0 of 3 ready to close. Next: send the review to the contractor. |
| 8 | walk | Phone: field review, photo, tap on the plan | The field review is done on a phone. Take a photo, and tap where it is on the drawing. | Field review / Take a photo. / Tap the spot |
| 9 | writeup | Phone: write-up, check and save, pins | Closeout writes up the deficiency. Check it, save it, and it is pinned to the plan. | Closeout writes it up. / AR-01. AR-02. AR-03. |
| 10 | office | Deficiencies list, report scroll | In the office, every deficiency is listed with its unit, floor and sheet. The report shows each one with its photo and plan pin. | Deficiencies / The report |
| 11 | contractor | Email to the contractor, then their reply | Closeout writes the email to the contractor, with the report attached. When they reply, the email is filed to the review and checked against each item. | The email: subject, message and the Field review 1 report, ready to send. / The reply: filed to Field review 1 and checked against AR-01, AR-02 and AR-03. |
| 12 | decide | Item with filed photo, Your call | Closeout checks the photo against the item. The engineer makes the call: ready to close, hold, or not accepted. | Location not confirmed / The engineer decides |
| 13 | ask | Ask panel and answer | And you can ask about the project. What does the contractor still need to send? Closeout lists the two items with nothing received. | What does the contractor still need to send? / AR-02 and AR-03 |
| 14 | — | End card | — | Closeout |

## Captions-only cut (`--captions`)

Two scores: the calm one under the new project and the site walk, the brighter one from the office on,
crossfaded over 2 s. Scenes overlap by 0.6 s.

| # | Sec | Screen | Caption |
|---|---|---|---|
| 1 | 6.5 | Site photo | A field review finds deficiencies. / Each one needs proof it was fixed. |
| 2 | 5.5 | Title card | Closeout / Deficiency tracking for field reviews. |
| 3 | 12.5 | Projects → upload the zip → status line | New project: upload the project folder as one zip. Here, 418 Alder Court: 5 PDFs. / Closeout reads the files and files them by building and discipline. |
| 4 | 13 | Drawings › Site → Drawings › 418 Alder Court | Drawings › Site: A-101 site plan. E-201 and E-202, electrical, site-wide. / Drawings › 418 Alder Court: A-201 main floor plan and A-202 upper floor plan. |
| 5 | 12.5 | Documents → Site › Architectural → Site › Electrical | The folders it set up: documents before occupancy, the project folder, and one per building. / Site › Architectural: both issues of the set. September is current, June is kept as older. / Site › Electrical: Electrical Set, issued 2026-09-02. |
| 6 | 12.5 | 418 Alder Court → its Electrical → its Other files | 418 Alder Court: Architectural, Electrical and Other files. / 418 Alder Court › Electrical: Electrical Letter of Assurance, 2026-05-28. / 418 Alder Court › Other files: Building Permit, 2026-05-20. |
| 7 | 9.5 | Documents before occupancy → Letters of assurance | Documents before occupancy: 6 checklists. Letters of assurance: 1 of 8 on file. / Letters of assurance: Schedule C-B, electrical: the Electrical Letter of Assurance is on file. |
| 8 | 9 | New folder → Older issues made | New folder: make a folder of your own inside any folder. Here, Older issues, inside Site › Architectural. |
| 9 | 8.5 | June 2026 issue inside Older issues → Move… panel | Folders inside folders: June 2026 issue, inside Older issues. / Move…: the June set goes into June 2026 issue. |
| 10 | 6 | The June set in its folder | Filed: Site › Architectural › Older issues › June 2026 issue. Every move can be undone. |
| 11 | 6 | Projects, both projects | Projects: open Cedar Row Townhomes for the site walk. |
| 12 | 6 | Phone: field tab | On site, open the field tab. Start the review. |
| 13 | 6.5 | Photo → where | Take a photo. Say which unit and floor. |
| 14 | 5 | Plan | The plan opens on your floor. |
| 15 | 6.5 | Tap on plan, "Write it up for me" | Tap the spot. Ask Closeout to write it up. |
| 16 | 7.8 | Write-up → form | It reads the photo and the drawing, and writes it up. / Check the wording, then save. |
| 17 | 5 | Typed form | Or type it yourself. |
| 18 | 6.5 | Saved items | AR-01. AR-02. AR-03. / Numbered, pinned to the plan, filed by unit. |
| 19 | 7 | Finish | Finish. It asks which units you walked. |
| 20 | 4.8 | Drafting → finished | Field review 1 finished. Closeout drafts the message to the contractor. |
| 21 | 6 | Deficiencies (second score starts) | 0 of 3 ready to close. Each item with its unit, floor and sheet. |
| 22 | 9 | Report scroll | The field review report. Each item with its photo, plan pin and what closes it. |
| 23 | 7.5 | Drafted message | The drafted message. 3 items to close at Cedar Row. Nothing is sent until you send it. |
| 24 | 7 | Contractor link | The contractor gets one link. / No account. Only their items. |
| 25 | 7 | Contractor upload | They upload AR-01.jpg. Closeout files it on item AR-01. |
| 26 | 8.5 | Item with the filed photo | Location not confirmed. The photo matches AR-01, but nothing in it shows where it was taken. |
| 27 | 7 | Accept | The engineer decides. Ready to close, hold, or not accepted. |
| 28 | 8.4 | Ask Closeout | Ask about the project: “What does the contractor still need to send?” |
| 29 | 7 | Office + phone | Office and contractor. The same list, on both screens. |
| 30 | 6 | End card | Closeout |
