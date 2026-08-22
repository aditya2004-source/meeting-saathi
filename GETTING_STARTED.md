# Getting Started — Read This First

This is the only file you need to read to get up and running. Everything
else is either code or reference documentation for later.

## What this is, in one line

A Chrome extension records your Google Meet calls (both sides of the
conversation), and after the meeting ends, three documents (MOM,
Requirement Gathering Sheet, Action Points) plus a full transcript are
generated and saved straight to
your computer automatically — no downloading or saving anything by hand.

## How it actually works (important — read this)

Unlike a service that sends a separate "bot" to join your meeting on its
own, this uses a **Chrome extension**. That means:
- **You still join the meeting yourself**, in Chrome, with the extension
  installed.
- Chrome requires **one click on the extension icon each time you join a
  call** to start recording — this is a Chrome security rule (extensions
  can't silently start recording tab audio), not something this project
  chose. Recording **stops automatically** when you leave, with no further
  clicks — upload, transcription, and document generation all happen on
  their own after that.
- Document generation uses **Gemini** (Google's API) on its **free tier**
  — no billing, no credit card. Everything else — joining, recording,
  transcription, figuring out who said what — runs locally on your
  computer or inside the free extension. No Recall.ai, no paid service at
  all.

## What is already done (you don't need to do this)

- The whole program is written and tested (`/home/enjay/projects/sarathi-meeting-bot`).
- The local server runs at **http://localhost:8420**.

## What YOU still need to do — 3 steps

### Step 1 — Install the Chrome extension (one time)

See `docs/INSTALL_EXTENSION.md` for exact click-by-click steps. Short
version: open `chrome://extensions`, turn on Developer mode, click "Load
unpacked", and select the `extension` folder inside this project.

### Step 2 — Get a free Gemini API key (one time, needed to write the documents)

1. Go to https://aistudio.google.com/apikey (sign in with any Google account)
2. Click "Create API key" — no billing setup, no credit card
3. Send me that key, or paste it into `.env` yourself as
   `GEMINI_API_KEY=...`, and ask me to restart the server.

This is genuinely free — the free tier has daily rate limits, but they're
far more than a normal day's meetings would use.

### Step 3 — Get a free HuggingFace token (one time, needed for "who said what")

The extension records one mixed audio track (everyone's voices together).
To figure out **who** said **what**, the program needs a small free local
AI model, which requires a one-time free account:

1. Sign up at https://huggingface.co (free)
2. Go to https://huggingface.co/pyannote/speaker-diarization-community-1 and click
   to accept the model's terms (one click, no cost)
3. Create a token at https://huggingface.co/settings/tokens (choose "read"
   access)
4. Send me that token, or paste it into `.env` yourself as
   `HUGGINGFACE_TOKEN=...`, and ask me to restart the server.

This is **not** a paid API — just a free account, similar to signing up for
any website.

## How you'll use it every day, after setup

1. Open Google Meet in Chrome like you normally would, and join your
   meeting.
2. **Click the Meeting Saathi icon in Chrome's toolbar once** — you'll
   briefly see an orange dot on the icon once you're in the call, inviting
   this click. This starts recording; you'll then see a small red "REC"
   badge on the icon.
3. Have your meeting normally.
4. Leave the call when it's done — recording stops automatically and gets
   sent off for processing. No further clicks needed.
5. A few minutes later, check:
   `~/Downloads/Meeting Saathi/<meeting title> - <date> <time>/`
   You'll find `MOM.pdf`, `Requirement_Gathering_Sheet.pdf`, `Action_Points.pdf`, and the full
   transcript, already sitting there.
6. You can also check progress any time at http://localhost:8420.

## If the icon click doesn't start recording

Open the popup again (click the icon) — if there's still a title box and a
"Start Recording" button visible and it's not already showing "Recording:
...", click that button directly, and optionally fix the meeting title in
the box above it.

## If something isn't working

See `docs/TROUBLESHOOTING.md`. For how the project is built internally, see
`docs/ARCHITECTURE.md`.
