# `app/pipeline/` — audio → speaker-labeled transcript

Turns one uploaded mixed-audio recording into `transcript.json`, the
durable backup and the input to document generation
(`../docgen/DESIGN.md`).

## Stage order

```
download.py (working dir)
  -> transcribe.py (faster-whisper: audio -> timestamped text segments)
  -> diarize.py    (pyannote.audio: audio -> speaker turns,
                     aligned to transcribe.py's segments by time overlap)
  -> speaker_names.py (optional: "Speaker N" -> real name, from the
                        extension's best-effort speaking-indicator timeline)
  -> merge.py       (segments -> transcript.json + plain-text rendering)
```
`orchestrator.py` (`../DESIGN.md`) drives this sequence and owns all
file I/O around it; everything in this directory is intentionally a pure
transform over in-memory data (or, for `transcribe`/`diarize`, over a
single audio file path) so it stays unit-testable without a live meeting.

## Why local transcription and diarization

Two constraints drove this: meeting audio content should stay on the
user's own machine (a stated privacy preference), and document generation
should be the *only* external API call in the whole system (currently
Gemini's free tier — see `app/DESIGN.md`). `faster-whisper` (CPU) and
`pyannote.audio` (free HuggingFace model + token) both satisfy that.
Python was chosen as the single backend language specifically because both
of these are Python-native with heavy C++/PyTorch bindings — shelling out
from another language would be more awkward than just being one Python
process.

`diarize.py` decodes audio via PyAV rather than pyannote 4.x's default
`torchcodec` path, because `torchcodec` needs system `ffmpeg` shared
libraries this machine doesn't have installed, and PyAV is already a
`faster-whisper` dependency that's confirmed working here.

**`_pyannote_turns` reads `output.exclusive_speaker_diarization`, not the
pipeline's return value directly.** pyannote 4.x's
`speaker-diarization-community-1` pipeline returns a `DiarizeOutput`
dataclass (`speaker_diarization`, `exclusive_speaker_diarization`,
`speaker_embeddings`), not an `Annotation` the way older pipelines did —
calling `.itertracks()` on the wrapper itself raises `AttributeError`
(hit in testing, once a real meeting finally got far enough to reach this
step). `exclusive_speaker_diarization` — non-overlapping turns — is used
specifically because pyannote's own docs recommend it for "downstream
transcription," which is exactly this alignment step (Whisper's segments
are non-overlapping too). Guarded by
`tests/test_diarize_alignment.py:test_pyannote_turns_reads_exclusive_speaker_diarization`.

## Alignment (`_align`/`_overlap` in `diarize.py`)

pyannote produces speaker *turns* (time ranges); Whisper produces
*segments* (timestamped text). Each transcribed segment is attributed to
whichever pyannote turn it overlaps the most (`_overlap` = max(0,
intersection)); a segment with zero overlap against every turn is labeled
`"Unknown"` rather than guessed. Raw pyannote labels (`SPEAKER_00`,
`SPEAKER_01`, ...) are remapped to `"Speaker 1"`, `"Speaker 2"`, ... in
order of first appearance, purely for readability — these placeholder
names are what `speaker_names.py` operates on next.

## Speaker-name resolution (`speaker_names.py`)

**A generated document must never show a bare `"Speaker N"`/`"Unknown"`
label** — that's the hard requirement this section satisfies. `"Speaker
N"` is still a normal, expected *intermediate* state right after
diarization; it's just never allowed to survive all the way to
`build_transcript()`. Two functions cooperate to guarantee that:

`resolve_speaker_names()` — best-effort resolution against the extension's
`speaker_events.json` sidecar (see `../../extension/DESIGN.md` for how that
timeline is captured — it's a best-effort DOM heuristic with real
fragility, not a solved problem):

1. Groups segments by their current placeholder label — but **only**
   labels that are still placeholder-shaped
   (`is_placeholder_speaker()`: `"Speaker N"`, its chunk-tagged form
   `"Speaker N (chunk@123.4)"` — see below — or `"Unknown"`). A segment
   that already carries a real name (the common case for the chunked
   pipeline, resolved per-chunk by the DOM-primary fast path before this
   function ever runs) is left completely untouched, never folded into a
   group and re-voted on. `"Unknown"` segments are also never grouped —
   they aren't reliably tied to one diarization cluster, so name-resolving
   them would compound uncertainty rather than reduce it.
