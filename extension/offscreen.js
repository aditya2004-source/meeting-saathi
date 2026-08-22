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
const DEFAULT_SERVER_BASE_URL = "https://activists-inkjet-walter-louis.trycloudflare.com";

async function getServerBaseUrl() {
  const { serverBaseUrl } = await chrome.storage.local.get("serverBaseUrl");
  return serverBaseUrl || DEFAULT_SERVER_BASE_URL;
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
  } catch (err) {
    console.warn("Meeting Saathi: microphone unavailable, recording tab audio only.", err);
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
  startRecorderCycle();
  scheduleNextCycle();
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

  try {
    const response = await fetch(url, { method: "POST", body: formData });
    const body = await response.json();
    return { ok: response.ok, ...body };
  } catch (err) {
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
    startRecording(message.streamId, message.title, message.runId)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => {
        console.error("Meeting Saathi: startRecording failed.", err);
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
