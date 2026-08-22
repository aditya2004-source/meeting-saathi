# Troubleshooting

## "The page at localhost:8420 won't load"

The server isn't running. Ask your assistant to start it, or run:
```bash
cd /home/enjay/projects/meeting-saathi
uvicorn app.main:app --host 127.0.0.1 --port 8420
```

## "Extension has not been invoked for the current page (see activeTab permission)"

This means recording was attempted without a real click on the extension.
Chrome only allows `chrome.tabCapture` to start in response to a genuine
user gesture on the extension itself (icon/popup click) — a page detecting
"you joined a call" doesn't qualify, so recording can never truly start by
itself. **Click the extension's toolbar icon once after joining a call** —
that's the one required manual step (see `../extension/DESIGN.md` for
why). Everything else (stop, upload, transcription, documents) still
happens automatically after that.

## The extension doesn't seem to start recording when I click its icon

- Confirm the extension is actually installed and enabled at
  `chrome://extensions` (see `INSTALL_EXTENSION.md`).
- Confirm you're actually **in** the call (not just on the pre-join
  screen) — detection is based on the "Leave call" button existing, which
  only appears once you've joined. If the extension's icon has no orange
  dot yet, it hasn't detected the call — wait a couple of seconds
  (detection polls every 2s) and try again.
- Open the popup and check the status text/button — if it still says "Not
  recording" with a "Start Recording" button after opening, click that
  button directly.
- Google occasionally changes Meet's page structure, which can break the
  automatic call-detection in `extension/content_script.js` — the manual
  **Start Recording** button in the popup always works regardless, as long
  as you're actually on the meeting tab when you click it.

## The "REC" badge stays red after I leave the call — no documents, no transcript

Recording never actually stopped, so nothing was ever uploaded. This had
two different causes historically, both now fixed (see
`../extension/DESIGN.md` for the full story):
- Three independent signals detect "the call ended" (a DOM check, a
  fallback for the tab/stream closing outright, and a listener for the
  tab navigating away from the meeting URL).
- The bigger one: `background.js`'s recording state used to live in plain
  variables, which Chrome's Manifest V3 service workers silently wipe
  after ~30s of inactivity — so on any meeting longer than that, the
  extension could forget it was even recording by the time you left,
  regardless of whether "call ended" was detected correctly. State now
  persists in `chrome.storage.session` specifically to survive that.

