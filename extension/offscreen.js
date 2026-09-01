// Runs in a hidden offscreen document — the one place an MV3 extension can use
// getUserMedia / MediaRecorder / long-lived fetch / IndexedDB. This document
// does TWO jobs across a meeting's life:
//
//   1. CAPTURE: tab audio + mic -> WebAudio mix -> ONE continuous MediaRecorder.
//      A requestData() flush timer appends each blob-part to IndexedDB
//      (lib/recorder.js + lib/idb.js), so losing this document mid-meeting costs
//      only the audio since the last flush.
//
//   2. PROCESSING: assemble -> Gemini Files upload -> transcribe -> extract
//      facts -> generate MOM + Meeting Analysis, as a resumable state machine
//      driven off the meeting row's `stage` (lib/pipeline.js). If this document
//      is reclaimed mid-pipeline, the SW recreates it and selfHealOnLoad() /
//      RESUME_PROCESSING continues from the last committed stage.
//
// No server. No founder resource. The only network egress is the user's own
// Gemini key, from lib/pipeline.js.

import { GeminiClient } from "./lib/gemini.js";
import { MEETING_STATUS, MeetingStore } from "./lib/idb.js";
import { MeetingRecorder } from "./lib/recorder.js";
import { runPipeline } from "./lib/pipeline.js";

let store = null;
let recorder = null;
let audioContext = null;
let capturedStreams = [];
let activeRunId = null;
let activeTitle = "Google Meet";

// runIds this document is already processing — guards against the on-load
// resume racing an explicit RESUME_PROCESSING / STOP_AND_PROCESS for the same
// meeting (double Gemini spend, out-of-order stage writes).
const inFlight = new Set();

async function getStore() {
  if (!store) store = await MeetingStore.open();
  return store;
}

/** Build a GeminiClient from the user's own key + settings. The offscreen
 *  document can't rely on chrome.storage.* (only chrome.runtime is guaranteed
 *  here), so it asks the service worker. */
async function makeClientFactory() {
  let s;
  try {
    s = await chrome.runtime.sendMessage({ type: "GET_SETTINGS" });
  } catch (err) {
    throw new Error(`Could not read settings from the extension: ${(err && err.message) || err}`);
  }
  if (!s || s.error) throw new Error(`Could not read settings: ${(s && s.error) || "no response"}`);
  const { geminiApiKey, model, qualityMode, languageMode } = s;
  if (!geminiApiKey) throw new Error("No Gemini API key set. Open Meeting Saathi settings and paste your key.");
  return {
    makeClient: (rowModel) =>
      new GeminiClient({
        apiKey: geminiApiKey,
        model: rowModel || model || "gemini-3.6-flash",
        requestTimeoutMs: 180000, // a stalled Gemini connection must fail, not hang the pipeline
        uploadTimeoutMs: 600000, // a 90-min audio upload can legitimately take minutes
        onRetry: (n, ms, statusCode) => debugLog("gemini:retry", `attempt=${n} in=${ms}ms status=${statusCode}`),
      }),
    qualityMode: qualityMode !== false,
    languageMode: languageMode || "translate",
  };
}

async function buildCtx() {
  const { makeClient, qualityMode, languageMode } = await makeClientFactory();
  return {
    store: await getStore(),
    makeClient,
    qualityMode,
    languageMode,
    send: (type, payload) => chrome.runtime.sendMessage({ type, ...payload }).catch(() => {}),
    logger: (msg, err) => debugLog("pipeline", `${msg}${err ? " :: " + ((err && err.message) || err) : ""}`),
  };
}

