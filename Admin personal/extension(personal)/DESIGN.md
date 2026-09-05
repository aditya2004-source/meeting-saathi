# `extension/` — Chrome extension (Manifest V3)

What this component does: joins the audio path of a Google Meet call the
user is actually in, detects call start/end, and uploads the finished
recording (plus a best-effort speaker-name timeline) to the local server.
Recording *stopping* and everything after it (upload, transcription,
document generation) is fully automatic; *starting* requires exactly one
click per meeting, on the extension icon, because Chrome's platform
security model requires it — see "Why one click to start is unavoidable"
below before assuming this could be made silent with more code. See
`../docs/ARCHITECTURE.md` for how this fits into the full pipeline.

## `background.js`'s state lives in `chrome.storage.session`, not plain variables

**This was the root cause of the most persistent, hard-to-reproduce bug in
the whole project — recordings that would never stop or upload, no matter
how leave-detection was fixed.** MV3 background service workers are not
persistent: Chrome kills them after ~30s of idle time and restarts them
fresh on the next event, wiping any plain `let`/`const` module-level
variables back to their initial values. A real meeting easily goes longer
than 30s between events reaching `background.js`. If the service worker
gets killed and restarted mid-meeting, it loses its own memory that "tab X
is being recorded" — so when `MEETING_LEFT` (or the `tabs.onUpdated`
listener, or `TAB_STREAM_ENDED`) later fires and calls `stopRecording()`,
it sees `activeTabId === null` (freshly reset) and silently does nothing,
even though the offscreen document is still actually recording (offscreen
documents have their own lifecycle, independent of the service worker's
idle timeout — they're not affected by this). The red "REC" badge appears
to stay on because Chrome persists badge text itself, decoupled from the
script's own (wiped) memory of what's happening — which is exactly why
this looked like a leave-*detection* bug for several rounds of fixes, when
it was actually a state-*persistence* bug.

Fix: `activeTabId`, `currentTitle`, `pendingTabId`, `pendingTitle`,
`recordingStartedAtMs`, `speakerEvents`, and (since the chunked/streaming
redesign) `runId` all live in `chrome.storage.session`
(`getState()`/`setState()`), read fresh at the start of every handler,
instead of module-level variables. `runId` needs the same treatment as
everything else here — it's obtained once from `POST /meetings/start`
before capture begins and has to survive for the whole meeting, easily
outlasting several service-worker restarts, since `offscreen.js` needs it
on every chunk upload. Session-scoped
storage was chosen specifically because it matches how long "currently
recording" should ever be considered true (cleared when the browser
closes, exactly like the old in-memory variables were meant to behave,
just actually surviving a service-worker restart in between).
`creatingOffscreenDocument` is the one exception, deliberately left as a
plain variable — it's only a same-tick concurrency guard, not meaningful
across a restart (worst case, a redundant `createDocument()` call that
Chrome rejects harmlessly).

## Why a Chrome extension, not a cloud "bot" service

A vendor like Recall.ai would join meetings unattended and hand back
per-participant audio + identity for free, but it's an extra paid
dependency. This project was built with a hard requirement of no paid
dependency at all (originally "just one paid API," later tightened to
"zero" when document generation moved to Gemini's free tier — see
`../app/DESIGN.md`), so recording had to happen locally instead — the
trade-off is that a real Chrome window with this extension installed has
to actually be in the call. This was a deliberate change from an earlier
version of the project that used a cloud bot vendor.

## Files and message flow

- **`content_script.js`** — runs on `meet.google.com`. Polls every 2s for
  call start/end (`isInCall()`), and while a recording is active, observes
  Meet's DOM for who's currently speaking (`findActiveSpeakerName()` /
  `startSpeakerObserver()`). Sends `MEETING_JOINED` / `MEETING_LEFT` /
  `SPEAKER_ACTIVE` to the background service worker; renders a small
  banner on `SARATHI_*` messages coming back, including the "click the icon
  to start recording" prompt shown the moment a call is detected.
