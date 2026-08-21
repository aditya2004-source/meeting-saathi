# Sarathi Meeting Bot — Project Overview

*A plain-language summary of what has been built, why, and what it needs to run at full scale.*

---

## 1. What is this?

Sarathi Meeting Bot is a tool that sits quietly in the background during a
Google Meet client call and automatically produces three ready-to-send
documents once the meeting ends:

- **Minutes of Meeting (MOM)**
- **Requirement Gathering Sheet**
- **Action Points**

No one needs to take notes during the call, and no one needs to sit down
afterward and write these documents by hand. The bot listens, figures out
who said what, and writes the documents itself.

## 2. Why this was built

After every client call, someone has to go back — either from memory or
from a recording — and write up the MOM, list out the requirements that
were discussed, and note down the action items. This is:

- **Slow** — it eats into time that could go toward actual project work.
- **Inconsistent** — quality and detail depend on who writes it and how
  tired/rushed they are.
- **Easy to get wrong** — details get missed, especially in longer calls
  with multiple speakers.

Sarathi Meeting Bot removes this manual step entirely. The documents are
ready within minutes of the call ending, in a consistent professional
format, every time.

## 3. How it works (in plain terms)

1. A small Chrome extension joins the meeting audio in the background
   while you're on the Google Meet call — nothing to operate during the
   call itself.
2. The audio is converted to text automatically (speech-to-text).
3. The system figures out **who** said **what**, using Google Meet's own
   on-screen "who's speaking" indicators and the list of people who
   joined the call.
4. This structured transcript is sent to Google's Gemini AI, which drafts
   the three documents in clean, simple English.
5. The finished documents are saved automatically — ready to review and
   send to the client.

No manual transcription, no manual note-taking, no manual formatting.

## 4. Effort this saves

**Before:** someone has to listen back through the call (or rely on
memory) and manually type out the MOM, requirements list, and action
items — real time spent per meeting, on top of the meeting itself.

**After:** the three documents are generated automatically, within
minutes of the call ending, already containing:
- the correct attendee names (everyone who joined, not just who spoke),
- the real date and time of the meeting (auto-filled, not guessed),
- clean, simple English throughout (even if parts of the conversation
  happened in Hindi).

The person who was on the call now only needs to **review and send**,
instead of **write from scratch**.

## 5. What it's built with (technology, in plain terms)

| Part | What's used | Why |
|---|---|---|
| Backend / brain of the system | Python (FastAPI) | Runs on the office machine, processes each meeting |
| Data storage | SQLite | A lightweight built-in database — no separate database server to install or maintain |
| Browser side | Chrome extension (JavaScript) | Captures the meeting audio and who's-in-the-call information directly from Google Meet |
| Speech-to-text | Whisper (runs locally, free) | Converts audio to text; also translates Hindi speech to English automatically |
| Speaker identification | Google Meet's own indicators, with an optional paid AI backup (AssemblyAI) for tricky audio | Figures out who said what |
| Document writing | Google Gemini AI | Reads the transcript and writes the actual MOM / Requirement Sheet / Action Points |
| Always-on operation | Runs as a background service on the machine | So it's ready the moment a meeting starts, with no one needing to manually start it |

## 6. What's needed to use this properly

The system works end-to-end today, tested on a real client call. To move
from "working and tested" to "reliable for everyday company use," a few
things are needed:

- **A paid Gemini API key.** Right now the system uses Google's **free
  tier**, which allows only around **20 document-generation requests per
  day**. That's enough for testing, but will run out quickly with real,
  regular usage across the team. Enabling billing on a Google Cloud
  project removes this cap. Gemini's pricing is pay-per-use — there's no
  fixed monthly fee, and at this scale the cost is low.

- **An AssemblyAI key (optional, only for harder calls).** This is a paid
  backup used only when the primary, free speaker-detection struggles
  with especially unclear audio. It costs roughly **$0.17 per hour of
  meeting audio**, and only when it's actually triggered — not a
  standing cost.

- **A dedicated, always-on machine.** The system currently runs on a
  local machine. For dependable day-to-day use, it should run on a small
  always-on server or cloud machine, rather than someone's personal
  laptop that gets shut down or disconnected.

- **Proper distribution of the Chrome extension.** Right now it's loaded
  manually for testing. For the wider team to use it, it should be
  packaged and shared (or privately published) so it can be installed
  without manual setup steps.

## 7. Current status

The system has been tested end-to-end on a real client meeting and works.
This week, three quality issues found during that test were fixed:
attendee names and the full attendee list are now captured correctly,
the real meeting date and time are filled in automatically, and Hindi
text no longer leaks into the English documents.

It is working and actively being tested, but not yet rolled out for
everyday company-wide use — the items in Section 6 above are what stand
between "working prototype" and "ready for the whole team."
