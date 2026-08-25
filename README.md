# Meeting Saathi

**New here? Start with [`GETTING_STARTED.md`](GETTING_STARTED.md)** — a
plain-language, step-by-step guide to the one-time setup and daily use.
This README is a technical overview; see `docs/` for more:
- [`docs/INSTALL_EXTENSION.md`](docs/INSTALL_EXTENSION.md) — installing the Chrome extension
- [`docs/SETUP.md`](docs/SETUP.md) — full reference for every setting
- [`docs/USAGE.md`](docs/USAGE.md) — day-to-day usage
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how it's built
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common issues

A Chrome extension records your Google Meet calls. After the meeting ends,
the transcript and a set of extracted facts (requirements, decisions, risks,
assumptions, dependencies, open questions, commitments, and — if a business
process was walked through — a structured AS-IS process) are ready
automatically. **No document generates automatically** — from the dashboard,
generate only whichever of these you actually need, on demand:

- **MOM** (Minutes of Meeting)
- **Meeting Analysis** (one consolidated summary)
- **BRD** (Business Requirements Document)
- **FRD** (Functional Requirements Document) — instant, no API call
- **User Stories** + **Acceptance Criteria** (one shared generation)
- **Business Process Flow** (AS-IS) — a Mermaid flowchart + PDF, instant, no
  API call; any part the transcript left unclear is visually flagged
  "Needs Clarification" rather than guessed

Everything is saved automatically — no manual downloads — into
`<BASE_STORAGE_DIR>/<Meeting Title> - <YYYY-MM-DD HHmm>/`, along with the full
speaker-labeled transcript as a backup. Document generation uses Gemini
(Google, free tier), only when you click "Generate" for a specific document;
everything else (recording, transcription, figuring out who's speaking, FRD,
Business Process Flow) runs locally or inside the free Chrome extension.
Optionally, setting `ASSEMBLYAI_API_KEY` offloads the slow
diarization-fallback case (sparse DOM speaker coverage) to AssemblyAI
(paid, pay-as-you-go ~$0.17/hour) instead of local pyannote — see
`.env.example`.

## How it works

1. The `extension/` Chrome extension detects when you join a Google Meet
   call. Chrome requires one click on the extension icon per meeting to
   actually start capture (a platform security rule — see
   `extension/DESIGN.md`); that click records tab audio (other
   participants) mixed with your microphone (you), via `chrome.tabCapture`
   + the Web Audio API in a Manifest V3 offscreen document.
2. When you leave the call, the extension uploads the recording to the
   local server at `POST /meetings/upload`.
3. Speech is transcribed locally with `faster-whisper` (CPU). Speakers are
   identified with local `pyannote.audio` diarization, aligned to Whisper's
   segments by timestamp, then resolved to real names where possible using
   a best-effort timeline of "who's currently speaking" the extension
   captures from Meet's UI while recording (falls back to "Speaker N" when
   there's no confident match).
4. Gemini (Google's API, free tier) extracts structured facts from the
   speaker-labeled transcript — this one call is still automatic, staying
   grounded in the transcript (no invented facts; anything unclear is marked
   "needs clarification" instead of guessed).
5. On the dashboard, click "Generate" on whichever document(s) you actually
   need — each triggers its own Gemini call (or, for FRD/Business Process
   Flow, a free local render) and is rendered to PDF (Markdown docs via
   headless Chrome/Playwright; Business Process Flow via a Mermaid diagram,
   also exported as an editable `.mmd` file), written into the meeting's
   folder.

## Setup

```bash
cd /home/enjay/projects/meeting-saathi
pip3 install --user -r requirements.txt
# If you'd rather use an isolated virtualenv instead of --user installs:
#   sudo apt install python3.10-venv && python3 -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt
playwright install chromium   # or rely on channel="chrome" to use system Chrome

cp .env.example .env
# then edit .env: GEMINI_API_KEY, HUGGINGFACE_TOKEN, BASE_STORAGE_DIR
```

Then install the Chrome extension — see `docs/INSTALL_EXTENSION.md`
(`chrome://extensions` → Developer mode → Load unpacked → select the
`extension/` folder).

## Run

Normally you don't need to start this by hand — a systemd user service keeps
it running automatically:

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$(pwd)/scripts/systemd/meeting-saathi.service" ~/.config/systemd/user/meeting-saathi.service
systemctl --user daemon-reload
systemctl --user enable --now meeting-saathi.service
```

It starts on every login and restarts itself if it ever crashes. Check on it
with `systemctl --user status meeting-saathi.service` or
`journalctl --user -u meeting-saathi -f`.

For local development (or if you'd rather run it in the foreground), the
manual command still works:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8420
```

Join a Google Meet call with the extension installed — a "Start Recording"
button appears on the page as soon as you join; click it once (required by
Chrome — see `extension/DESIGN.md`) and it stops automatically when you
leave. Watch http://localhost:8420 for progress. A manual upload form is
also available there for processing an existing recording without the
extension (testing/fallback).

## Regenerating documents without re-processing the recording

If you want to tweak a document's prompt and re-run generation against an
existing transcript (without re-transcribing or re-diarizing), or just
generate one from the command line instead of the dashboard:

```bash
# every document (extracts facts.json first if it doesn't already exist)
python scripts/regenerate_docs.py "/path/to/meeting/folder/transcript.json"

# just one or a few (see app/docgen/registry.py for the full key list)
python scripts/regenerate_docs.py "/path/to/meeting/folder/transcript.json" mom brd
```

## Tests

```bash
pytest
```

## v1 scope notes

- One meeting at a time; no concurrency handling.
- Meeting title is best-effort from Google Meet's page title (or typed
  manually via the extension popup / the manual upload form) — no Google
  Calendar lookup.
- No separate "bot" identity — the extension records a meeting you're
  actually present in, not one joined unattended.
- Raw uploaded recording is deleted after processing by default
  (`KEEP_RAW_RECORDING=false`); only the transcript is kept as the backup.
- No retry/resume after a crash — a failed run is visible with an error
  message on the status page; `scripts/regenerate_docs.py` lets you recover
  the documents from a saved transcript if the pipeline died after
  transcription.
