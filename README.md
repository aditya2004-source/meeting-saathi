# Meeting Saathi

**New here? Start with [`GETTING_STARTED.md`](GETTING_STARTED.md)** — a
plain-language, step-by-step guide to the one-time setup and daily use.
This README is a technical overview; see `docs/` for more:
- [`docs/INSTALL_EXTENSION.md`](docs/INSTALL_EXTENSION.md) — installing the Chrome extension
- [`docs/SETUP.md`](docs/SETUP.md) — full reference for every setting
- [`docs/USAGE.md`](docs/USAGE.md) — day-to-day usage
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how it's built
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common issues

A Chrome extension records your Google Meet calls, and after the meeting
ends, generates:

- **MOM** (Minutes of Meeting)
- **Requirement Gathering Sheet** (a 4-column table mapping what the client
  discussed to how it fits into Sarathi, area by area)
- **Discussion + Action Points** (who said what, who owns what action)

Everything is saved automatically — no manual downloads — into
`<BASE_STORAGE_DIR>/<Meeting Title> - <YYYY-MM-DD HHmm>/`, along with the full
speaker-labeled transcript as a backup. Document generation uses Gemini
(Google, free tier); everything else (recording, transcription, figuring
out who's speaking) runs locally or inside the free Chrome extension by
default. Optionally, setting `ASSEMBLYAI_API_KEY` offloads the slow
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
4. Gemini (Google's API, free tier) turns the speaker-labeled transcript
   into MOM, Requirement Gathering Sheet, and Action Points documents,
   using a two-call "extract then generate" pattern that stays grounded in
   the transcript (no invented facts).
5. Markdown docs are rendered to PDF via headless Chrome (Playwright), and
   everything is written to the meeting's folder.

## Setup

```bash
cd /home/enjay/projects/sarathi-meeting-bot
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
ln -sf "$(pwd)/scripts/systemd/sarathi-meeting-bot.service" ~/.config/systemd/user/sarathi-meeting-bot.service
systemctl --user daemon-reload
systemctl --user enable --now sarathi-meeting-bot.service
```

It starts on every login and restarts itself if it ever crashes. Check on it
with `systemctl --user status sarathi-meeting-bot.service` or
`journalctl --user -u sarathi-meeting-bot -f`.

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

If you want to tweak the MOM/Requirement-Gathering-Sheet/Action-Points prompts and re-run generation
against an existing transcript (without re-transcribing or re-diarizing):

```bash
python scripts/regenerate_docs.py "/path/to/meeting/folder/transcript.json"
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
