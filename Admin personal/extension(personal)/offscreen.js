// Runs in a hidden offscreen document. This is where actual audio capture
// happens -- MV3 service workers can't use getUserMedia/MediaRecorder, so
// Chrome requires this separate document for it.
//
// Streaming/chunked upload: instead of recording the whole meeting into one
// MediaRecorder session and uploading a single Blob at the end,
// MediaRecorder is stopped and restarted on a fixed interval
// (CHUNK_INTERVAL_MS). Each stop/start cycle produces one self-contained,
// independently-decodable WebM file -- individual ondataavailable Blobs
// from a single continuous session are NOT independently decodable (only
// the very first one carries the container's init segment/headers), so a
// real stop/start cycle is required to get chunks the server can actually
// process on their own. This costs a small (expected <100-200ms) audio gap
// per cycle -- minimized by starting the next cycle's MediaRecorder
// immediately in onstop, before doing anything else (including the upload
// of the blob that just finished).

// Phase 1 (sharing with BA testers): configurable, same convention as
// background.js's own copy of this helper (separate execution context,
// can't share code) -- set once in popup.js's Setup section. Defaults to
// the current Cloudflare Tunnel URL so a customer install works without
// them ever typing a server address; update if the tunnel URL changes.
const DEFAULT_SERVER_BASE_URL = "http://localhost:8420";

// Defensive: this is the very first `await` in both uploadChunk() and
// logDebug(), and neither of those callers originally wrapped it in a
// try/catch of their own -- confirmed as the actual root cause tonight
// (live-tested: zero chunk uploads and zero diagnostic breadcrumbs ever
// reached the server from THIS file specifically, while background.js's
// identical-shaped calls worked every time) and matches this session's very
// first bug report, an uncaught error whose stack trace pointed at exactly
// this line. Whatever the underlying cause of chrome.storage.local.get()
// failing inside an offscreen document turns out to be, this function
// should simply never be a single point of failure for the entire upload
// pipeline -- falling back to the default URL is always a safe, correct
// answer here, same as the `serverBaseUrl || DEFAULT_SERVER_BASE_URL` logic
// already does for the common case (key never set).
async function getServerBaseUrl() {
  try {
    const { serverBaseUrl } = await chrome.storage.local.get("serverBaseUrl");
    return serverBaseUrl || DEFAULT_SERVER_BASE_URL;
  } catch (err) {
    console.error("Meeting Saathi: chrome.storage.local.get() failed, falling back to default server URL.", err);
    return DEFAULT_SERVER_BASE_URL;
  }
}

// Diagnostic breadcrumb channel -- see app/main.py's /debug/log and
// background.js's own copy of this helper for the full rationale. This is
// the more important of the two copies: the offscreen document's console is
// one of the two Chrome surfaces this session's tooling has been unable to
// reach directly, so without this there is no way to see what's actually
// happening inside startRecording()/uploadChunk() at all.
function logDebug(event, detail) {
  getServerBaseUrl()
    .then((serverBaseUrl) => {
      const formData = new FormData();
      formData.append("source", "offscreen");
      formData.append("event", event);
      formData.append("detail", detail === undefined ? "" : String(detail));
      return fetchWithTimeout(`${serverBaseUrl}/debug/log`, { method: "POST", body: formData }, 5000);
    })
    .catch(() => {});
}

const CHUNK_INTERVAL_MS = 50000; // ~50s per chunk

let mediaRecorder = null;
let destinationStream = null;
let currentChunkBlobs = [];
let runId = null;
let sequence = 0;
let currentTitle = "Google Meet";
let audioContext = null;
let capturedStreams = [];
let cycleTimer = null;
let stopRequested = false;
let stopResolve = null;
// Set the moment ANY finalization sequence begins (an explicit stop, or
// the capture stream dying on its own -- see handleCycleStop()) and
// cleared once it resolves. Exists because two independent "the stream
// just ended" signals can fire at nearly the same moment for the exact
// same real-world event (the tab's audio track ending): the track's own
// "ended" listener below (which asks background.js to call
// stopRecording()) and MediaRecorder's own onstop handler. Without this,
// whichever one loses that race finds mediaRecorder already null/
// "inactive" and reports a spurious "not recording" failure -- confirmed
// in production -- instead of waiting for the finalization already under
// way to actually finish and report its real result.
let finalizePromise = null;

