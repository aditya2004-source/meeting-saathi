// MV3 service worker: coordinates recording start/stop. Actual audio
// capture happens in offscreen.js, since service workers can't use
// getUserMedia/MediaRecorder directly.

const OFFSCREEN_DOCUMENT_PATH = "offscreen.html";
// Phase 1 (sharing with BA testers): no longer a single hardcoded value --
// each install can point at a different backend (e.g. a tunnel URL), set
// once in popup.js's Setup section and stored in chrome.storage.local.
// Defaults to the current Cloudflare Tunnel URL (not localhost) so a
// customer's install works out of the box without them ever having to type
// a server address -- only update this if the tunnel restarts and gets a
// new URL (see ~/.config/systemd/user/meeting-saathi-tunnel.service).
const DEFAULT_SERVER_BASE_URL = "http://localhost:8420";

// Defensive -- see offscreen.js's own copy of this helper for the full
// rationale (confirmed there tonight as the actual root cause of the
// upload pipeline going silently, permanently dark). Applied here too for
// the same reason: this must never be a single point of failure for
// startMeetingRun()/the cancel calls/logDebug.
async function getServerBaseUrl() {
  try {
    const { serverBaseUrl } = await chrome.storage.local.get("serverBaseUrl");
    return serverBaseUrl || DEFAULT_SERVER_BASE_URL;
  } catch (err) {
    console.error("Meeting Saathi: chrome.storage.local.get() failed, falling back to default server URL.", err);
    return DEFAULT_SERVER_BASE_URL;
  }
}

// fetch() has no default timeout -- see offscreen.js's own copy of this
// helper for the full rationale (confirmed in production: an unbounded
// fetch during a bad spell of the Cloudflare tunnel connection hung
// forever instead of erroring, freezing everything downstream of it).
// Applied here too since startMeetingRun() below sits directly in the
// critical path of every "click Start Recording" -- a hang there looks
// identical to the extension simply doing nothing.
const FETCH_TIMEOUT_MS = 20000;

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

// Diagnostic breadcrumb channel -- see app/main.py's /debug/log for the
// full rationale. Fire-and-forget on purpose: never awaited by callers,
// errors swallowed internally, short timeout of its own -- a debug call
// must never itself slow down or block the actual recording path it's
// trying to diagnose.
function logDebug(event, detail) {
  getServerBaseUrl()
    .then((serverBaseUrl) => {
      const formData = new FormData();
      formData.append("source", "background");
      formData.append("event", event);
      formData.append("detail", detail === undefined ? "" : String(detail));
      return fetchWithTimeout(`${serverBaseUrl}/debug/log`, { method: "POST", body: formData }, 5000);
    })
    .catch(() => {});
}

async function getUserName() {
  const { userName } = await chrome.storage.local.get("userName");
  return userName || "";
}

// Comparison key for "is this the same person" -- mirrors
// content_script.js's/app/pipeline/roster.py's own normalizeKey()/
// normalize_key() (duplicated rather than shared). Two roster scrapes of
// the same real person can produce slightly different strings (extra
// whitespace, a "(You)"/"(Host)" suffix, different casing); exact-match
// dedup treated each as a distinct person, inflating the attendee count.
// Comparison only -- never used as the stored/displayed name.
function normalizeKey(name) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .join(" ")
    .replace(/\s*\((you|host|co-host|presenting)\)\s*$/i, "")
    .trim()
    .toLowerCase();
}