If you still ever see this (e.g. on an old cached copy of the extension
that hasn't been reloaded since this fix):
1. **Reload the extension** at `chrome://extensions` (↻ on the Meeting
   Saathi card) — this bug only reproduces on old code.
2. Click the extension icon and press **Stop Recording** directly in the
   popup to recover whatever was captured — this doesn't depend on any of
   the automatic detection, so it should still work.
3. If it still happens on current code, mention it — there may be another
   variant of this worth investigating.

## Recording finished but nothing shows up at localhost:8420

- Make sure the server was actually running for the **whole** meeting, not
  just at the end — the extension now uploads chunks to
  `http://localhost:8420` continuously *during* the call (see
  `extension/DESIGN.md`), not just once when you leave. If the server
  wasn't running when a chunk tried to upload, that chunk fails (see "A
  banner says a chunk upload failed" above); if it wasn't running for the
  whole meeting, most/all chunks will have failed. Open the extension's
  background page console (`chrome://extensions` → Meeting Saathi →
  "service worker" link → Console tab) to see upload errors.
- Confirm `host_permissions` in `extension/manifest.json` includes
  `http://localhost:8420/*` and you didn't change the server's port without
  also updating `SERVER_BASE_URL` in both `extension/background.js` (used
  for `POST /meetings/start`) and `extension/offscreen.js` (used for the
  per-chunk `/chunk`/`/finalize` uploads).

## A meeting shows status `failed`

Check the error message shown next to it on the status page. Common causes:

- **"401" / "authentication" / "invalid API key"** — your `GEMINI_API_KEY`
  in `.env` is missing, wrong, or the server hasn't been restarted since you
  added it. Restart the server after editing `.env`. Get/check a key at
  https://aistudio.google.com/apikey.
- **"429" / rate limit error** — the free tier has daily/per-minute request
  limits; wait a bit and try again (see
  https://ai.google.dev/gemini-api/docs/rate-limits). A single meeting's
  document generation only needs 4 requests (1 extraction + 3 concurrent
  generate calls), so this is unusual for normal personal meeting volume —
  but the *daily* quota is shared across every meeting (and any manual
  testing/regeneration) that day, so it's possible to hit it on a heavy day.
- **A diarization/pyannote error** (mentions `pyannote` or `huggingface`) —
  you likely haven't set `HUGGINGFACE_TOKEN` yet, or haven't accepted the
  model terms at https://huggingface.co/pyannote/speaker-diarization-community-1 —
  see `SETUP.md`.
- **`failed` appears *during* the meeting, not after** — for the
  chunked/streaming pipeline (what real meetings use now), a `failed`
  state reached while still in `chunk_processing` means a chunk failed to
  process *while the call was still happening*, not a post-meeting issue.
  Whatever was successfully processed up to that point is not recovered
  automatically; check the error message the same way as above.

## Speaker names are missing / everyone is labeled "Speaker 1", "Speaker 2"

The extension tries to match real names automatically, by watching Google
Meet's "who's currently speaking" indicator while it records and lining
that up with the diarization timeline afterward. This is a best-effort
heuristic against Meet's UI (there's no official API for it), so it won't
always work — expect it to occasionally miss a participant, especially:
- during long screen-share stretches (the speaking indicator can
  disappear or move when tiles reflow),
- for participants who never appear on-screen (large meetings virtualize
  off-screen tiles), or
- after a Google Meet UI update, until the extension is adjusted for it.

"Speaker N" for anyone it couldn't confidently match is the intentional,
permanent fallback — not a bug. See `extension/DESIGN.md` and
`app/pipeline/DESIGN.md` for how the matching works. You can still
manually relabel "Speaker N" → the real name afterward in the saved
`transcript.txt`/`.json`, or mention names out loud during the meeting so
Gemini can infer speaker identity from context when generating the
documents.

## My own voice is missing from the recording, only other people are there

## Console shows "Meeting Saathi: microphone unavailable, recording tab audio only"

Both of the above are the same issue: the extension's origin has never
been granted microphone access. This isn't about `meet.google.com`'s own
site permissions — it's the *extension's* permission, and offscreen
documents (where audio capture happens) can't show the permission prompt
themselves, a Chrome restriction (see `extension/DESIGN.md`). Fix:
1. Click the extension's toolbar icon to open the popup.
2. If it shows a red **"Enable Microphone Access"** button, click it — this
   opens a new tab with its own "Grant Microphone Access" button (a real
   tab, not the popup, because the popup can close before Chrome's prompt
   finishes — see `extension/DESIGN.md`). Click that button and allow the
   prompt Chrome shows.
3. That's a one-time step — you can close that tab afterward, and future
   recordings will include your voice without needing to repeat it.

If the popup instead says microphone is *blocked* (you denied it before),
it'll tell you to open `chrome://settings/content/microphone`, find
Meeting Saathi in the blocked list, and switch it to Allow.

## A banner says a chunk upload failed / part of the meeting may be missing

The extension now uploads the recording in ~50-second chunks *during* the
call (see `extension/DESIGN.md`) instead of one file at the end, so a
transient network/server hiccup can affect an individual chunk instead of
the whole meeting. Each chunk retries automatically up to 3 times
(1s/3s/9s backoff) before this banner appears — by the time you see it,
retries are already exhausted. **v1 limitation:** that chunk's ~50 seconds
of audio is not recovered — there's no local replay queue yet, so the
final transcript/documents will have a gap for that stretch. This doesn't
stop the rest of the meeting from being recorded and processed normally;
only the failed chunk's segment is affected.

## The documents look wrong / made something up

Open `transcript.txt` in that meeting's folder and compare it against what
the document says — the documents are supposed to be strictly grounded in
the transcript. If you spot an invented fact, that's worth flagging so the
prompts can be tightened; in the meantime you can:
```bash
python scripts/regenerate_docs.py "/path/to/that/meeting/transcript.json"
```
to try generating again without re-processing the whole recording.

## "python3-venv" / virtual environment errors during setup

You don't need a virtual environment — dependencies were installed with
`pip3 install --user`, which doesn't require one. Ignore any instructions
that assume a `.venv` unless you specifically set one up yourself.

## Something else

Run the test suite to check the core code itself is healthy:
```bash
cd /home/enjay/projects/meeting-saathi
pytest
```
If tests pass but you're still stuck, describe exactly what you did and
what happened, and share the error message from the status page, terminal,
or the extension's service-worker console.