2. Per group, collects every speaker-event whose timestamp falls within
   `tolerance_seconds` (default 1.0s) of any segment in that group, and
   majority-votes a name.
3. Only accepts the vote if the winning name's share of that group's
   matched events is ≥ `min_confidence` (default 50%) — otherwise the
   group keeps its placeholder rather than risk a noisy mislabel.
4. **Collision rule:** if two different placeholder groups would resolve
   to the *same* real name, only the more strongly-voted group is actually
   renamed; the other keeps its placeholder. Letting two diarization
   clusters both claim one name would misattribute someone's words in the
   final documents — worse than an honest `"Speaker N"`.

`fill_unresolved_with_excerpts()` — the **terminal guarantee**, called by
both orchestrators right after `resolve_speaker_names()` (unconditionally,
even when no `speaker_events.json` exists at all): replaces every segment
whose speaker is still placeholder-shaped with a label quoting 2-3 lines of
that speaker's own transcript (`Unidentified speaker ("...")`), so a human
reading the final document can manually identify who it was instead of
seeing a meaningless `"Speaker 2"`. A `"Speaker N"` group shares ONE
excerpt label (it's one real diarization cluster — one person); each
`"Unknown"` segment gets its OWN independent excerpt label, since
`"Unknown"` segments aren't reliably the same person from one to the next.
`generate_prompt.py`'s grounding rule and `extract_prompt.py` both
explicitly tell Gemini to copy an excerpt-shaped label verbatim rather than
paraphrasing or inventing a plausible real name for it.

**Why `orchestrator_streaming.py` needed a fix, not just `orchestrator.py`:**
the legacy whole-file path always called `resolve_speaker_names()`, but the
chunked/streaming path — the one real meetings actually use — did not; any
chunk that fell into `diarize_chunk()`'s pyannote-fallback branch produced a
placeholder that never got a second chance to resolve against the *full*
accumulated `speaker_events.json`, even though it's sitting right there at
`finalize_run()` time. This was the direct cause of speaker-name resolution
appearing to "work sometimes, not others" — now fixed by calling both
functions in `finalize_run()`, exactly mirroring `orchestrator.py`.

**Why `diarize_chunk()`'s fallback branch tags its placeholders:**
`_align()` mints `"Speaker 1"`, `"Speaker 2"`, ... fresh on every call,
scoped only to whatever pyannote turns it was given. For the legacy
whole-file path that's fine (one call, one meeting). For the chunked path,
each fallback chunk calls `_align()` independently — chunk 5's `"Speaker
1"` and chunk 39's `"Speaker 1"` are almost certainly *different people*
who each happened to speak first within their own chunk. Since
`resolve_speaker_names()` groups segments by their exact placeholder string
across the *whole* accumulated transcript, that collision would silently
merge two unrelated people into one resolved (wrong) name. `diarize_chunk()`
tags each fallback chunk's placeholders with that chunk's (globally unique)
start offset — `"Speaker 1 (chunk@123.4)"` — so grouping only ever pools
segments from the same chunk's own cluster.

**Staleness cap on the DOM-primary fast path:** `speaker_from_dom_events()`
attributes a segment to the nearest *preceding* DOM event, carried forward
indefinitely by default — if the extension's `MutationObserver` stalls
(backgrounded tab, an unrecognized Meet UI change), that stale event would
otherwise keep confidently mislabeling everyone as whoever spoke last
before the stall, for the rest of the meeting. Past
`settings.speaker_event_max_staleness_seconds` (default 300s) without a
fresher event, attribution reverts to `"Unknown"` instead — which
`fill_unresolved_with_excerpts()` then turns into an honest excerpt label.

All of this is deliberately pure functions (`parse_speaker_events`,
`resolve_speaker_names`, `fill_unresolved_with_excerpts`,
`is_placeholder_speaker`, no I/O) — see `tests/test_speaker_names.py` for
the behavior contract and `tests/test_orchestrator_streaming_finalize.py`
for the end-to-end wiring test that would have caught the missing
`resolve_speaker_names()` call.

## Chunked/streaming diarization (`diarize_chunk`, DOM-primary + pyannote fallback)

`diarize()` (above) is the **legacy whole-file path** — still exactly as
written, still used by the manual/testing `POST /meetings/upload` route
(`../DESIGN.md`). It has one real problem: pyannote's clustering stage
(`scipy.cluster.hierarchy.linkage` + a VBx EM loop over every embedding in
the file) is superlinear in audio duration, not linear like the
segmentation/embedding stages that precede it — confirmed by tracing the
pipeline's own stages, not assumed. That's *why* whole-file processing of a
65-minute meeting took over three hours end to end on this CPU-only
machine, and why it can't just be "made faster" with better threading
alone: the bottleneck grows worse than proportionally with meeting length.

`diarize_chunk()` exists to sidestep that, for the chunked/streaming
pipeline (`../orchestrator_streaming.py`) that processes short (~50s)
audio chunks *during* the call instead of one file after it:

1. Check what fraction of the chunk's time window is covered by the
   extension's Meet active-speaker-tile timestamps (`speaker_events.json`,
   see `speaker_names.py` below and `../../extension/DESIGN.md`) —
   `dom_coverage_ratio()`. This check costs nothing (no audio touched at
   all) and happens *before* transcribing, so the common case (good
   coverage) never runs pyannote.