// MV3 service workers are NOT persistent -- Chrome kills this script after
// ~30s of idle time and restarts it fresh on the next event, wiping any
// plain `let` variables back to their initial values. A real meeting
// easily goes longer than that between events reaching this script, so
// recording state MUST survive a restart -- otherwise (confirmed bug,
// found via direct testing): the service worker gets killed mid-meeting,
// loses its own memory of "tab X is being recorded," and when
// MEETING_LEFT/TAB_STREAM_ENDED/tabs.onUpdated later fire and call
// stopRecording(), it silently no-ops because it thinks nothing is
// recording -- even though the offscreen document (a separate context,
// unaffected by the service worker's idle timeout) is still actually
// recording. The red "REC" badge stays because Chrome persists badge text
// itself; it's decoupled from this script's (wiped) memory. Fix: all of
// this state lives in chrome.storage.session (session-scoped -- cleared
// when the browser closes, which matches how long "currently recording"
// should ever be considered true) instead of module-level variables.
const STATE_KEYS = [
  "activeTabId",
  "currentTitle",
  "pendingTabId",
  "pendingTitle",
  "recordingStartedAtMs",
  "speakerEvents",
  // Accumulated People-panel attendee names from content_script.js's
  // periodic roster scrape -- a deduped list of plain name strings (not
  // timestamped like speakerEvents, since Meet's panel doesn't expose a
  // join time, just current membership).
  "attendeeRoster",
  // The chunked/streaming pipeline's run id, obtained from POST
  // /meetings/start before capture begins -- same restart-survival
  // rationale as every other entry here: offscreen.js needs it on every
  // chunk upload for the rest of the meeting, which can easily outlast
  // several service-worker restarts.
  "runId",
];

async function getState() {
  const stored = await chrome.storage.session.get(STATE_KEYS);
  return {
    activeTabId: stored.activeTabId ?? null,
    currentTitle: stored.currentTitle ?? null,
    pendingTabId: stored.pendingTabId ?? null,
    pendingTitle: stored.pendingTitle ?? null,
    recordingStartedAtMs: stored.recordingStartedAtMs ?? null,
    speakerEvents: stored.speakerEvents ?? [],
    attendeeRoster: stored.attendeeRoster ?? [],
    runId: stored.runId ?? null,
  };
}

function setState(partial) {
  return chrome.storage.session.set(partial);
}

// NOT persisted -- purely a same-tick concurrency guard inside
// ensureOffscreenDocument(), meaningless across a restart. Worst case
// after a restart wipes it: a redundant createDocument() call, which
// Chrome rejects harmlessly since hasOffscreenDocument() is checked first.
let creatingOffscreenDocument = null;

async function hasOffscreenDocument() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  return contexts.length > 0;
}

async function ensureOffscreenDocument() {
  if (await hasOffscreenDocument()) return;
  if (creatingOffscreenDocument) {
    await creatingOffscreenDocument;
    return;
  }
  creatingOffscreenDocument = chrome.offscreen.createDocument({
    url: OFFSCREEN_DOCUMENT_PATH,
    reasons: ["USER_MEDIA"],
    justification: "Record Google Meet tab + microphone audio for meeting notes.",
  });
  await creatingOffscreenDocument;
  creatingOffscreenDocument = null;
}

function notifyTab(tabId, message) {
  if (tabId == null) return;
  chrome.tabs.sendMessage(tabId, message).catch(() => {
    // Tab may have navigated away/closed -- nothing to show a banner on.
  });
}

// In-page banners auto-dismiss and are easy to miss during an active call
// (confirmed by direct testing: a real server-down failure was banner-shown
// but went unnoticed). System notifications persist outside the tab --
// requireInteraction keeps them on screen until the user actually dismisses
// them, instead of vanishing after Chrome's default ~5s.
const NOTIFICATION_ICON_URL = chrome.runtime.getURL("icon128.png");

function notifyError(tabId, message, title, body) {
  notifyTab(tabId, message);
  chrome.notifications.create("sarathi-meeting-bot-error", {
    type: "basic",
    iconUrl: NOTIFICATION_ICON_URL,
    title,
    message: body,
    requireInteraction: true,
  });
}

async function armTab(tabId, title) {
  await setState({ pendingTabId: tabId, pendingTitle: title });
  chrome.action.setBadgeText({ text: "●" });
  chrome.action.setBadgeBackgroundColor({ color: "#e07b00" });
  chrome.action.setTitle({ title: "Meeting Saathi — click to start recording" });
}

