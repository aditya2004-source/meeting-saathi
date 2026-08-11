# Day-to-Day Usage

Assumes setup (`../GETTING_STARTED.md` or `SETUP.md`) is already done: the
extension is installed, `.env` has both keys, and the server is running.

## Recording a meeting

1. Join your Google Meet call in Chrome, normally, as you always would.
2. **Click the extension's toolbar icon once.** Chrome requires this — a
   security rule that stops any extension from silently recording tab
   audio, not something this project can skip (see
   `../extension/DESIGN.md` if you want the technical reason). You'll
   notice an orange dot on the icon once the extension detects you're in a
   call, inviting this click; clicking it opens the popup and starts
   recording immediately, with no further click inside the popup needed.
   Look for the small red **REC** badge on the toolbar icon to confirm.
3. Have your meeting.
4. Leave the call. Recording stops automatically and is sent off for
   processing — no further clicks needed.

If the badge never turns red after clicking (rare — Meet occasionally
changes its page structure and call-detection misses), open the popup and
use the manual **Start Recording** button directly, with an optional title
box.

## Watching progress

Open http://localhost:8420. Each meeting appears as a row with a status that
updates as it moves through: `received` → `transcribing` → `diarizing` →
`generating_docs` → `rendering` → `saving` → `saved`. The page refreshes
itself every 10 seconds — leave it open or check back later.

If a run shows `failed`, an error message appears next to it explaining
what went wrong.

## Where your documents land

```
~/Downloads/Sarathi Meetings/<Meeting Title> - <YYYY-MM-DD HHmm>/
├── MOM.pdf                          <- share this one
├── Requirement_Gathering_Sheet.pdf   <- share this one
├── Action_Points.pdf                 <- share this one
├── MOM.md, Requirement_Gathering_Sheet.md, Action_Points.md   (same content, plain text/markdown)
├── transcript.json        (full backup, structured)
└── transcript.txt         (full backup, easy to read)
```

The meeting title comes from Google Meet's page title, best-effort — if it
comes out wrong (e.g. just the meeting code), retitle the meeting in Meet
before joining, or type the correct title into the extension popup and
start recording manually instead of relying on auto-detection.

## Processing a recording you already have

You don't need the extension for this — the status page itself has a
manual upload option ("Manually process an existing recording") where you
can pick any audio/video file and a title, and it'll run through the same
pipeline. Useful for testing, or for meetings you recorded some other way.

## Fixing up a document without re-processing the whole recording

If a MOM/Requirement Gathering Sheet/Action Points document didn't come out right and you want to
regenerate just the documents (using the transcript that's already saved,
without re-transcribing or re-diarizing):

```bash
cd /home/enjay/projects/sarathi-meeting-bot
python scripts/regenerate_docs.py "/home/enjay/Downloads/Sarathi Meetings/<folder name>/transcript.json"
```

This overwrites `MOM.md`/`.pdf`, `Requirement_Gathering_Sheet.md`/`.pdf`, and
`Action_Points.md`/`.pdf` in that same folder.

## Stopping / restarting the server

If you changed `.env` (e.g. added a key), the server needs a restart to pick
it up. Ask your assistant to restart it, or manually:
```bash
pkill -f "uvicorn app.main:app"
cd /home/enjay/projects/sarathi-meeting-bot
uvicorn app.main:app --host 127.0.0.1 --port 8420
```