2. If coverage is at or above `settings.chunk_pyannote_coverage_threshold`
   (default 0.5), segments are built directly from Whisper's transcribed
   segments + the nearest-preceding DOM event's name
   (`speaker_from_dom_events()`) — no audio-based diarization model runs at
   all for this chunk.
3. If coverage is too sparse (screen-share, off-screen/virtualized
   participant tiles — the same caveats `extension/DESIGN.md` already
   documents for this signal), pyannote runs on *this chunk only*,
   concurrently with `transcribe()` (both are independent, audio-only
   operations — the same argument as `diarize()`'s own sequential
   transcribe-then-diarize could have been parallelized, just applied here).
   Because the chunk is short, pyannote's quadratic clustering cost is
   bounded to that chunk's duration, not the whole meeting's.

Returned segments always carry **global** timestamps (`chunk_start_offset`
already added), so chunks accumulate into one ordered transcript with no
further adjustment. Chunk offsets themselves come from *measured* chunk
durations (`probe_duration_seconds()` — demuxes and reads the last packet's
timestamp, since WebM files from `MediaRecorder` don't reliably carry a
duration in the container header, confirmed empirically), not an assumed
nominal chunk length, since `MediaRecorder` restart-cycling doesn't produce
exactly uniform chunks.

## Timing instrumentation (`timing.py`)

`TimingRecorder` accumulates `{stage: seconds}` for one run and flushes it
to `working/<run_id>/timing.json` after every stage (not just at the end),
so a still-in-progress run's timing is visible on the status page while
it's actually happening — the whole point of the chunked redesign is
"during the call," so that needs to be observable during the call too, not
just after. `transcribe()`, `diarize()`/`diarize_chunk()`,
`docgen/engine.py`'s four Gemini calls, and `docgen/render_pdf.py`'s three
PDF renders all accept an optional `recorder` and wrap themselves in
`timed()`. Thread-safe (a `threading.Lock` per recorder) since several
stages now genuinely run concurrently (the three parallel Gemini generate
calls, the three parallel PDF renders, per-chunk transcribe+pyannote, and
potentially several chunks' processing threads at once).

**Cross-run stage-duration history**, also in `timing.py`
(`record_stage_durations`/`load_stage_history`): a run's own `timing.json`
is deleted along with the rest of `work_dir` the moment it reaches
`"saved"`, so there's no per-run history left to estimate a *future* run's
ETA from. Both orchestrators fold their run's bounded post-meeting-tail
durations (`app/progress.py`'s `TAIL_STAGE_KEYS` — extract facts, the 3
generate calls, the 3 PDF renders, save) into a tiny separate
`data/stage_duration_history.json` right before deleting `work_dir`, using
an incremental mean (Welford's method) rather than storing every run's raw
durations — the file stays a few KB forever regardless of how many
meetings have been processed. `app/progress.py`'s `describe_progress()`
reads this to compute an ETA for the current run's remaining tail stages.

## `transcript.json` as the durable backup

`merge.py` builds the structured transcript that becomes the meeting
folder's backup and the sole input to `scripts/regenerate_docs.py` (see
`../docgen/DESIGN.md`) — if the pipeline dies after this stage, the
transcript alone is enough to regenerate every document without
re-transcribing or re-diarizing.