async function disarm() {
  await setState({ pendingTabId: null, pendingTitle: null });
  chrome.action.setBadgeText({ text: "" });
  chrome.action.setTitle({ title: "Meeting Saathi" });
}

// Meet's "You've left the meeting" screen auto-redirects the SAME tab to
// meet.google.com/home a few seconds later (confirmed by direct testing).
// That's a full page navigation, which destroys content_script.js's JS
// context outright and loads a fresh instance on the new page -- so the
// script instance that was supposed to notice "the call ended" is simply
// gone before it gets the chance, and a fresh instance starts with
// inCall=false and never sends MEETING_LEFT (there's no "true -> false"
// transition to detect from a script that starts already at false). This
// listener lives in the service worker instead -- restartable, but its
// *state* now survives restarts via chrome.storage.session above -- and
// watches the recording/armed tab's URL directly, independent of
// content_script.js's page-lifetime limitations and of TAB_STREAM_ENDED
// (which only fires if the tab/stream itself closes, not on same-tab
// navigation to a different Meet page).
const _MEETING_URL_PATTERN = /^\/[a-z]{3}-[a-z]{4}-[a-z]{3}/i;

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (!changeInfo.url) return;
  const { activeTabId, pendingTabId } = await getState();
  if (tabId !== activeTabId && tabId !== pendingTabId) return;

  let path;
  try {
    path = new URL(changeInfo.url).pathname;
  } catch {
    return;
  }
  if (_MEETING_URL_PATTERN.test(path)) return; // still on a meeting URL

  if (tabId === pendingTabId) await disarm();
  if (tabId === activeTabId) await stopRecording();
});

// Obtains a run_id before any audio capture begins -- the chunked pipeline
// needs somewhere to upload each chunk to as it's produced during the
// call, unlike the old design where the server only heard about a meeting
// once it was already over.
async function startMeetingRun(title) {
  const [serverBaseUrl, userName] = await Promise.all([getServerBaseUrl(), getUserName()]);
  const formData = new FormData();
  formData.append("title", title);
  formData.append("user_name", userName);
  const response = await fetchWithTimeout(
    `${serverBaseUrl}/meetings/start`,
    { method: "POST", body: formData },
    FETCH_TIMEOUT_MS
  );
  if (!response.ok) {
    // The daily-limit rejection (see app/main.py's /meetings/start) carries
    // a friendly, already-Hinglish message meant to be shown as-is --
    // surface it instead of a generic "server returned 429" so the popup's
    // existing `Could not start: ${result.error}` path shows something
    // useful. Any other non-ok response falls back to the old generic text.
    let message = `server returned ${response.status} from /meetings/start`;
    try {
      const body = await response.json();
      if (body && body.message) message = body.message;
    } catch {
      // response body wasn't JSON -- keep the generic message above.
    }
    throw new Error(message);
  }
  const body = await response.json();
  return body.id;
}