async function startRecording(streamId, title, newRunId) {
  currentTitle = title || "Google Meet";
  runId = newRunId;
  sequence = 0;
  stopRequested = false;
  capturedStreams = [];
  logDebug("startRecording:begin", `runId=${runId} streamId=${streamId ? "yes" : "no"}`);

  // The tab's own audio output (what plays through your speakers -- i.e.
  // other participants' voices in the meeting).
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId,
      },
    },
    video: false,
  });
  capturedStreams.push(tabStream);
  logDebug("startRecording:tab getUserMedia resolved", "");

  // Your own microphone. Meet does NOT echo your own voice back through the
  // tab's audio output, so without this, your own speech would be missing
  // from the recording entirely.
  //
  // Raced against a timeout: an offscreen document CANNOT show Chrome's
  // native mic permission prompt (a platform restriction -- see popup.js's
  // own comment on this). If permission is still unresolved ("prompt"
  // state) when this runs, getUserMedia has nobody able to answer the
  // prompt it silently opened -- confirmed in production: the call just
  // hangs forever instead of rejecting, which stalls this whole function
  // before startRecorderCycle() ever runs, so NOTHING is ever captured or
  // uploaded and no error is ever reported (background.js's own
  // START_RECORDING round-trip hangs right along with it). A real
  // permission denial rejects quickly on its own and is unaffected by this
  // race; only the stuck-prompt case needs the timeout to fall through to
  // the existing tab-audio-only fallback below.
  const MIC_PERMISSION_TIMEOUT_MS = 4000;
  let micStream = null;
  try {
    micStream = await Promise.race([
      navigator.mediaDevices.getUserMedia({ audio: true }),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("microphone permission prompt unanswered")), MIC_PERMISSION_TIMEOUT_MS)
      ),
    ]);
    capturedStreams.push(micStream);
    logDebug("startRecording:mic getUserMedia resolved", "");
  } catch (err) {
    console.warn("Meeting Saathi: microphone unavailable, recording tab audio only.", err);
    logDebug("startRecording:mic getUserMedia failed/timed out", String((err && err.message) || err));
  }

  // Safety net independent of content_script.js's DOM-based call-end
  // detection: if the captured tab itself closes or navigates away (its
  // audio track ends), tell background.js so it can stop/finalize through
  // the normal path -- otherwise a recording could be stuck running
  // forever if the "you left the call" DOM signal is ever missed (this is
  // exactly what happened before hasLeftMeetingScreen() was added in
  // content_script.js -- this is the second, independent layer for the
  // same failure mode, e.g. closing the tab outright).
  tabStream.getAudioTracks().forEach((track) => {
    track.addEventListener("ended", () => {
      chrome.runtime.sendMessage({ type: "TAB_STREAM_ENDED" }).catch(() => {});
    });
  });

  audioContext = new AudioContext();
  const destination = audioContext.createMediaStreamDestination();
  audioContext.createMediaStreamSource(tabStream).connect(destination);
  if (micStream) {
    audioContext.createMediaStreamSource(micStream).connect(destination);
  }

  // Capturing the tab mutes its normal playback -- route it back to your
  // speakers so you can still hear the meeting while it's being recorded.
  const monitorSource = audioContext.createMediaStreamSource(tabStream);
  monitorSource.connect(audioContext.destination);

  destinationStream = destination.stream;
  logDebug("startRecording:audio graph wired", "");
  startRecorderCycle();
  logDebug("startRecording:startRecorderCycle done", mediaRecorder ? mediaRecorder.state : "null");
  scheduleNextCycle();
  logDebug("startRecording:scheduleNextCycle done, fully started", "");
}

function startRecorderCycle() {
  currentChunkBlobs = [];
  mediaRecorder = new MediaRecorder(destinationStream, { mimeType: "audio/webm;codecs=opus" });
  mediaRecorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) currentChunkBlobs.push(event.data);
  };
  mediaRecorder.onstop = handleCycleStop;
  mediaRecorder.start(1000);
}

function scheduleNextCycle() {
  cycleTimer = setTimeout(() => cycleRecorder(false), CHUNK_INTERVAL_MS);
}

function cycleRecorder(isFinal) {
  if (cycleTimer) {
    clearTimeout(cycleTimer);
    cycleTimer = null;
  }
  if (!mediaRecorder || mediaRecorder.state === "inactive") return;
  stopRequested = isFinal;
  mediaRecorder.stop();
}

function _tearDownCapture() {
  capturedStreams.forEach((stream) => stream.getTracks().forEach((track) => track.stop()));
  const closing = audioContext ? audioContext.close() : Promise.resolve();
  audioContext = null;
  mediaRecorder = null;
  return closing;
}