/** Run the pipeline for one meeting, deduped, never throwing to the caller. */
async function processMeeting(runId) {
  if (inFlight.has(runId)) {
    debugLog("processMeeting:already-running", runId);
    return { ok: true, alreadyRunning: true };
  }
  inFlight.add(runId);
  debugLog("processMeeting:start", runId);
  try {
    const ctx = await buildCtx();
    const out = await runPipeline(runId, ctx);
    debugLog("processMeeting:done", `${runId} :: ${JSON.stringify(out)}`);
    return { ok: true, runId };
  } catch (err) {
    const msg = String((err && err.message) || err);
    debugLog("processMeeting:failed", `${runId} :: ${msg}`);
    // runPipeline marks its own stage failures — but a setup failure (no API
    // key, storage error) happens in buildCtx() *before* runPipeline, and
    // would otherwise leave the row silently stuck at `processing`. Surface it
    // so the dashboard shows "Failed: <reason>" + Retry instead of a spinner
    // that never ends.
    try {
      const db = await getStore();
      const m = await db.getMeeting(runId);
      if (m && m.status === MEETING_STATUS.PROCESSING) {
        await db.updateMeeting(runId, { status: MEETING_STATUS.FAILED, error: msg });
        chrome.runtime.sendMessage({ type: "PROCESSING_FAILED", runId, stage: m.stage || "assembling", error: msg }).catch(() => {});
      }
    } catch {
      /* best effort */
    }
    return { ok: false, runId, error: msg };
  } finally {
    inFlight.delete(runId);
  }
}

function debugLog(event, detail) {
  // Replaces the old server /debug/log breadcrumb — now a message to the SW,
  // which keeps a capped ring buffer in chrome.storage.local (Phase 4).
  chrome.runtime.sendMessage({ type: "DEBUG_LOG", source: "offscreen", event, detail: String(detail ?? "") }).catch(() => {});
}

// --------------------------------------------------------------------- capture

async function startCapture(streamId, title, runId) {
  activeTitle = title || "Google Meet";
  activeRunId = runId;
  capturedStreams = [];
  debugLog("startCapture:begin", `runId=${runId}`);

  // The tab's own audio output — the other participants' voices.
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
    video: false,
  });
  capturedStreams.push(tabStream);

  // Your microphone. Meet does not echo your own voice through the tab audio,
  // so without this your speech is missing entirely. Raced against a timeout:
  // an offscreen document can't surface Chrome's mic-permission prompt, so a
  // still-"prompt" permission would hang getUserMedia forever.
  let micStream = null;
  try {
    micStream = await Promise.race([
      navigator.mediaDevices.getUserMedia({ audio: true }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("microphone permission prompt unanswered")), 4000)),
    ]);
    capturedStreams.push(micStream);
  } catch (err) {
    console.warn("Meeting Saathi: microphone unavailable, recording tab audio only.", err);
    debugLog("startCapture:mic unavailable", (err && err.message) || err);
  }

  // Safety net independent of content_script.js's DOM call-end detection: if
  // the captured tab closes/navigates (its audio track ends), tell the SW.
  tabStream.getAudioTracks().forEach((track) => {
    track.addEventListener("ended", () => {
      chrome.runtime.sendMessage({ type: "TAB_STREAM_ENDED", runId }).catch(() => {});
    });
  });

  audioContext = new AudioContext();
  const destination = audioContext.createMediaStreamDestination();
  audioContext.createMediaStreamSource(tabStream).connect(destination);
  if (micStream) audioContext.createMediaStreamSource(micStream).connect(destination);
  // Route the tab audio back to the speakers — capturing a tab mutes its
  // normal playback, so without this you can't hear the meeting.
  audioContext.createMediaStreamSource(tabStream).connect(audioContext.destination);

  const db = await getStore();
  await db.createMeeting({ runId, title: activeTitle, audioMimeType: "audio/webm" });

  recorder = new MeetingRecorder({
    stream: destination.stream,
    runId,
    store: db,
    onError: (err, ctx) => {
      console.error("Meeting Saathi: recorder error", err, ctx);
      debugLog("recorder:error", `${(err && err.message) || err} ${JSON.stringify(ctx)}`);
    },
  });
  recorder.start();
  debugLog("startCapture:recording", recorder.state);
}

function teardownCapture() {
  capturedStreams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
  capturedStreams = [];
  const closing = audioContext ? audioContext.close().catch(() => {}) : Promise.resolve();
  audioContext = null;
  return closing;
}