- **`background.js`** — MV3 service worker. Owns the recording state
  (`activeTabId`/`currentTitle` for the active recording,
  `pendingTabId`/`pendingTitle` for a call that's been detected but not
  yet armed, the speaker-event accumulator, `runId`) — persisted in
  `chrome.storage.session`, not plain variables, for a reason that matters
  a lot (see the dedicated section below) — and coordinates the offscreen
  document. Calls `POST /meetings/start` itself, before telling
  `offscreen.js` to start capturing, so there's already a `run_id` to hand
  over. Also handles `ARM_RECORDING`/`MANUAL_START`/`MANUAL_STOP`/
  `GET_STATUS`/`GET_SPEAKER_EVENTS_SNAPSHOT` from the popup/offscreen.
- **`offscreen.js`** — the only place that can call
  `getUserMedia`/`MediaRecorder` under MV3. Captures + mixes audio, and
  since the chunked/streaming redesign, cycles `MediaRecorder` on a fixed
  interval (`CHUNK_INTERVAL_MS`, ~50s) rather than running one long session
  — each stop/start cycle produces one self-contained, independently
  decodable chunk (individual `ondataavailable` blobs from a single
  session aren't independently decodable — only the first carries the
  container's init segment), immediately `fetch`-uploaded to `POST
  /meetings/{run_id}/chunk` (or `/finalize` for the terminal cycle) rather
  than accumulated into one whole-meeting blob.
- **`popup.html`/`popup.js`** — this is where the required "click to
  start" gesture actually lands. Opening the popup auto-arms recording if
  a call was detected (`ARM_RECORDING`, see below) — no second click
  inside the popup needed. Also has a manual Start/Stop button + title box
  as a fallback for whenever call-detection itself misses.

Full message map (all via `chrome.runtime.sendMessage`, `target:
"offscreen"` distinguishes background→offscreen messages from
content-script/offscreen→background ones):

| Message | Direction | Purpose |
|---|---|---|
| `MEETING_JOINED` / `MEETING_LEFT` | content → background | call start/end signal; joining "arms" the tab (badge + tooltip), doesn't start capture yet |
| `SPEAKER_ACTIVE` | content → background | one speaker-timeline datapoint |
| `ARM_RECORDING` | popup → background | fired the instant the popup opens; consumes the armed tab and starts capture — this *is* the one required click |
| `MANUAL_START` / `MANUAL_STOP` / `GET_STATUS` | popup → background | manual fallback UI + status polling |
| `START_RECORDING` / `STOP_RECORDING` | background → offscreen | actual capture control; `START_RECORDING` now carries `runId` (obtained by background.js from `POST /meetings/start` beforehand) |
| `GET_SPEAKER_EVENTS_SNAPSHOT` | offscreen → background | pulled right before every chunk/finalize upload, so each chunk carries the current accumulated speaker-name timeline as of that moment |
| `CHUNK_UPLOAD_FAILED` | offscreen → background | a chunk exhausted its upload retries; relayed to the tab as a banner (see "Chunk upload durability" below) |
| `SARATHI_RECORDING_STARTED` / `_FAILED` / `SARATHI_UPLOAD_DONE` / `_FAILED` / `SARATHI_CHUNK_UPLOAD_FAILED` | background → content | on-page banner + gates the speaker observer |

## Call-detection heuristics (`isInCall()`)

Three independent, redundant signals rather than one exact string/selector
match, because Meet's DOM/labels change over time and a single wrong guess
means recording silently never starts:
1. A control with an aria-label containing "leave" (covers "Leave call" /
   "Leave the call" wording changes).
2. A live call-duration timer element (`12:34` / `1:02:03` format) — Meet
   only renders this once you're actually in a call.
3. A real meeting-code URL, past the pre-join "Ready to join"/"Join now"
   screen, with mic/camera controls present.

The manual Start/Stop button in the popup exists specifically as the
fallback for whenever all three miss.

**`hasLeftMeetingScreen()` is checked first, as a hard override.** Meet's
post-call screen ("You've left the meeting", with a "Rejoin" option)
briefly stays on the same URL as the call before auto-redirecting (see
below) — during that window it can still show a mic/camera preview tile,
which satisfies Signal 3 above and gets misread as still being in the
call. `hasLeftMeetingScreen()` matches that screen's text ("you've left
the meeting" / "return to home screen") and forces `isInCall()` to `false`
regardless of what the other signals think.

**A third, independent layer lives in `background.js`'s `chrome.tabs.
onUpdated` listener — added after the first two still weren't enough.**
Confirmed by directly testing a live call (join, leave, observe): Meet's
"You've left the meeting" screen auto-redirects the same tab to
`meet.google.com/home` a few seconds later. That's a full page navigation,
which destroys `content_script.js`'s JS context outright — a fresh
instance loads on `/home` starting with `inCall = false`, so there's no
`true → false` transition left for it to detect, and `MEETING_LEFT` is
never sent. `hasLeftMeetingScreen()` alone is a race against this redirect
timer and isn't reliable. The `chrome.tabs.onUpdated` listener lives in
the service worker instead, which persists across that navigation (it's
tied to the tab, not the page) — it watches the recording/armed tab's URL
and, the moment it no longer matches the meeting-code pattern (`/xxx-xxxx-
xxx`), stops/disarms directly, with no dependency on any content script
instance surviving to report it. This is now the primary, most reliable
"call ended" signal; `hasLeftMeetingScreen()` and the `TAB_STREAM_ENDED`
safety net (tab/stream closed outright) remain as additional layers for
scenarios this one doesn't cover.

**A second, independent safety net lives in `offscreen.js`**: if the
captured tab's audio track itself ends (tab closed, navigated away from
`meet.google.com`), that's detected via the `MediaStreamTrack`'s `"ended"`
event and reported to `background.js` as `TAB_STREAM_ENDED`, which
finalizes/uploads through the exact same path as a normal `MEETING_LEFT`.
This covers failure modes no DOM heuristic can (e.g. the tab being closed
outright) and is a deliberate second layer, not a replacement for
`hasLeftMeetingScreen()` — the two catch different scenarios (DOM says
"call ended" but tab stays open, vs. the tab disappearing entirely).

## Why one click to start is unavoidable (the tabCapture gesture requirement)

`chrome.tabCapture.getMediaStreamId()` — the API that actually captures the
Meet tab's audio — only succeeds when it's called in response to a genuine
user gesture *on the extension itself*: clicking its toolbar icon/popup,
using a bound keyboard command, or a context-menu item it registered.
Chrome enforces this deliberately, so no extension can silently start
recording a tab's audio/video just because a page changed state — a
content script detecting "you joined a call" does **not** qualify, even
though a real user action (joining the meeting) triggered it. Calling
`tabCapture` from that path fails with `"Extension has not been invoked
for the current page (see activeTab permission)"`. This is a hard platform
restriction, not a bug, and there is no permission or manifest
configuration that removes it — it was confirmed by hitting this exact
error in testing.

Given that, `MEETING_JOINED` only **arms** the detected tab
(`armTab()` in `background.js`: sets `pendingTabId`/`pendingTitle`, an
orange badge, and a tooltip) rather than attempting capture directly. The
qualifying gesture is deferred to whenever the user next opens the popup —
`popup.js` fires `ARM_RECORDING` the instant it opens (see `refreshStatus()`),
which both satisfies Chrome's gesture requirement and immediately starts
the actual capture, with no second click needed inside the popup itself.
So in practice: **click the extension icon once, right after joining a
call** — that's the only manual step in the whole system. Everything else
(detecting the call, stopping on leave, uploading, transcribing,
diarizing, generating documents, saving) is fully automatic, matching the
zero-friction goal as closely as Chrome's platform allows.

## Audio capture and mixing

`chrome.tabCapture` only captures what plays through the tab — i.e. other
participants — because Google Meet does not echo your own microphone back
to you. Without also capturing the mic and mixing it in, the recording
would be missing the local user's side of the conversation entirely.
`offscreen.js` gets both streams via `getUserMedia`, mixes them through the
Web Audio API into one `MediaStreamDestination`, and records that with
`MediaRecorder`. Capturing the tab mutes its normal playback, so the tab's
stream is also routed back to the real audio output (`monitorSource`) —
otherwise the user couldn't hear the meeting while it's being recorded.

## Microphone permission has to be granted from a real tab, not offscreen.js or the popup

Offscreen documents cannot show the native browser permission prompt —
another Chrome restriction on that document type, separate from the
tabCapture gesture rule above. So `offscreen.js`'s `getUserMedia({audio:
true})` call for the user's own microphone silently fails (logged as
`"microphone unavailable, recording tab audio only"`) until microphone
access has been granted to the extension's origin from a *visible*
extension page at least once — hit in testing (recordings had tab audio
only, no mic).

The obvious fix — request it inline from the popup — was tried first and
is **unreliable**: extension popups are transient and close the instant
something else takes focus, and Chrome's native permission prompt taking
focus can close the popup before the user gets to respond, silently
dropping the request. The actual fix: `popup.js`'s `refreshMicSection()`
checks `navigator.permissions.query({name: "microphone"})` on open, and if
not yet granted, shows an "Enable Microphone Access" button that opens
`permissions.html` in a **real tab** (`chrome.tabs.create`) — a stable,
non-transient place for the prompt to appear. `permissions.js` there calls
`getUserMedia` and immediately releases the track. This only needs to run
once; the grant is remembered for the extension's origin and
`offscreen.js`'s later calls succeed without prompting again.

## Why an offscreen document, not the service worker directly

MV3 service workers can't use `getUserMedia`/`MediaRecorder`. Chrome's
documented pattern for this exact situation is what's used here:
`background.js` gets a stream ID via
`chrome.tabCapture.getMediaStreamId`, hands it to a hidden offscreen
document, and the actual recording happens there.

## Speaker-name detection (real names, not "Speaker 1/2/3")

**Goal:** Discussion/Action Points should show real names where possible,
not generic diarization labels. There is no official Google Meet API for
"who is speaking right now" or "who is this participant," so this is a
best-effort DOM heuristic, not a solved problem — treat it as such, and
expect to revisit it when Meet's UI changes.

**How it works:**
1. `findActiveSpeakerName()` (in `content_script.js`) looks for an element
   whose class/attributes mention "speaking" (Meet's visual indicator for
   the actively-talking participant's tile), then reads the name label
   from that tile via `pickNameFromElement()` — prefers the tile's own
   `aria-label` (stripping a trailing status clause like `"is speaking"`/
   `", presenting"`) over scanning child leaf text nodes; when it does fall
   back to leaf text, `pickNameFromCandidates()` picks the best-looking
   candidate (prefers text containing a space, rejects known status/badge
   tokens like initials, "muted", "presenting") rather than blindly
   trusting whichever leaf happens to render first in the DOM — the
   original "first leaf" approach was confirmed (via real-meeting testing)
   to sometimes grab a badge/status leaf instead of the actual name.
2. `onPossibleSpeakerChange()` debounces state changes (300ms minimum
   hold) via a `MutationObserver`, so DOM flicker/reflow doesn't produce
   noisy events, and sends a `SPEAKER_ACTIVE {name, atMs}` message on each
   real speaker change.
3. This only runs while `recordingActive` is true — gated on receiving
   `SARATHI_RECORDING_STARTED` (which fires for both auto-join and manual
   recordings), not on the looser `inCall` state, so the timeline's `t=0`
   lines up with `background.js`'s `recordingStartedAtMs` anchor.
4. `background.js` converts each event to seconds-since-recording-start
   and accumulates them; `offscreen.js` attaches the whole timeline as a
   `speaker_events` JSON field on the upload.
5. The backend (`app/pipeline/speaker_names.py`, see
   `../app/pipeline/DESIGN.md`) majority-votes a real name per diarization
   cluster from this timeline (normalizing name variants — casing,
   whitespace, a `"(Host)"`/`"(You)"` suffix — to the same vote bucket, so
   they don't fragment below the confidence threshold), falling back to
   the existing "Speaker N" placeholder when there's no confident match.
   When multiple diarization clusters independently vote confidently for
   the same real name — common when pyannote/AssemblyAI over-segment one
   person's voice across a pause or a chunk boundary — all of them are
   merged under that name, rather than only the strongest-voted cluster
   winning it (the old behavior left the other clusters as stray
   "Unidentified speaker N" entries for a person who *was* identified).

**Known fragility, called out explicitly rather than silently assumed:**
- Meet's class names are obfuscated/version-dependent — this needs
  re-verification against a live call (open devtools, speak, confirm what
  actually toggles) whenever Meet's UI changes, and can silently stop
  working with no error, just a quiet fallback to "Speaker N". A handful
  of `console.debug("[Meeting Saathi] ...")` lines at the extraction decision
  points exist specifically so a real test's DevTools console can show
  what's actually being seen/rejected, rather than guessing again.
- Screen-share mode reflows/shrinks tiles and may hide the indicator
  entirely for a large chunk of a client meeting.
- Large participant counts virtualize off-screen tiles — those
  participants can never be matched this way. Expected, not a bug.
- The local user's own tile is commonly labeled just "You", with no name
  text alongside it. `findActiveSpeakerName()` substitutes
  `localUserRealName` (learned from a `"(You)"`-suffixed row the People-
  panel roster scrape below already captured) when it's known, instead of
  silently dropping the local user's turns — previously, dropping them let
  `speaker_from_dom_events()`'s carry-forward semantics misattribute the
  local user's speech to whoever spoke last.
- The People/participant panel is a related but distinct signal from tile
  captions — it usually isn't open during a call — so it's used separately
  (see "Attendee roster" below), not as part of this per-utterance
  attribution.

## Attendee roster (People-panel scrape)

**Goal:** the Attendees field in generated documents should list everyone
who actually joined the meeting, including people who never spoke —
speaker-name detection above can only ever see someone who spoke, so a
quiet participant is otherwise invisible end-to-end. Real client meeting:
7 people joined, but the generated MOM's Attendees list was missing anyone
who didn't get picked up by tile-speaking-indicator scraping.

**How it works:**
1. `startRosterScraper()` (in `content_script.js`), gated the same way as
   the speaker observer (`recordingActive`), periodically calls
   `scrapeRoster()` — once ~10s after recording starts, then every ~4
   minutes.
2. `scrapeRoster()` finds Meet's People-panel toggle button
   (`findPeoplePanelToggleButton()`), briefly opens the panel if it isn't
   already open, reads every row via `readPeoplePanelRoster()` — each row's
   name comes from the same `pickNameFromElement()`/
   `pickNameFromCandidates()` helper described above (prefers `aria-label`,
   otherwise the best-looking leaf, rejecting badge/status text), then
   strips a trailing `"(You)"`/`"(Host)"` role suffix — then restores
   whatever open/closed state it found the panel in (never closes a panel
   the user opened themselves). This causes a brief (~400ms) visible flash
   of the panel if it wasn't already open — an accepted UX tradeoff,
   confirmed with the user, in exchange for reliably capturing silent
   attendees. A row whose un-stripped text carries `"(You)"` also updates
   `localUserRealName` (used by speaker-name detection above).
3. New names are deduped and sent via a `ROSTER_UPDATE {names, atMs}`
   message using `normalizeKey()` — trims, collapses whitespace, strips a
   trailing role suffix, casefolds — as the comparison key rather than raw
   case-insensitive equality; two scrapes of the same real person
   otherwise commonly produce slightly different-looking strings (a
   `"(Host)"` suffix on one pass but not another, extra whitespace, a
   different capture of leaf-vs-aria-label text) that would previously
   each be added as a "new" attendee. `background.js` merges them into
   `attendeeRoster` in `chrome.storage.session` using the same
   `normalizeKey()` (union, first-seen display string kept); `offscreen.js`
   attaches the current accumulated roster as an `attendee_roster` JSON
   field on every chunk/finalize upload, same full-snapshot-every-time
   convention as `speaker_events`.
4. The backend (`app/pipeline/roster.py`) treats this roster as the
   authoritative "who was there" list — combined with any additionally-
   detected real speaker name not already on it (also matched via the
   equivalent Python `normalize_key()`) — rather than deriving Attendees
   purely from who spoke in the transcript.

**Known fragility, called out explicitly rather than silently assumed:**
- Same caveat as speaker-name detection above: Meet's People-panel
  structure/class names are not a stable API and can change across
  releases — `findPeoplePanelToggleButton()`/`readPeoplePanelRoster()`
  need re-verification against a live multi-participant call before being
  fully trusted. The same `console.debug("[Meeting Saathi] ...")` instrumentation
  applies here.
- If the toggle button can't be found at all, this silently falls back to
  a passive read (only picks up a roster if the user already has the panel
  open themselves) rather than failing loudly — consistent with this
  extension's "best-effort, never block the recording" philosophy.
- Doesn't attempt to detect "near the meeting's end" to force one final
  scrape — relies on the periodic ~4-minute cadence alone, which is a
  reasonable simplification for meetings of typical length but could in
  principle miss someone who joined and left entirely within one interval.
- Normalization collapses whitespace/casing/role-suffix variants, but is
  not fuzzy matching — a genuinely different-looking capture of the same
  person (e.g. a tile-scrape truncated name vs. the People-panel's full
  name) can still slip through as two entries. Confirmed reduced, not
  eliminated, by this fix; if it's still visibly happening after a real
  test, the debug logging above is how to get real DOM data to refine
  `pickNameFromElement()` further instead of guessing again.

## Chunked upload: MediaRecorder restart-cycling

The old design ran one `MediaRecorder` for the whole meeting and uploaded a
single whole-file `Blob` on stop — the server then had no choice but to
process the entire recording after the meeting was already over, and
diarization's dominant cost (`../app/pipeline/DESIGN.md`) is superlinear in
duration, so a 65-minute meeting took over three hours end to end. The
fix: process audio in short chunks *during* the call instead.

`MediaRecorder` was already started with a 1000ms `timeslice`
(`ondataavailable` firing every second) even in the old design, but that
was purely an in-memory buffering detail — individual per-second `Blob`s
from one continuous session are **not independently decodable** (only the
very first carries the WebM container's init segment/headers; later ones
are raw clusters that need it). Getting a chunk the server can actually
transcribe/diarize on its own requires a real `mediaRecorder.stop()` +
new `MediaRecorder` on the same stream — that's what `cycleRecorder()`
does, on a `CHUNK_INTERVAL_MS` (~50s) timer, plus once more (as the
terminal/`isFinal` cycle) whenever `stopRecording()` is called.

This costs a small, accepted audio gap per cycle (expected <100-200ms) —
minimized by starting the next cycle's `MediaRecorder` **immediately** in
`onstop`, before doing anything else (including uploading the blob that
just finished, which happens concurrently with the new cycle already
recording).

## Chunk upload durability

Each chunk/finalize `fetch()` retries up to 3 times with backoff (1s, 3s,
9s) on failure — unlike the old single end-of-meeting upload, a dropped
chunk here means a permanent gap in the transcript, not something the user
can just re-trigger, so silently giving up after one attempt (the old
behavior) isn't good enough anymore.

**v1 gap, documented rather than silently assumed:** if all 3 retries
still fail, that chunk's audio is lost — there's no local
persistence/replay queue yet (would need chunk blobs serialized into
`chrome.storage.local`, which only stores JSON-serializable data, plus
quota and reconstruction logic). The failure is surfaced
(`CHUNK_UPLOAD_FAILED` → `SARATHI_CHUNK_UPLOAD_FAILED` banner) so an
incomplete transcript doesn't silently look like a complete one, but
recovering the lost segment is a future improvement, not something this
redesign builds.

## Manual start/stop fallback

Exists because every automatic signal above (call detection, speaker
detection) is a heuristic against an unofficial UI, and heuristics can
miss. The popup's Start/Stop button and title box let the user route
around a failed auto-detection without losing the meeting.