async function handleCycleStop() {
  const blob = new Blob(currentChunkBlobs, { type: "audio/webm" });
  const finishedSequence = sequence;
  sequence += 1;
  const isFinal = stopRequested;
  let streamDied = false;
  logDebug("handleCycleStop:begin", `sequence=${finishedSequence} isFinal=${isFinal} blobSize=${blob.size}`);

  if (isFinal) {
    // No more audio needed -- tear down capture now, before the upload
    // (which can take a while) rather than after.
    await _tearDownCapture();
  } else {
    try {
      // Start the next cycle immediately -- before uploading the blob that
      // just finished -- to keep the recording gap as small as possible.
      startRecorderCycle();
      scheduleNextCycle();
    } catch (err) {
      // The source stream has ended -- e.g. Chrome silently revoked tab
      // capture after the tab sat backgrounded for a long time after the
      // meeting ended. Starting a new MediaRecorder on a dead stream
      // throws here. Confirmed in production: this exception used to just
      // vanish (an unhandled rejection inside a MediaRecorder.onstop
      // handler), leaving the recording permanently stuck -- no more
      // cycles, no more uploads, no notification -- until a manual stop
      // click later failed with a confusing "not recording" error, because
      // this very recorder was the one left sitting inactive. Treat this
      // chunk as the final one instead of trying (and failing) to continue.
      console.error("Meeting Saathi: could not start next recording cycle -- source stream likely ended.", err);
      streamDied = true;
      await _tearDownCapture();
    }
  }

  if (!isFinal && !streamDied) {
    await uploadChunk(finishedSequence, blob, false);
    logDebug("handleCycleStop:non-final upload done", `sequence=${finishedSequence}`);
    return;
  }

  // Publish the in-flight promise *before* awaiting it -- a concurrently
  // racing stopRecording() call (background.js's TAB_STREAM_ENDED handler
  // fires from the exact same underlying "tab audio track ended" event
  // that can trigger this path, at nearly the same moment) checks this
  // first and awaits it instead of finding mediaRecorder already null and
  // reporting a premature "not recording".
  finalizePromise = uploadChunk(finishedSequence, blob, true);
  const result = await finalizePromise;
  finalizePromise = null;

  if (stopResolve) {
    const resolve = stopResolve;
    stopResolve = null;
    resolve(result);
  } else if (streamDied) {
    // Nobody was waiting on a stopRecording() promise -- this wasn't a
    // manual/automatic stop request, the capture just died on its own.
    // Tell background.js directly (not via the STOP_RECORDING round-trip --
    // there's nothing left here to stop) so it clears its own state/badge
    // instead of showing "Recording" forever.
    chrome.runtime.sendMessage({ type: "CAPTURE_STREAM_DIED", result }).catch(() => {});
  }
}

async function fetchSpeakerEventsSnapshot() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "GET_SPEAKER_EVENTS_SNAPSHOT" });
    return (response && response.speakerEvents) || [];
  } catch {
    return [];
  }
}

async function fetchRosterSnapshot() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "GET_ROSTER_SNAPSHOT" });
    return (response && response.attendeeRoster) || [];
  } catch {
    return [];
  }
}

const UPLOAD_MAX_ATTEMPTS = 3;
const UPLOAD_BACKOFF_MS = [1000, 3000, 9000];
// fetch() has no default timeout -- confirmed in production: during a bad
// spell of the Cloudflare tunnel's own connection (see the tunnel systemd
// service's own known reconnect-loop issues), a chunk/finalize upload just
// hung forever instead of erroring, which stalled EVERYTHING downstream of
// it -- no more chunks ever uploaded (handleCycleStop() never got back to
// scheduling the next cycle... actually it already had, but every
// subsequent cycle's own upload hung the same way), and a manual stop
// looked permanently "stuck" because stopRecording()'s promise chain
// ultimately bottoms out in this same uploadChunk() call for the final
// chunk. Bounding it means a bad connection fails fast into the existing
// retry/backoff logic instead of freezing the whole pipeline silently.
const UPLOAD_TIMEOUT_MS = 20000;

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    // ngrok's free tier serves an HTML "are you sure you trust this link"
    // interstitial to any request that looks like a browser navigation,
    // instead of proxying through to the actual server -- this header is
    // ngrok's documented way for a known client (this extension) to skip
    // it. Harmless no-op against Railway/Cloudflare/localhost.
    const headers = { ...(options && options.headers), "ngrok-skip-browser-warning": "true" };
    return await fetch(url, { ...options, headers, signal: controller.signal });
  } finally {
    clearTimeout(timeoutId);
  }
}

