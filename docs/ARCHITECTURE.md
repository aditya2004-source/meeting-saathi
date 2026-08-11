# How It's Built

This is a short technical index — for using the tool day-to-day, see
`USAGE.md` instead. For *why* each component works the way it does (the
internal decisions), see the linked `DESIGN.md` next to that component's
code; this page stays at the whole-system level so those decisions live
in one place, next to the code they describe, instead of being duplicated
here and drifting out of sync.

## Pipeline overview

The original design did all processing *after* the meeting ended, as one
whole-file upload. Diarization's dominant cost (pyannote's clustering
stage) is superlinear in audio duration, not just linear — confirmed by
tracing the pipeline's own stages — so that design took over three hours
end-to-end for a real 65-minute meeting. The current design instead
processes short (~50s) audio chunks **during the call**, so only the last
short chunk is left to process once the meeting ends:

```
DURING THE CALL
Chrome extension (content script watches meet.google.com for "Leave call"
button = in-call signal, plus a best-effort "who's speaking" observer
while recording)
   -> detected call arms the extension icon (badge + tooltip); one click on
      it is required by Chrome to actually start tabCapture (see
      extension/DESIGN.md) -- everything else here stays automatic
   -> POST /meetings/start (once, before capture) -> gets a run_id
   -> offscreen document captures tab audio (other participants) + mic
      audio (you) via Web Audio API, mixes them, and cycles MediaRecorder
      on a fixed interval -- each stop/start cycle yields one
      self-contained, independently-decodable chunk
   -> each chunk POSTed to /meetings/{run_id}/chunk as it's produced,
      transcribed + diarized on the server immediately (not queued for
      later) -- see "Chunked diarization" below
AFTER THE CALL (only the last short chunk's worth of work left)
   -> final chunk POSTed to /meetings/{run_id}/finalize
   -> once every chunk has finished processing, the accumulated
      speaker-labeled segments are assembled into transcript.json (the
      durable backup) and the rest proceeds the same as the legacy path
      below: Gemini document generation -> PDF -> save
```

**Chunked diarization** (`app/pipeline/diarize.py:diarize_chunk`,
DOM-primary + pyannote fallback): each chunk's "who said what" comes
primarily from the extension's Meet active-speaker-tile timestamps
(near-zero cost, no audio ML at all), falling back to running pyannote on
*that chunk only* (bounding its quadratic clustering cost to ~50s of audio
instead of the whole meeting) when that signal is too sparse for a given
stretch (screen-share, off-screen/virtualized participants). See
`app/pipeline/DESIGN.md` for the coverage-ratio decision in detail.

A **legacy whole-file path** (`POST /meetings/upload` ->
`orchestrator.py` -> `diarize()`) still exists unchanged, as the
manual/testing fallback described in `app/DESIGN.md` — not the path real
meetings use anymore, but useful for processing an existing recording
without a live call.

Both paths converge on the same tail once a transcript is assembled:

```
   -> Speaker-name resolution: most segments already carry a real name from
      diarize_chunk()'s DOM-primary fast path (chunked) or are resolved
      here directly (legacy); either way, resolve_speaker_names() gets one
      more pass at whatever's still a placeholder (e.g. a chunk that fell
      into diarize_chunk()'s pyannote-fallback branch), then
      fill_unresolved_with_excerpts() guarantees nothing placeholder-shaped
      survives into the final documents -- see app/pipeline/DESIGN.md
   -> merge -> transcript.json (speaker-labeled, timestamped, the durable backup)
   -> Gemini document generation (Call 1: structured extraction ->
      Call 2 x3, grounded and run CONCURRENTLY: MOM, Requirement Gathering
      Sheet, Discussion+Action-Points)
   -> Markdown -> PDF (Playwright + system Chrome, 3 renders run CONCURRENTLY)
   -> Filesystem writer: "<Meeting Title> - <YYYY-MM-DD HHmm>/" folder,
      written atomically
```

## Component documentation

| Component | Covers | Doc |
|---|---|---|
| `extension/` | Call detection, audio capture/mixing, message flow, speaker-name detection | [`extension/DESIGN.md`](../extension/DESIGN.md) |
| `app/pipeline/` | Transcription, diarization, alignment, speaker-name resolution, transcript building | [`app/pipeline/DESIGN.md`](../app/pipeline/DESIGN.md) |
| `app/docgen/` | The two-call Gemini prompting pattern, the Requirement Gathering Sheet's structured schema, PDF rendering | [`app/docgen/DESIGN.md`](../app/docgen/DESIGN.md) |
| `app/` (core) | Web layer, legacy + chunked orchestrators, state machine, config, filesystem writer | [`app/DESIGN.md`](../app/DESIGN.md) |