/**
 * Stop capture, mark the row for processing, ack the caller, then run the
 * resumable pipeline in the background (its PROCESSING_* messages carry status).
 */
async function stopAndProcess(runId) {
  const db = await getStore();
  const id = runId || activeRunId;
  if (!id) return { ok: false, reason: "no active meeting" };

  let seq = 0;
  let bytes = 0;
  if (recorder) {
    ({ seq, bytes } = await recorder.stop());
    recorder = null;
  }
  await teardownCapture();

  // Snapshot the DOM speaker/roster signal the SW accumulated, onto the row,
  // so the transcription prompt can use it.
  const [speakerEvents, attendeeRoster] = await Promise.all([
    chrome.runtime.sendMessage({ type: "GET_SPEAKER_EVENTS_SNAPSHOT" }).then((r) => (r && r.speakerEvents) || []).catch(() => []),
    chrome.runtime.sendMessage({ type: "GET_ROSTER_SNAPSHOT" }).then((r) => (r && r.attendeeRoster) || []).catch(() => []),
  ]);

  await db.updateMeeting(id, {
    status: MEETING_STATUS.PROCESSING,
    stage: "assembling",
    endedAt: new Date().toISOString(),
    speakerEvents,
    attendeeRoster,
  });
  activeRunId = null;
  debugLog("stopAndProcess:captured", `runId=${id} parts=${seq} bytes=${bytes}`);

  // Fire-and-forget — the caller gets a quick ack; progress arrives via
  // PROCESSING_* messages. Guarded by inFlight so a later RESUME_PROCESSING
  // for the same runId is a no-op while this runs.
  processMeeting(id);
  return { ok: true, runId: id, parts: seq, bytes };
}

/** Resume (or start) processing for one meeting — used by the SW after it
 *  (re)creates this document, and by the on-load self-heal. */
async function resumeProcessing(runId) {
  if (!runId) return { ok: false, reason: "no runId" };
  processMeeting(runId);
  return { ok: true, runId, started: true };
}

/** On (re)creation, pick up any meeting already in `processing` whose offscreen
 *  document died mid-pipeline. Rows still marked `recording` are left for the SW
 *  to hand back explicitly (it knows whether a capture is genuinely active).
 *  Routes through processMeeting() so it can't double-run a meeting an explicit
 *  RESUME_PROCESSING is already handling. */
async function selfHealOnLoad() {
  try {
    const db = await getStore();
    const resumable = (await db.listResumable()).filter((m) => m.status === MEETING_STATUS.PROCESSING);
    if (resumable.length === 0) return;
    debugLog("selfHealOnLoad", `resuming ${resumable.map((m) => m.runId).join(",")}`);
    for (const m of resumable) processMeeting(m.runId);
  } catch (err) {
    debugLog("selfHealOnLoad:error", (err && err.message) || err);
  }
}
// Give the SW a beat to send START_RECORDING for a brand-new meeting first.
setTimeout(selfHealOnLoad, 3000);

// -------------------------------------------------------------------- messaging

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.target !== "offscreen") return;

  switch (message.type) {
    case "START_RECORDING":
      startCapture(message.streamId, message.title, message.runId)
        .then(() => sendResponse({ ok: true }))
        .catch((err) => {
          console.error("Meeting Saathi: startCapture failed.", err);
          debugLog("startCapture:ERROR", (err && err.message) || err);
          sendResponse({ ok: false, error: String((err && err.message) || err) });
        });
      return true;

    case "STOP_AND_PROCESS":
      stopAndProcess(message.runId)
        .then((r) => sendResponse(r))
        .catch((err) => sendResponse({ ok: false, error: String((err && err.message) || err) }));
      return true;

    case "RESUME_PROCESSING":
      resumeProcessing(message.runId)
        .then((r) => sendResponse(r))
        .catch((err) => sendResponse({ ok: false, error: String((err && err.message) || err) }));
      return true;

    case "GET_CAPTURE_STATE":
      sendResponse({ recording: recorder ? recorder.state === "recording" : false, runId: activeRunId });
      return false;

    default:
      return false;
  }
});