async function startRecording(tabId, title) {
  // Declared here, not inside the try, so the catch block below can still
  // see it if a LATER step fails -- startMeetingRun() already created a DB
  // row by that point, and without this the row would sit at
  // state=received forever with nothing left to ever clean it up.
  let runId = null;
  try {
    logDebug("startRecording:begin", tabId);
    runId = await startMeetingRun(title);
    logDebug("startRecording:got runId", runId);
    await ensureOffscreenDocument();
    logDebug("startRecording:offscreen document ready", "");
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
    logDebug("startRecording:got streamId", streamId ? "yes" : "no");
    const response = await chrome.runtime.sendMessage({
      target: "offscreen",
      type: "START_RECORDING",
      streamId,
      title,
      runId,
    });
    logDebug("startRecording:START_RECORDING response", JSON.stringify(response));
    if (!response || !response.ok) {
      throw new Error((response && response.error) || "offscreen document failed to start");
    }
    // Anchor point for the speaker-name timeline's t=0. Set at the exact
    // same point the content script is told recording started, so its
    // observer's start and this anchor stay tied together (the only skew
    // left is MediaRecorder's own startup latency -- a few hundred ms at
    // most, well within speaker_names.py's tolerance window).
    await setState({
      activeTabId: tabId,
      currentTitle: title,
      runId,
      recordingStartedAtMs: Date.now(),
      speakerEvents: [],
      attendeeRoster: [],
    });
    chrome.action.setBadgeText({ text: "REC" });
    chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
    notifyTab(tabId, { type: "SARATHI_RECORDING_STARTED" });
    logDebug("startRecording:success", runId);
    return { ok: true };
  } catch (err) {
    console.error("Meeting Saathi: failed to start recording.", err);
    logDebug("startRecording:CAUGHT ERROR", String((err && err.message) || err));
    // Clear any stale "click to arm" badge/tooltip left over from armTab()
    // -- whatever state prompted the click, it didn't result in a
    // recording, so there's nothing left to invite another click for.
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "Meeting Saathi" });
    const reason = String(err.message || err);
    if (runId) {
      // Best-effort, non-blocking -- a failed cancel call must never mask
      // or delay surfacing the original recording-start error below. Sends
      // the real reason so the dashboard can distinguish "never started"
      // from "started but failed to upload" (see app/main.py's
      // cancel_meeting()) instead of one generic, ambiguous message.
      getServerBaseUrl()
        .then((serverBaseUrl) => {
          const formData = new FormData();
          formData.append("reason", `Recording never started: ${reason}`);
          return fetchWithTimeout(
            `${serverBaseUrl}/meetings/${runId}/cancel`,
            { method: "POST", body: formData },
            FETCH_TIMEOUT_MS
          );
        })
        .catch(() => {});
    }
    notifyError(
      tabId,
      { type: "SARATHI_RECORDING_FAILED", reason },
      "Meeting Saathi: recording failed to start",
      reason
    );
    return { ok: false, error: reason };
  }
}

// Shared tail of "a recording has just ended, one way or another": clears
// local state/badge and reports the outcome, either via a manual/automatic
// STOP_RECORDING round-trip (stopRecording() below) or when offscreen.js
// reports its capture died on its own (CAPTURE_STREAM_DIED handler below)
// -- both need the exact same cleanup + notify-or-cancel logic, just
// starting from a different trigger.
async function _settleAfterRecordingEnds(tabId, runId, result) {
  await setState({
    activeTabId: null,
    currentTitle: null,
    runId: null,
    recordingStartedAtMs: null,
    speakerEvents: [],
    attendeeRoster: [],
  });
  chrome.action.setBadgeText({ text: "" });

  if (result && result.ok) {
    notifyTab(tabId, { type: "SARATHI_UPLOAD_DONE" });
  } else {
    const reason = (result && (result.error || result.reason)) || "unknown error";
    // Without this, the backend run has no way to ever hear that the
    // meeting ended -- it sits at chunk_processing/received forever,
    // showing "Recording" on the dashboard with a timer that never stops.
    // Distinct wording from startRecording()'s cancel call above -- audio
    // WAS captured here, it just never finished uploading/saving.
    if (runId) {
      getServerBaseUrl()
        .then((serverBaseUrl) => {
          const formData = new FormData();
          formData.append("reason", `Recording captured audio but failed to upload/save: ${reason}`);
          return fetchWithTimeout(
            `${serverBaseUrl}/meetings/${runId}/cancel`,
            { method: "POST", body: formData },
            FETCH_TIMEOUT_MS
          );
        })
        .catch(() => {});
    }
    notifyError(
      tabId,
      { type: "SARATHI_UPLOAD_FAILED", reason },
      "Meeting Saathi: recording failed to save",
      `Upload failed (${reason}) -- is the local server running?`
    );
  }
  return result;
}

