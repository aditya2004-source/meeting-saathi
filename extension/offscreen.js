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

const SERVER_BASE_URL = "http://localhost:8420";
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
  let micStream = null;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    capturedStreams.push(micStream);
  } catch (err) {
    console.warn("Sarathi Meeting Bot: microphone unavailable, recording tab audio only.", err);
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

async function handleCycleStop() {
  const blob = new Blob(currentChunkBlobs, { type: "audio/webm" });
  const finishedSequence = sequence;
  sequence += 1;
  const isFinal = stopRequested;

  if (isFinal) {
    // No more audio needed -- tear down capture now, before the upload
    // (which can take a while) rather than after.
    capturedStreams.forEach((stream) => stream.getTracks().forEach((track) => track.stop()));
    if (audioContext) {
      await audioContext.close();
      audioContext = null;
    }
    mediaRecorder = null;
  } else {
    // Start the next cycle immediately -- before uploading the blob that
    // just finished -- to keep the recording gap as small as possible.
    startRecorderCycle();
    scheduleNextCycle();
  }

  const result = await uploadChunk(finishedSequence, blob, isFinal);
  if (isFinal) {
    const resolve = stopResolve;
    stopResolve = null;
    if (resolve) resolve(result);
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

const UPLOAD_MAX_ATTEMPTS = 3;
const UPLOAD_BACKOFF_MS = [1000, 3000, 9000];

async function uploadChunk(sequenceNumber, blob, isFinal, attempt = 1) {
  const url = isFinal
    ? `${SERVER_BASE_URL}/meetings/${runId}/finalize`
    : `${SERVER_BASE_URL}/meetings/${runId}/chunk`;
  const speakerEvents = await fetchSpeakerEventsSnapshot();

  const formData = new FormData();
  formData.append("sequence", String(sequenceNumber));
  formData.append("audio", blob, `chunk-${sequenceNumber}.webm`);
  // Full accumulated array every time, not just events since the last
  // chunk -- simpler than delta-tracking, and the payload stays tiny (a
  // meeting's worth of speaker-change events is at most a few hundred
  // entries), plus it's safely idempotent if this same chunk gets retried.
  formData.append("speaker_events", JSON.stringify(speakerEvents));

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
      `Sarathi Meeting Bot: chunk ${sequenceNumber} upload failed after ${UPLOAD_MAX_ATTEMPTS} attempts.`,
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
        console.error("Sarathi Meeting Bot: startRecording failed.", err);
        sendResponse({ ok: false, error: String(err.message || err) });
      });
    return true;
  }
  if (message.type === "STOP_RECORDING") {
    stopRecording().then((result) => sendResponse(result));
    return true;
  }
});
