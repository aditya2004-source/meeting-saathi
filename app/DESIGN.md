# `app/` core — web layer, orchestration, state, storage, config

Everything in `app/` outside `pipeline/` and `docgen/` (which have their
own `DESIGN.md`s): the FastAPI server, the pipeline driver, the state
machine, settings, and the filesystem writer.

## `main.py` — the web layer

Two upload paths hit the same `_start_processing()`:
- `POST /meetings/upload` — the **legacy whole-file path**, kept working
  unchanged as the manual/testing fallback. Used automatically by earlier
  extension versions and by the manual form on the status page. Accepts
  `title`, `audio`, and an optional `speaker_events` JSON field (the
  extension's best-effort speaker-name timeline — see
  `../extension/DESIGN.md` and `pipeline/DESIGN.md`). A malformed
  `speaker_events` value is swallowed silently (`json.loads` wrapped in
  try/except) rather than failing the upload — it's optional enrichment
  data, not something that should ever block saving the recording.
- `POST /meetings/upload-form` — the manual form on the status page, for
  processing an existing recording without the extension (testing/
  fallback). No `speaker_events`, since there's no live call to observe.

The **chunked/streaming path** (what the current extension actually uses)
is a separate trio, additive alongside the two routes above, backed by
`orchestrator_streaming.py` instead of `orchestrator.py`:
- `POST /meetings/start` (`title`) — called once, before any audio
  capture begins, so there's a `run_id` to upload chunks to for the rest
  of the meeting. Returns immediately with the new run's id.
- `POST /meetings/{run_id}/chunk` (`sequence`, `audio`, `speaker_events`)
  — called once per `MediaRecorder` restart-cycle (~50s of audio each,
  see `../extension/DESIGN.md`) during the call. Writes the chunk to
  `working/<run_id>/chunks/<sequence>.webm`, overwrites
  `working/<run_id>/speaker_events.json` with the latest full accumulated
  array (simplest correct approach — small payload, safely idempotent on
  retry), and hands off to a background thread
  (`orchestrator_streaming._process_chunk_then_maybe_finalize`) so a slow
  chunk never blocks the next chunk's upload from being accepted.
- `POST /meetings/{run_id}/finalize` — same shape as `/chunk`, for the
  terminal (usually short) chunk when the meeting ends. Once that chunk
  and everything still in flight finishes, the run proceeds through the
  same docgen → render → save tail the legacy path uses.

CORS is wide open (`allow_origins=["*"]`) because the extension runs as a
`chrome-extension://...` origin, not a normal web origin — it needs to be
allowed to `POST` here from that origin, and this server never listens on
anything but `127.0.0.1`.

Each upload (whole-file or chunk) spawns a background `threading.Thread` —
a meeting recording can be an hour+ of processing, which must never block
the web server's event loop.

## Live processing status (`app/progress.py` + `status.js`)

The requirement: after the meeting ends (and the extension has left the
call), the user needs to see *live*, without a manual reload, whether audio
was actually captured, which stage processing is in, an ETA, and the
final saved folder path the moment it's done.

`app/progress.py`'s `describe_progress()` is the pure function that maps a
run's raw state (`db.STATES` + `timing.json` + `chunk_durations.json` +
the persisted cross-run stage-duration history — see `pipeline/DESIGN.md`)
into `{label, percent, eta_seconds, recorded, completed, folder_path}`.
Both `GET /` (`index()`) and `GET /meetings/{run_id}/status` call the same
`_progress_for_run()` helper in `main.py`, so the page's initial render and
every subsequent poll are always built from identical logic.

A deliberate scope decision: **percent/ETA are only ever shown for the
bounded post-meeting tail** (extract facts → generate 3 docs → render 3
PDFs → save). While a meeting is still live (`chunk_processing`, or the
legacy path's `transcribing`/`diarizing`), duration is genuinely
open-ended — a fabricated countdown there would be actively misleading.
Instead, that phase shows a live "how much audio has been captured so far"
signal computed straight from `chunk_durations.json`, which doubles as the
"was this meeting actually recorded" signal: `chunk_durations` empty/absent
while still live is not evidence of a problem (the first chunk just hasn't
arrived yet), but empty/absent *after* the meeting has ended
(`generating_docs` or later) is a real, actionable "no audio was captured"
signal, surfaced as a warning on the status page.

`GET /meetings/{run_id}/status` already existed before this feature and
already returned live `timing.json` data — the actual gap was that nothing
in the browser ever called it. `app/web/static/status.js` (the first
JavaScript file in this project) is a small vanilla-JS polling loop (3s
interval) that fetches that endpoint per row and updates the DOM in place —
replacing the old blunt `<meta http-equiv="refresh" content="10">`
full-page reload, which flashed the whole page and reset scroll position.
A Chrome extension badge/popup was considered and ruled out: MV3 popups are
destroyed the moment they're closed (can't show live status after the user
has left the call, which is exactly when this matters most), and the badge
only has room for ~4 characters. The existing FastAPI status page, already
mostly there, was the clearly better fit.

## `orchestrator.py` — the legacy whole-file pipeline driver

`_run()` is a straight-line sequence: diarize → (optionally) resolve
speaker names → merge → generate documents → render PDFs → save →
clean up the working directory. `process_recording()` wraps the whole
thing in a top-level `try/except` that calls `db.mark_failed()` on any
exception — a single bad meeting must never crash the server or take down
other runs; a `failed` state with the exception message visible on the
status page is the intended failure mode, not an unhandled 500.

## `orchestrator_streaming.py` — the chunked pipeline driver