// Fallback used only when the offscreen document has vanished entirely
// (confirmed in production: STOP_RECORDING has nowhere to be delivered,
// so it can't render its own final upload the normal way -- see
// offscreen.js's uploadChunk()/CAPTURE_STREAM_DIED path for the normal
// case). Every ~50s chunk recorded *before* this point already uploaded
// successfully to the server on its own -- only the very last, still-in-
// progress chunk's audio is actually lost with the document. Without
// this, the whole meeting used to be thrown away (marked failed via
// /cancel) despite most of it already sitting safely on the server,
// because nothing ever told the backend the meeting was over.
//
// Delegates the actual upload to a freshly (re)created offscreen document
// rather than fetch()-ing directly from this service worker -- confirmed
// in production that a direct service-worker fetch to this exact same
// endpoint kept failing with no useful error, even though curl-ing the
// identical request works fine, most likely because the service worker
// doesn't reliably stay alive for the whole request. Sends an empty
// placeholder as the final chunk purely to trigger the server's finalize
// path -- app/orchestrator_streaming.py's per-chunk processing tolerates
// one bad/empty chunk without failing the whole run (see
// _process_chunk_then_maybe_finalize()'s try/except there), so this just
// means the last few seconds might be missing, not the whole meeting.
async function _finalizeWithoutOffscreen(id) {
  try {
    await ensureOffscreenDocument();
    const result = await chrome.runtime.sendMessage({ target: "offscreen", type: "FINALIZE_EMPTY", runId: id });
    if (!result || !result.ok) {
      console.error("Meeting Saathi: salvage finalize via a fresh offscreen document failed.", result);
      // Surface the REAL reason instead of collapsing every possible
      // salvage failure into the same generic "offscreen document
      // unavailable" string -- that string used to hide whatever actually
      // went wrong (e.g. the salvage upload's own network error), forcing
      // a DevTools session to find out. Now the notification itself says why.
      return { ok: false, reason: (result && (result.error || result.reason)) || "salvage upload failed" };
    }
    return { ok: true };
  } catch (err) {
    console.error("Meeting Saathi: could not recreate an offscreen document to salvage this meeting.", err);
    return { ok: false, reason: `could not recreate offscreen document: ${String(err.message || err)}` };
  }
}

// Guards stopRecording() against being entered multiple times concurrently.
// Several independent triggers can all fire within milliseconds of the same
// real-world event (leaving a meeting): content_script.js's MEETING_LEFT,
// offscreen.js's TAB_STREAM_ENDED (the tab audio track ending), and
// chrome.tabs.onUpdated's own URL-based check all call stopRecording()
// separately. Without this guard each one independently re-messages the
// offscreen document and independently runs _settleAfterRecordingEnds (duplicate
// notifications, duplicate /cancel calls, and -- worst case -- one of the
// redundant STOP_RECORDING sends racing the offscreen document while it's
// mid-teardown from another one). offscreen.js already collapses concurrent
// STOP_RECORDING calls onto a single finalizePromise; this is the equivalent
// guard on this side so only one of these triggers ever actually drives the
// stop sequence.
let stopInProgress = null;

function stopRecording() {
  if (stopInProgress) return stopInProgress;
  stopInProgress = _doStopRecording().finally(() => {
    stopInProgress = null;
  });
  return stopInProgress;
}