async function uploadChunk(sequenceNumber, blob, isFinal, attempt = 1) {
  const serverBaseUrl = await getServerBaseUrl();
  const url = isFinal
    ? `${serverBaseUrl}/meetings/${runId}/finalize`
    : `${serverBaseUrl}/meetings/${runId}/chunk`;
  const speakerEvents = await fetchSpeakerEventsSnapshot();
  const attendeeRoster = await fetchRosterSnapshot();

  const formData = new FormData();
  formData.append("sequence", String(sequenceNumber));
  formData.append("audio", blob, `chunk-${sequenceNumber}.webm`);
  // Full accumulated array every time, not just events since the last
  // chunk -- simpler than delta-tracking, and the payload stays tiny (a
  // meeting's worth of speaker-change events is at most a few hundred
  // entries), plus it's safely idempotent if this same chunk gets retried.
  formData.append("speaker_events", JSON.stringify(speakerEvents));
  // Same full-snapshot-every-time convention -- the accumulated People-panel
  // roster as of this upload moment, overwriting attendee_roster.json
  // server-side with the freshest version each time.
  formData.append("attendee_roster", JSON.stringify(attendeeRoster));

  logDebug("uploadChunk:begin", `sequence=${sequenceNumber} isFinal=${isFinal} attempt=${attempt} url=${url}`);
  try {
    const response = await fetchWithTimeout(url, { method: "POST", body: formData }, UPLOAD_TIMEOUT_MS);
    const body = await response.json();
    logDebug("uploadChunk:fetch resolved", `sequence=${sequenceNumber} status=${response.status}`);
    return { ok: response.ok, ...body };
  } catch (err) {
    logDebug(
      "uploadChunk:fetch threw/timed out",
      `sequence=${sequenceNumber} attempt=${attempt} err=${String((err && err.message) || err)}`
    );
    if (attempt < UPLOAD_MAX_ATTEMPTS) {
      await new Promise((resolve) => setTimeout(resolve, UPLOAD_BACKOFF_MS[attempt - 1]));
      return uploadChunk(sequenceNumber, blob, isFinal, attempt + 1);
    }
    console.error(
      `Meeting Saathi: chunk ${sequenceNumber} upload failed after ${UPLOAD_MAX_ATTEMPTS} attempts.`,
      err
    );
    // v1 durability gap, documented: no local persistence/replay queue for
    // exhausted-retry chunks yet, so this segment's audio is lost. Surfaced
    // to the user (via background.js) rather than failing silently, so an
    // incomplete transcript doesn't look like a complete one.
    chrome.runtime
      .sendMessage({ type: "CHUNK_UPLOAD_FAILED", sequence: sequenceNumber, reason: String(err) })
      .catch(() => {});
    return { ok: false, error: String(err) };
  }
}

function stopRecording() {
  logDebug(
    "offscreen stopRecording:called",
    `hasFinalizePromise=${!!finalizePromise} mediaRecorderState=${mediaRecorder ? mediaRecorder.state : "null"}`
  );
  // A finalization is already under way (the stream died on its own --
  // see handleCycleStop()) -- wait for its real result instead of racing
  // ahead and reporting a spurious "not recording" just because
  // mediaRecorder already looks inactive to this call.
  if (finalizePromise) return finalizePromise;
  return new Promise((resolve) => {
    if (!mediaRecorder || mediaRecorder.state === "inactive") {
      resolve({ ok: false, reason: "not recording" });
      return;
    }
    stopResolve = resolve;
    cycleRecorder(true);
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.target !== "offscreen") return;

  if (message.type === "START_RECORDING") {
    logDebug("onMessage:START_RECORDING received", "");
    startRecording(message.streamId, message.title, message.runId)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => {
        console.error("Meeting Saathi: startRecording failed.", err);
        logDebug("startRecording:CAUGHT ERROR", String((err && err.message) || err));
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true;
  }
  if (message.type === "STOP_RECORDING") {
    stopRecording().then((result) => sendResponse(result));
    return true;
  }
  if (message.type === "FINALIZE_EMPTY") {
    // Used when the ORIGINAL offscreen document (the one that actually
    // had mediaRecorder / the real in-progress audio) vanished entirely.
    // background.js recreates a brand new offscreen document (this one)
    // and asks it to send a placeholder final chunk instead of trying to
    // fetch() directly from the service worker -- confirmed in
    // production that the service-worker-direct attempt kept failing even
    // though the exact same request works fine via curl, most likely
    // because the service worker doesn't reliably stay alive for the
    // whole fetch. This document never actually recorded anything (its
    // own mediaRecorder is null), so `runId` has to be set explicitly
    // instead of coming from a real startRecording() call.
    runId = message.runId;
    uploadChunk(999999, new Blob([], { type: "audio/webm" }), true)
      .then((result) => sendResponse(result))
      .catch((err) => sendResponse({ ok: false, error: String(err.message || err) }));
    return true;
  }
});