## Code layout

```
extension/                 The Chrome extension (Manifest V3)
├── DESIGN.md
├── manifest.json           Permissions, content script registration
├── content_script.js       Detects call start/end + who's speaking on meet.google.com
├── background.js           Service worker: coordinates start/stop, badge, speaker timeline
├── offscreen.js             Actual audio capture + mixing + upload
├── offscreen.html
├── popup.html / popup.js    Manual start/stop fallback UI

app/
├── DESIGN.md
├── main.py                 FastAPI web server: status page, legacy /meetings/upload,
│                            and the chunked /meetings/start|{id}/chunk|{id}/finalize trio
├── config.py                Reads .env into a typed Settings object
├── db.py                     SQLite: one row per meeting, tracks its state
├── orchestrator.py          Drives one whole-file upload through the legacy pipeline
├── orchestrator_streaming.py Drives the chunked pipeline (accumulate chunks -> finalize)
├── chunked_state.py          In-memory per-run bookkeeping for in-flight chunks
├── pipeline/
│   ├── DESIGN.md
│   ├── download.py       Working-directory helper
│   ├── transcribe.py     Wraps faster-whisper (BatchedInferencePipeline)
│   ├── diarize.py        "Who said what": legacy diarize() (pyannote, whole file)
│   │                      and diarize_chunk() (DOM-primary, pyannote fallback per chunk)
│   ├── speaker_names.py  "Speaker N" -> real name; DOM-coverage + DOM-primary segment building
│   ├── merge.py          Combines everything into transcript.json
│   └── timing.py          Per-stage timing, surfaced on the status page while a run is in progress
├── docgen/
│   ├── DESIGN.md
│   ├── extract_prompt.py     Gemini prompt: pull structured facts from transcript
│   ├── generate_prompt.py    Gemini prompts: write MOM / Requirement Gathering Sheet / Action Points
│   ├── render_tables.py       Renders the Requirement Gathering Sheet's rows to a markdown table
│   ├── engine.py               Runs those Gemini calls (the 3 generate calls run concurrently)
│   └── render_pdf.py           Turns the generated Markdown into PDF (3 renders run concurrently)
├── storage.py            Builds the "<Title> - <date>" folder, writes files safely
└── web/                   The HTML/CSS for the status page

scripts/
├── regenerate_docs.py     Re-run docgen+render from an existing transcript.json
└── benchmark_pipeline.py  Before/after timing: legacy whole-file vs. chunked pipeline
```

## State machine

Each meeting run moves through these states (visible on the status page),
stored in `data/runs.sqlite3`. Legacy whole-file path:

```
idle -> received -> transcribing -> diarizing -> generating_docs
     -> rendering -> saving -> saved
```

Chunked/streaming path (what real meetings use now) instead goes through
one state, `chunk_processing`, for the whole in-call chunk-accumulation
period:

```
idle -> received -> chunk_processing -> generating_docs
     -> rendering -> saving -> saved
```

(or `failed`, with an error message, at any step, either path)

## Live processing status

`GET /` and `GET /meetings/{run_id}/status` (`app/main.py`) both surface a
live, auto-updating status per meeting — no manual reload needed. This
answers, at a glance, right after the extension leaves a call: was audio
actually captured, which stage is processing in now (down to which of the
3 concurrent Gemini documents are still generating), an ETA for the
bounded post-meeting tail, and the exact saved folder path the moment it's
done. See `app/DESIGN.md`'s "Live processing status" section for the full
design (`app/progress.py`'s `describe_progress()`, the polling
`app/web/static/status.js`, and the persisted cross-run stage-duration
history that makes the ETA real rather than guessed).

## Performance: before vs. after the chunked redesign

Measured on this development machine (4 physical cores, CPU-only —
absolute numbers will vary by hardware, but the *shape* of the improvement
— moving the dominant cost off the post-meeting critical path entirely —
is what matters here).

**Before** (legacy whole-file path, traced stage-by-stage on a real
65-minute meeting): **over three hours end-to-end**, dominated by
pyannote's clustering stage, whose cost is superlinear in audio duration.

