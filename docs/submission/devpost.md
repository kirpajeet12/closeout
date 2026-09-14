# Devpost submission draft

Draft for review. Paste each section into the matching Devpost field. Nothing here has been submitted.

## Project name (max 60 characters)

Closeout

## Tagline

Drawings, documents, field reviews and contractor replies for an engineering office, in one project.

## Inspiration

We work in an engineering office that does field reviews on residential and commercial buildings. The site walk
takes an hour. What follows takes weeks: the report goes to the contractor, photos come back by email with
"done" in the subject, and someone has to work out which photo answers which deficiency, what is still missing,
and whether the drawings used on the walk were the current issue. None of that needs an engineer's judgement,
but all of it sits on an engineer's desk. We wanted the reading and filing done for us, with every decision
still ours.

## What it does

Closeout is a project workspace for the office that reviews a building.

- **Project folder.** Upload the project as one zip. Closeout reads every file, files drawings and documents by
  building and discipline, keeps older issues apart from the current set, and lists documents still needed
  before occupancy, such as letters of assurance. Every move it makes is marked and can be undone.
- **Drawings.** Each sheet is read once for its discipline, units and floors, so a field review opens on the
  plan for the floor you are standing on.
- **Field review on a phone.** Tap the spot on the plan, add a photo, type or dictate the problem. Closeout can
  tidy it into a numbered deficiency (`EL-01`) with its location and what the contractor must send to close it.
- **Finish and send.** Closeout confirms which review it was and who gets the report, writes the covering email,
  and attaches the PDF report with every deficiency, photo and plan pin. Nothing leaves until the engineer
  presses Send from the office's own Gmail or Microsoft 365 mailbox.
- **Replies.** Closeout reads the replies in that thread, or new emails with the review's reference in the
  subject, and matches each photo or document to the item it answers. An email that says "I did it" with no
  photo does not close an item that asked for one, and Closeout says so next to the words.
- **The engineer decides.** Each item shows what was asked for, what came in and what is missing. The engineer
  marks it ready to close, on hold or not accepted.
- **Ask.** Ask about the project in plain words, typed or spoken, and get an answer from the project's records.
- **Office accounts.** The office adds each person under People. They sign in with a password from the welcome
  email, or continue with Google or Microsoft using that same address. Anyone signed in can report an issue with the
  screen they were on, and the office marks it fixed.

Closeout never says work is acceptable, compliant or approved, never sends email on its own, and never guesses
a location or picks between two items when a photo could fit either.

## How we built it

- **Strands Agents SDK, one narrow job per agent.** Reading a sheet, filing a folder, matching plans to units,
  writing up a deficiency, writing the contractor email, matching a returned photo to an item, and answering a
  question are separate Strands agents. Each gets only the tools for its job, and those tools write structured
  records.
- **Tools that refuse bad calls.** Every tool validates its input: a unit or floor must exist in the drawing set,
  the email must mention every item in the report, judgement words like "compliant" are refused, and a photo's
  location must come from the file, the pin or the contractor's note. A refused call returns the reason, and the
  agent corrects itself in the same job.
- **Plain code decides the state.** Whether an item has everything it asked for, whether a reply closes anything,
  and what goes in the report are computed from the recorded evidence, not by the model.
- **Claude on Amazon Bedrock.** Sonnet for reading drawings and writing; Haiku for quick answers. The server's IAM
  role allows Bedrock calls only, so there are no AWS keys in the app.
- **App.** FastAPI and SQLite, a single-page web app that works on a phone on site, PDF reports, Gmail and
  Microsoft Graph over OAuth for sending and reading mail. Runs on EC2 with Docker Compose and Caddy for TLS.
- **Voice.** Dictation on the walk; for spoken questions, OpenAI's realtime voice model listens and speaks while
  the Strands Ask agent produces the answer.

## Challenges we ran into

- **Locations on real drawings.** Sheets print units as "#2", "UNIT B" or a civic address, and one sheet can show
  several buildings. We had to match those labels to buildings, units and floors before a pin could mean anything.
- **Knowing when not to answer.** The easy failure is a model that files a photo on the most likely item. We made
  "ambiguous" and "location not confirmed" first-class results that stay with the engineer.
- **Email as the contractor's only tool.** Contractors will not log in to another app. Replies arrive in threads,
  in new emails, from other people, with or without attachments, so filing had to work from the thread, the
  reference in the subject, and the engineer's pick as the last resort.
- **Keeping a real project private.** We built against real drawing sets, so every screenshot, sample and the
  video use fictional projects instead.

## Accomplishments that we're proud of

- A field review goes from a tap on the plan to a sent report with photos and plan pins, and the replies come
  back filed against the right items.
- The agents describe and the office decides: no tool can mark work accepted, and no email leaves without Send.
- It is a complete product built against our office's real drawing sets, not a single-screen demo.

## What we learned

- A small agent with strict tools is easier to trust than one agent with a long prompt. A tool that refuses a bad
  call with a reason gives the model something concrete to fix.
- Deciding what the model should not decide (completeness, closing an item, sending) made the rest easier to trust.
- Contractors and offices already run on email, so the product had to meet them there.

## What's next for Closeout

- Stage-by-stage reviews (underground, rough-in, pre-drywall, final) that carry open items forward.
- Checking a new drawing issue against the last one and flagging open items it affects.
- More offices on it, and more disciplines.

## Built with

strands-agents, amazon-bedrock, claude, python, fastapi, sqlite, amazon-ec2, docker, caddy, gmail-api,
microsoft-graph, javascript

## Links

- Repository: https://github.com/kirpajeet12/closeout
- Live app: https://closeout.getcrewbrew.com (access code shared with judges in the testing instructions)
- Video: (YouTube URL, once uploaded)

---

# builder.aws.com post draft (bonus)

Title must contain "Agents for Humans".

## Title

Agents for Humans: filing a contractor's "done" emails for an engineering office with Strands

## Body

Field reviews end with a report to the contractor. Weeks of email follow: photos with no location, "fixed" with
no photo, replies from people who were never on the thread. Closeout is the tool we built for our own office to
handle that part.

It is built on the Strands Agents SDK as a set of small agents, each with one job and only the tools for it: read
a drawing sheet, file the project folder, write up a deficiency from a pin on the plan, write the email to the
contractor, match a returned photo to the item it answers. The tools are where the rules live. A unit must exist
in the drawing set, the email must name every item, and words like "compliant" or "approved" are refused with a
reason the agent can act on.

What the model does not decide matters as much. Whether an item has everything it asked for is plain code over
the recorded evidence. Closing an item is the engineer's press. No email leaves without Send. When a contractor
writes "I did it" with no photo, Closeout shows the words next to what is still needed, and the item stays open.

It runs on EC2 with Claude on Amazon Bedrock through an IAM role, sends and reads mail from the office's own Gmail
or Microsoft 365 mailbox, and works on a phone on site.

Repository: https://github.com/kirpajeet12/closeout · Video: (YouTube URL, once uploaded)