async function _doStopRecording() {
  const { activeTabId, runId } = await getState();
  logDebug("stopRecording:begin", `activeTabId=${activeTabId} runId=${runId}`);
  if (activeTabId === null) return { ok: false, reason: "not recording" };
  const tabId = activeTabId;
  // No speakerEvents/attendeeRoster passed here -- offscreen.js already has
  // runId from START_RECORDING, and pulls a fresh snapshot of each via
  // GET_SPEAKER_EVENTS_SNAPSHOT/GET_ROSTER_SNAPSHOT right before every
  // chunk/finalize upload (including this terminal one and the salvage
  // path's own placeholder upload), so it's always current as of the
  // actual upload moment rather than a possibly-stale value passed here.
  let result;
  try {
    result = await chrome.runtime.sendMessage({ target: "offscreen", type: "STOP_RECORDING" });
    logDebug("stopRecording:STOP_RECORDING response", JSON.stringify(result));
  } catch (err) {
    logDebug("stopRecording:STOP_RECORDING threw", String((err && err.message) || err));
    // The offscreen document is gone -- e.g. the extension was reloaded/
    // updated mid-meeting, which tears down offscreen documents outright
    // (confirmed in production: this exact case left a run stuck showing
    // "Recording" with an ever-increasing elapsed timer forever, since the
    // uncaught rejection here used to skip everything below, including the
    // /cancel call). Try to salvage the meeting from whatever chunks
    // already uploaded before giving up.
    result = runId
      ? await _finalizeWithoutOffscreen(runId)
      : { ok: false, reason: "offscreen document unavailable" };
  }
  return _settleAfterRecordingEnds(tabId, runId, result);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.target === "offscreen") return; // not for us, offscreen.js handles it

  if (message.type === "MEETING_JOINED") {
    // Can't start capturing yet -- see the comment on chrome.storage.session
    // above. Only arm if nothing's already recording (v1 records one
    // meeting at a time).
    (async () => {
      const { activeTabId } = await getState();
      if (activeTabId === null) {
        await armTab(sender.tab.id, message.title);
      }
    })();
    return;
  }
  if (message.type === "MEETING_LEFT") {
    (async () => {
      const { pendingTabId } = await getState();
      if (pendingTabId !== null && sender.tab && sender.tab.id === pendingTabId) {
        await disarm();
      }
      await stopRecording();
    })();
    return;
  }
  if (message.type === "TAB_STREAM_ENDED") {
    // From offscreen.js: the captured tab's audio track ended on its own
    // (tab closed/navigated away) -- a safety net independent of
    // content_script.js's DOM-based leave detection. Finalizes through the
    // same stopRecording() path as a normal MEETING_LEFT.
    stopRecording();
    return;
  }
  if (message.type === "CAPTURE_STREAM_DIED") {
    // From offscreen.js: a mid-meeting recording cycle failed to restart
    // (confirmed in production -- see offscreen.js's handleCycleStop()) --
    // typically the tab's capture stream ending on its own after sitting
    // backgrounded a long time. offscreen.js already tore itself down and
    // did its own best-effort final upload before sending this, so this
    // must NOT re-send STOP_RECORDING (offscreen has nothing left to stop
    // -- that produced the confusing "not recording" error this replaces).
    // Just settle local state/badge and report whatever offscreen's final
    // upload attempt actually returned.
    (async () => {
      const { activeTabId, runId } = await getState();
      if (activeTabId === null) return;
      await _settleAfterRecordingEnds(activeTabId, runId, message.result);
    })();
    return;
  }
  if (message.type === "ARM_RECORDING") {
    // Fired by popup.js the moment its popup opens (a qualifying user
    // gesture on the extension, since opening the popup IS the click) --
    // this is the "one click to arm" the user takes per meeting.
    (async () => {
      const { pendingTabId, pendingTitle } = await getState();
      if (pendingTabId === null) {
        sendResponse({ ok: false, reason: "no meeting waiting to be armed" });
        return;
      }
      await setState({ pendingTabId: null, pendingTitle: null });
      const result = await startRecording(pendingTabId, pendingTitle);
      sendResponse(result);
    })();
    return true;
  }
  if (message.type === "GET_SPEAKER_EVENTS_SNAPSHOT") {
    // Pulled by offscreen.js right before every chunk/finalize upload, so
    // each chunk carries the most current accumulated speaker-name
    // timeline rather than whatever was known back when recording started.
    getState().then(({ speakerEvents }) => {
      sendResponse({ speakerEvents });
    });
    return true;
  }
  if (message.type === "CHUNK_UPLOAD_FAILED") {
    // From offscreen.js: a chunk exhausted its upload retries. That
    // segment's audio is lost for this v1 (no local durability queue yet)
    // -- surface it so the user knows part of the meeting may be missing,
    // rather than silently producing an incomplete transcript.
    (async () => {
      const { activeTabId } = await getState();
      notifyError(
        activeTabId,
        { type: "SARATHI_CHUNK_UPLOAD_FAILED", sequence: message.sequence, reason: message.reason },
        "Meeting Saathi: part of the recording was lost",
        `Chunk ${message.sequence} failed to upload (${message.reason}) -- some audio is missing from this meeting.`
      );
    })();
    return;
  }
  if (message.type === "SPEAKER_ACTIVE") {
    // Only accumulate while a recording is actually in progress, and only
    // from the tab we're actually recording (in case of stray messages
    // from another meet.google.com tab -- v1 only records one meeting at
    // a time).
    (async () => {
      const { activeTabId, recordingStartedAtMs, speakerEvents } = await getState();
      if (activeTabId !== null && sender.tab && sender.tab.id === activeTabId && recordingStartedAtMs !== null) {
        speakerEvents.push({
          name: message.name,
          t_seconds: (message.atMs - recordingStartedAtMs) / 1000,
        });
        await setState({ speakerEvents });
      }
    })();
    return;
  }
  if (message.type === "ROSTER_UPDATE") {
    // From content_script.js's periodic People-panel scrape. Merges into
    // the accumulated roster (union, first-seen order) rather than
    // replacing it -- a later scrape may see fewer rows than an earlier one
    // (e.g. the panel briefly rendered incompletely), and a name once seen
    // should never be lost for the rest of the meeting.
    (async () => {
      const { activeTabId, attendeeRoster } = await getState();
      if (activeTabId !== null && sender.tab && sender.tab.id === activeTabId) {
        const merged = [...attendeeRoster];
        const seen = new Set(merged.map((n) => normalizeKey(n)));
        for (const name of message.names || []) {
          const key = normalizeKey(name);
          if (!seen.has(key)) {
            seen.add(key);
            merged.push(name);
          }
        }
        await setState({ attendeeRoster: merged });
      }
    })();
    return;
  }
  if (message.type === "GET_ROSTER_SNAPSHOT") {
    // Pulled by offscreen.js right before every chunk/finalize upload, same
    // pattern as GET_SPEAKER_EVENTS_SNAPSHOT.
    getState().then(({ attendeeRoster }) => {
      sendResponse({ attendeeRoster });
    });
    return true;
  }
  if (message.type === "MANUAL_START") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs[0];
      startRecording(tab.id, message.title || tab.title || "Google Meet").then(sendResponse);
    });
    return true; // keep sendResponse alive for the async chrome.tabs.query
  }
  if (message.type === "MANUAL_STOP") {
    stopRecording().then((result) => sendResponse(result));
    return true;
  }
  if (message.type === "GET_STATUS") {
    getState().then(({ activeTabId, currentTitle, pendingTabId, pendingTitle }) => {
      sendResponse({
        recording: activeTabId !== null,
        title: currentTitle,
        armable: pendingTabId !== null,
        pendingTitle,
      });
    });
    return true; // keep sendResponse alive for the async getState()
  }
});

// A keyboard shortcut is one of the few things Chrome recognizes as "the
// user invoked the extension" (same category as clicking the toolbar icon
// or a context menu item) -- unlike a click on a page element injected by
// content_script.js, which does NOT qualify (confirmed the hard way: Chrome
// rejects tabCapture with "Extension has not been invoked for the current
// page" for that case). This is what content_script.js's on-join reminder
// banner is telling the user to press.
chrome.commands.onCommand.addListener((command, tab) => {
  if (command !== "start-recording" || !tab) return;
  (async () => {
    await setState({ pendingTabId: null, pendingTitle: null });
    await startRecording(tab.id, tab.title || "Google Meet");
  })();
});