**After** (chunked/streaming path), measured with
`scripts/benchmark_pipeline.py`'s approach on a synthesized 65.7-minute
recording (79 chunks of ~50s each):

| Stage | Before (legacy) | After (chunked) |
|---|---|---|
| Transcription + diarization | Sequential, after the call ends; pyannote's clustering cost scales with the *whole meeting's* length | **During the call**, per chunk. DOM-primary fast path (no pyannote): **582.8s total / 7.38s per chunk average** across all 79 chunks — comfortably finishes before the next ~50s chunk arrives, i.e. **effectively zero backlog left when the meeting ends** |
| Diarization fallback (sparse DOM coverage) | N/A (always ran pyannote on the whole file) | Only runs on chunks with poor DOM coverage. **Confirmed in real production usage** (a real 7-minute solo test call, 0% DOM coverage on every chunk — expected for a solo call with no other participant tile): the earlier-suspected CPU thread-oversubscription was real and severe, not just a sampling artifact — local pyannote took **630.7s for one ~50s chunk** (embeddings substage alone: 575.1s). Mitigated two ways: (1) `app/orchestrator_streaming.py` now caps concurrent chunk processing at 2 (was unbounded), and `whisper_cpu_threads`/`pyannote_torch_threads` dropped from 4→2 each, keeping in-flight threads closer to this box's 4 real cores; (2) optionally, setting `ASSEMBLYAI_API_KEY` routes this fallback branch to a single paid AssemblyAI call (transcription + diarization together, ~$0.17/hour of audio) instead of local pyannote — **measured on this same real recording: 10.8s vs. pyannote's 630s, a 58x speedup**, since it's offloaded off this box's CPU entirely. See `app/pipeline/diarize.py`'s `diarize_chunk()`. |
| Gemini document generation (extract + 3x generate) | Sequential | Concurrent (`ThreadPoolExecutor(max_workers=3)`) — not yet re-measured with a real API call after this redesign (see caveat below) |
| PDF rendering (3 documents) | Sequential | **Reverted back to sequential.** A concurrent version (`ThreadPoolExecutor(max_workers=3)`, each thread independently launching Chromium via Playwright's sync API) was measured at 8.21s → 3.71s (2.21x) — but Playwright's sync API is not safe to call from multiple threads in one process, and this design caused a real production crash (`RuntimeError: Racing with another loop to spawn a process.`, from the shared asyncio subprocess child watcher). The earlier clean benchmark was never real evidence of safety, since races like this are inherently intermittent. See `app/docgen/render_pdf.py`'s `render_documents_to_pdf()` docstring. |

**Net effect**: since transcription+diarization now happens *during* the
call instead of after it, the post-meeting wait for a 65-minute meeting is
bounded by the tail alone (Gemini docgen, still concurrent, + now-sequential
PDF rendering) — the exact number depends on Gemini's own response latency
(network-bound, outside this codebase's control) and Gemini call latency
wasn't re-measured after this redesign (the configured API key's free-tier
daily quota was exhausted at benchmark time — re-measure once it resets).

**Known caveat, not yet root-caused:** most chunks in the DOM-primary
benchmark reported zero transcribed segments, likely because the
synthesized/looped test audio was too repetitive/quiet for Whisper's own
voice-activity detection (not a pipeline bug — the *timing* measurement
itself still holds regardless of segment count) — worth a sanity check
against a real recording before treating the 7.38s/chunk figure as final.

## What's deliberately not built (out of scope for this design)

- A separate bot identity that joins meetings unattended — ruled out when
  the third-party bot vendor was dropped in favor of the Chrome extension.
- Auto-detecting the meeting title from Google Calendar.
- Support for Zoom/Teams (the extension only runs on `meet.google.com`).
- Running more than one meeting recording at a time.
- A guaranteed real-name mapping for every participant — speaker-name
  resolution is explicitly best-effort (see `extension/DESIGN.md`). What
  *is* guaranteed: a bare, meaningless "Speaker N"/"Unknown" never reaches
  a final document. When a name can't be confidently matched, the accepted
  fallback is a label quoting a few lines of that person's own transcript
  (`Unidentified speaker ("...")`, see `app/pipeline/DESIGN.md`'s
  `fill_unresolved_with_excerpts()`), so a human can manually identify them
  from the document itself.