Sibling to `orchestrator.py`, not a replacement — shares `merge.py`,
`docgen/engine.py`, `docgen/render_pdf.py`, and `storage.py` completely
unchanged; only transcript *assembly* differs (accumulated per-chunk
`SpeakerSegment`s here, vs. one whole-file `diarize()` call there).

`accept_chunk()` (called from the `/chunk`/`/finalize` routes) writes the
chunk to disk and returns immediately; the actual work
(`_process_chunk()`: measure the chunk's real duration via
`pipeline.diarize.probe_duration_seconds()`, compute its global time
offset, run `diarize_chunk()` — see `pipeline/DESIGN.md`) happens on a
background thread. Per-run in-progress state (which chunks are still
processing, segments accumulated so far, measured chunk durations) lives
in `app/chunked_state.py`'s in-memory `RunChunkState` registry, **not** in
SQLite — accepted v1 gap: a server restart mid-meeting loses in-flight
accumulation for any run not yet `saved`, same category as the
already-documented "server must be running while recording" constraint.
When the `/finalize` chunk's own processing completes,
`_wait_for_pending_then_finalize()` polls until every other in-flight
chunk for that run drains, then calls `finalize_run()` — the same
docgen → render → save tail as the legacy path.

## `db.py` — state machine

One row per meeting in SQLite (`data/runs.sqlite3`), see `db.STATES` for
the full state list: `idle -> received -> transcribing -> diarizing ->
generating_docs -> rendering -> saving -> saved`, or `failed` from
anywhere, for the legacy path; the chunked path instead goes `idle ->
received -> chunk_processing -> generating_docs -> rendering -> saving ->
saved` (`chunk_processing` spans the whole in-call chunk-accumulation
period). Adding `chunk_processing` was a plain new value in the `STATES`
list — `update_run()`'s validation is the only place it's checked against,
so this needed no schema change (see the migration note below).

**Migration note, worth remembering before adding any column:**
`init_db()` uses `CREATE TABLE IF NOT EXISTS`, which does **not** alter an
already-existing table. This machine already has a populated
`runs.sqlite3` from real use — adding a column to `_SCHEMA` alone will
silently do nothing here, and the first `update_run(..., new_column=...)`
call will throw `sqlite3.OperationalError: no such column`. There's no
migration precedent in this codebase yet; any future schema change needs
an explicit `PRAGMA table_info` → `ALTER TABLE ... ADD COLUMN` guard in
`init_db()`, not just a `_SCHEMA` edit. (This is why, e.g., speaker-name
resolution provenance was deliberately *not* added as a DB column — the
transcript's `diarization_source` field plus each segment's speaker label
are sufficient signal without touching the schema.)

## `config.py` — settings

Pydantic `Settings` read from `.env`. The single-external-API constraint
shows up here directly: `gemini_api_key`/`gemini_model` are the only
credentials for document generation, and are specifically for Gemini's
**free tier** (no billing) — switched from Anthropic/Claude at the user's
explicit request, since Claude's API has no meaningful free tier and this
project has a hard "no paid dependency" requirement. `huggingface_token`
is separately free too (diarization model access, not inference billing).
`base_storage_dir` defaults to
`~/Downloads/Meeting Saathi` — chosen over `~/Documents` specifically
because saving to Downloads was an explicit requirement (everything lands
somewhere the user already expects downloaded/generated files to appear,
with no manual save step). Note this default only applies going forward —
folders saved under an old `base_storage_dir` value aren't moved
automatically when the setting changes.

## `storage.py` — the filesystem writer

`save_meeting_folder()` writes to a temp directory under `base_storage_dir`
first, then `shutil.move()`s it into place — a crash mid-write can never
leave a half-written folder where a completed `"<Title> - <date>"` one is
expected. Folder-name collisions (same title, same minute) are resolved by
appending `" (2)"`, `" (3)"`, etc., rather than overwriting.

## Per-run working directory lifecycle

`app/working/<run_id>/` (via `pipeline/download.py:working_dir_for`) holds
everything transient for one run. Legacy path: the uploaded `original.<ext>`
audio and, if the extension sent one, `speaker_events.json`. Chunked path
additionally has `chunks/<sequence>.webm` (one per uploaded chunk) and
`chunk_durations.json` (sequence → measured duration, the append-only
sidecar `orchestrator_streaming.py` uses to compute each chunk's global time
offset). Both paths also get `timing.json` (`pipeline/timing.py`'s
`TimingRecorder` output), flushed incrementally so a still-processing run's
per-stage timing is visible on `GET /meetings/{run_id}/status` and the index
page while it's happening, not just after.

None of this is a backup — `orchestrator._run()` / `orchestrator_streaming.
finalize_run()` both delete the whole directory (`shutil.rmtree`) after a
successful save, which also means `timing.json`/`chunk_durations.json`
naturally disappear once a run reaches `saved` (their purpose was live
visibility during processing, not a permanent record — `describe_progress()`
doesn't need either one for a terminal `saved`/`failed` run). The durable
backup is `transcript.json`/`transcript.txt` inside the meeting's saved
folder, not anything in `working/`.

One exception to "nothing here is a backup": `data/stage_duration_history.json`
(sibling to `data/runs.sqlite3`, not under `working/`) *is* meant to
outlive every individual run — both orchestrators fold their run's
bounded-tail stage durations into it right before deleting `work_dir`, so a
future run's ETA has real historical data to project from. See
`pipeline/DESIGN.md`'s timing-instrumentation section for the incremental-
mean format.
