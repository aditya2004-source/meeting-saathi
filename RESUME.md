# Resume Notes — Meeting Saathi

Both work streams from this session are now complete and verified. This
file is kept as a record of what was done and why, not as an active task
list.

## Stream A — Pipeline speed (65-min meeting → docs in under 5 min)

**Status: done**, with one permanently-external caveat (see below).

- Verified all 5 requested optimizations were already implemented
  (streaming/chunked transcription, concurrent diarize+transcribe per
  chunk, concurrent 3x Gemini generate calls, `BatchedInferencePipeline`,
  DOM-primary diarization replacing pyannote as the common case).
- Real measured numbers obtained and written into `docs/ARCHITECTURE.md`'s
  new "Performance" section: PDF render 8.21s→3.71s (2.21x, real Playwright
  calls); chunked DOM-primary diarization 582.8s total/7.38s per chunk
  average across a synthesized 65.7-min/79-chunk recording (effectively
  zero backlog left at meeting-end); pyannote-fallback cost sampled at
  ~61.0s average per ~50s chunk, flagged as a likely CPU thread-
  oversubscription artifact worth revisiting if that branch fires often in
  real usage.
- **Permanently blocked, not fixable this session**: real Gemini docgen
  call timing — the configured API key's free-tier daily quota (20
  requests/day) was exhausted, confirmed via direct test, not a transient
  rate limit. Documented honestly in `docs/ARCHITECTURE.md` as "not yet
  re-measured" rather than guessed. Re-run once the quota resets if an
  exact number is needed.
- Docs updated: `docs/ARCHITECTURE.md` (new Performance section + fixed two
  stale statements), `app/pipeline/DESIGN.md` (new stage-duration-history
  subsection).

## Stream B — Two new features

**Status: done.** Both features fully implemented, tested (31 new/changed
tests, 72/72 total passing), and manually smoke-tested against a live
`uvicorn` server (verified real HTTP responses for `chunk_processing` /
`generating_docs` / `saved` states, the "no audio captured" warning, and a
computed ETA from persisted history).

### Feature 1 — Speaker names always real, never a bare "Speaker N"

Root cause was `app/orchestrator_streaming.py` never calling
`resolve_speaker_names()` at all (only the legacy `orchestrator.py` did) —
fixed by adding that call plus a new terminal guarantee,
`fill_unresolved_with_excerpts()`, to **both** orchestrators.

- `app/pipeline/speaker_names.py`: new `is_placeholder_speaker()`,
  `fill_unresolved_with_excerpts()` (excerpt-quote fallback label, one
  shared label per "Speaker N" cluster, one independent label per
  "Unknown" segment); hardened `resolve_speaker_names()` to only group
  placeholder-shaped labels (so a real DOM-resolved name is never re-voted
  on); added a staleness cap to `speaker_from_dom_events()`
  (`settings.speaker_event_max_staleness_seconds`, default 300s).
- `app/pipeline/diarize.py`: `diarize_chunk()`'s pyannote-fallback branch
  now tags its "Speaker N" labels with the chunk's start offset
  (`"Speaker 1 (chunk@123.4)"`) — found and fixed a real bug where two
  different fallback chunks' independently-numbered clusters would
  otherwise collide under the same literal string once accumulated across
  the whole meeting.
- `app/docgen/extract_prompt.py` / `generate_prompt.py`: told Gemini to
  copy an excerpt-shaped label verbatim, never paraphrase or invent a real
  name for it.
- Tests: `tests/test_speaker_names.py` (new cases for all of the above),
  new `tests/test_orchestrator_streaming_finalize.py` (end-to-end
  finalize_run() wiring test — the kind of test that would have caught the
  original missing-call bug).

### Feature 2 — Live post-meeting processing status

`GET /meetings/{run_id}/status` already existed and already returned live
`timing.json`; the actual gap was that nothing in the browser called it.

- New `app/progress.py`: pure `describe_progress()` mapping DB state +
  timing + `chunk_durations.json` + persisted history into
  `{label, percent, eta_seconds, recorded, completed, folder_path}`.
  Percent/ETA only shown for the bounded post-meeting tail (no fabricated
  countdown while the meeting is still live); "recorded" is a real signal
  from `chunk_durations.json` (empty *after* the meeting ended = genuine
  "no audio captured" warning, empty *while* still live = no verdict yet).
- New `app/pipeline/timing.py` functions `record_stage_durations()` /
  `load_stage_history()`: tiny persisted `data/stage_duration_history.json`
  (incremental mean, not raw samples) so the ETA is projected from real
  historical per-stage timing instead of guessed — both orchestrators now
  call this right before deleting their run's `timing.json`.
- `app/main.py`: wired `describe_progress()` into both `GET /` and
  `GET /meetings/{run_id}/status`.
- `app/web/static/status.js` (first JS file in the project): 3s polling,
  in-place DOM updates, drops the old blunt 10s full-page meta-refresh;
  `app/web/templates/index.html` restructured to match; extension
  badge/popup considered and ruled out (popups die on close, badge has ~4
  chars).
- Tests: new `tests/test_progress.py`, new `tests/test_timing_history.py`.
- Docs: `app/DESIGN.md` (new "Live processing status" section),
  `docs/ARCHITECTURE.md` (new short pointer section).

### Verification performed

- `python3 -m pytest -q` → 72/72 passing (was 41 before this session).
- Started a real `uvicorn` server and exercised `GET /` and
  `GET /meetings/{id}/status` against manually-crafted run states,
  confirming: correct per-document "Generating X, Y..." labels, correct
  25%/88%/100% tail percentages, a real computed ETA once history existed,
  the "no audio captured" warning firing only once a live meeting had
  actually ended with zero chunks, and the completed-run JSON/HTML shape
  unchanged for pre-existing real "saved" runs already in the database.
- No test/manual-verification artifacts were left behind (temporary DB
  rows, working dirs, and the history file created during smoke-testing
  were all cleaned up).

### Not done / explicitly out of scope this session

- Gemini docgen call timing (external quota block, see Stream A).
- No live end-to-end test with a real Google Meet recording + real Gemini
  calls (blocked by the same quota exhaustion) — everything above was
  verified with real code paths and fakes/manual DB state instead.
