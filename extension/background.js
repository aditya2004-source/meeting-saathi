// MV3 service worker — pure orchestration. It never does Gemini work (a long
// fetch in a service worker dies with it, ~30s idle) and never talks to a
// server (there is none). It:
//   • arms/starts/stops capture and detects "you left the meeting" (3 layers)
//   • holds small recording state in chrome.storage.session (survives SW restarts)
//   • accumulates the content script's speaker/roster signal
//   • relays the offscreen document's PROCESSING_* progress to notifications/badge
//   • keeps a capped debug ring buffer in chrome.storage.local
//
// Classic worker (no ES modules) — every heavy import lives in offscreen.js.

const OFFSCREEN_DOCUMENT_PATH = "offscreen.html";
const IDB_NAME = "meeting-saathi";
const IDB_VERSION = 1;

// --------------------------------------------------------------------- debug log

const DEBUG_LOG_KEY = "debugLog";
const DEBUG_LOG_CAP = 500;

async function debugLog(source, event, detail) {
  try {
    const { [DEBUG_LOG_KEY]: log = [] } = await chrome.storage.local.get(DEBUG_LOG_KEY);
    log.push({ t: new Date().toISOString(), source, event, detail: detail === undefined ? "" : String(detail) });
    if (log.length > DEBUG_LOG_CAP) log.splice(0, log.length - DEBUG_LOG_CAP);
    await chrome.storage.local.set({ [DEBUG_LOG_KEY]: log });
  } catch {
    /* logging must never break the flow */
  }
}
const bgLog = (event, detail) => debugLog("background", event, detail);

// --------------------------------------------------------------------- settings

async function hasApiKey() {
  try {
    const { geminiApiKey } = await chrome.storage.local.get("geminiApiKey");
    return Boolean(geminiApiKey && geminiApiKey.trim());
  } catch {
    return false;
  }
}

function openSettings() {
  chrome.tabs.create({ url: chrome.runtime.getURL("settings.html") });
}

function openDashboard(runId) {
  const url = runId
    ? chrome.runtime.getURL(`dashboard.html?m=${encodeURIComponent(runId)}`)
    : chrome.runtime.getURL("dashboard.html");
  chrome.tabs.create({ url });
}

// Comparison key for roster dedup — mirrors lib/transcript.js::normalizeKey and
// app/pipeline/roster.py (duplicated, not shared — classic worker).
function normalizeKey(name) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .join(" ")
    .replace(/\s*\((you|host|co-host|presenting)\)\s*$/i, "")
    .trim()
    .toLowerCase();
}

// --------------------------------------------------------------- recording state

// MV3 service workers are not persistent — Chrome kills this script after ~30s
// idle and restarts it fresh, wiping module-level `let`s. A meeting easily
// outlasts that between events, so recording state lives in
// chrome.storage.session (cleared on browser close — the right lifetime for
// "currently recording").
const STATE_KEYS = [
  "activeTabId",
  "currentTitle",
  "pendingTabId",
  "pendingTitle",
  "recordingStartedAtMs",
  "speakerEvents",
  "attendeeRoster",
  "runId",
  "processingRunId",
  "processingStage",
];

async function getState() {
  const s = await chrome.storage.session.get(STATE_KEYS);
  return {
    activeTabId: s.activeTabId ?? null,
    currentTitle: s.currentTitle ?? null,
    pendingTabId: s.pendingTabId ?? null,
    pendingTitle: s.pendingTitle ?? null,
    recordingStartedAtMs: s.recordingStartedAtMs ?? null,
    speakerEvents: s.speakerEvents ?? [],
    attendeeRoster: s.attendeeRoster ?? [],
    runId: s.runId ?? null,
    processingRunId: s.processingRunId ?? null,
    processingStage: s.processingStage ?? null,
  };
}

const setState = (partial) => chrome.storage.session.set(partial);

// --------------------------------------------------------------- offscreen doc

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
    reasons: ["USER_MEDIA", "BLOBS"],
    justification: "Record Google Meet tab + microphone audio and process it with your Gemini key.",
  });
  await creatingOffscreenDocument;
  creatingOffscreenDocument = null;
}

function sendOffscreen(message) {
  return chrome.runtime.sendMessage({ target: "offscreen", ...message });
}

/** Tear down and rebuild the offscreen document — used before a Resume so a
 *  stuck run (an offscreen doc wedged on a hung request, its in-flight guard
 *  never clearing) can't block the retry. Safe only when NOT recording. */
async function recreateOffscreenDocument() {
  try {
    if (await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
  } catch {
    /* nothing to close */
  }
  creatingOffscreenDocument = null;
  await ensureOffscreenDocument();
}

// --------------------------------------------------------------------- UI helpers

const NOTIFICATION_ICON_URL = chrome.runtime.getURL("icon128.png");

function notifyTab(tabId, message) {
  if (tabId == null) return;
  chrome.tabs.sendMessage(tabId, message).catch(() => {});
}

function notify(id, title, message, { requireInteraction = true } = {}) {
  chrome.notifications.create(id, {
    type: "basic",
    iconUrl: NOTIFICATION_ICON_URL,
    title,
    message,
    requireInteraction,
  });
}

function notifyError(tabId, tabMessage, title, body) {
  notifyTab(tabId, tabMessage);
  notify("meeting-saathi-error", title, body);
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

async function armTab(tabId, title) {
  await setState({ pendingTabId: tabId, pendingTitle: title });
  setBadge("●", "#e07b00");
  chrome.action.setTitle({ title: "Meeting Saathi — click to start recording" });
}

async function disarm() {
  await setState({ pendingTabId: null, pendingTitle: null });
  setBadge("");
  chrome.action.setTitle({ title: "Meeting Saathi" });
}

// --------------------------------------------------------------- leave detection

// Meet's "you left" screen auto-redirects the SAME tab to /home a few seconds
// later — a full navigation that destroys content_script.js before it can send
// MEETING_LEFT. This SW-side watcher is independent of that.
const MEETING_URL_PATTERN = /^\/[a-z]{3}-[a-z]{4}-[a-z]{3}/i;

/** True only if `tabId` is an actual Google Meet *call* (meet.google.com/xxx-xxxx-xxx),
 *  not the Meet home page or some unrelated tab. Guards manual/keyboard start. */
async function isMeetingTab(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    const u = new URL(tab.url || "");
    return u.hostname === "meet.google.com" && MEETING_URL_PATTERN.test(u.pathname);
  } catch {
    return false;
  }
}

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
  if (MEETING_URL_PATTERN.test(path)) return;
  if (tabId === pendingTabId) await disarm();
  if (tabId === activeTabId) await stopRecording();
});

// ------------------------------------------------------------------- start / stop

async function startRecording(tabId, title) {
  try {
    bgLog("startRecording:begin", tabId);

    if (!(await isMeetingTab(tabId))) {
      setBadge("");
      chrome.action.setTitle({ title: "Meeting Saathi" });
      notifyError(
        tabId,
        { type: "SARATHI_RECORDING_FAILED", reason: "not in a Google Meet call" },
        "Meeting Saathi: no meeting to record",
        "Open a Google Meet call first, then start the recording.",
      );
      return { ok: false, error: "not-a-meeting" };
    }

    if (!(await hasApiKey())) {
      setBadge("");
      chrome.action.setTitle({ title: "Meeting Saathi" });
      notifyError(
        tabId,
        { type: "SARATHI_RECORDING_FAILED", reason: "no Gemini API key" },
        "Meeting Saathi: add your Gemini API key",
        "Open Meeting Saathi settings and paste your own Gemini API key, then start the recording.",
      );
      openSettings();
      return { ok: false, error: "no-api-key" };
    }

    const runId = crypto.randomUUID();
    await ensureOffscreenDocument();
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
    const response = await sendOffscreen({ type: "START_RECORDING", streamId, title, runId });
    if (!response || !response.ok) {
      throw new Error((response && response.error) || "offscreen document failed to start");
    }

    await setState({
      activeTabId: tabId,
      currentTitle: title,
      runId,
      recordingStartedAtMs: Date.now(),
      speakerEvents: [],
      attendeeRoster: [],
    });
    setBadge("REC", "#c0392b");
    notifyTab(tabId, { type: "SARATHI_RECORDING_STARTED" });
    bgLog("startRecording:success", runId);
    return { ok: true };
  } catch (err) {
    const reason = String((err && err.message) || err);
    console.error("Meeting Saathi: failed to start recording.", err);
    bgLog("startRecording:ERROR", reason);
    setBadge("");
    chrome.action.setTitle({ title: "Meeting Saathi" });
    notifyError(
      tabId,
      { type: "SARATHI_RECORDING_FAILED", reason },
      "Meeting Saathi: recording failed to start",
      reason,
    );
    return { ok: false, error: reason };
  }
}

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
  bgLog("stopRecording:begin", `activeTabId=${activeTabId} runId=${runId}`);
  if (activeTabId === null) return { ok: false, reason: "not recording" };
  const tabId = activeTabId;

  let result;
  try {
    result = await sendOffscreen({ type: "STOP_AND_PROCESS", runId });
    bgLog("stopRecording:STOP_AND_PROCESS response", JSON.stringify(result));
  } catch (err) {
    // The offscreen document is gone (extension reload mid-meeting, Chrome
    // reclaim). Recreate it and let it resume from whatever audio parts made
    // it to IndexedDB.
    bgLog("stopRecording:offscreen gone, recreating", String((err && err.message) || err));
    try {
      await ensureOffscreenDocument();
      result = await sendOffscreen({ type: "RESUME_PROCESSING", runId });
    } catch (err2) {
      result = { ok: false, reason: String((err2 && err2.message) || err2) };
    }
  }

  await setState({
    activeTabId: null,
    currentTitle: null,
    runId: null,
    recordingStartedAtMs: null,
    speakerEvents: [],
    attendeeRoster: [],
  });

  if (result && result.ok) {
    setBadge("…", "#e07b00");
    await setState({ processingRunId: runId, processingStage: "assembling" });
    notifyTab(tabId, { type: "SARATHI_PROCESSING_STARTED" });
  } else {
    setBadge("");
    const reason = (result && (result.error || result.reason)) || "unknown error";
    notifyError(
      tabId,
      { type: "SARATHI_UPLOAD_FAILED", reason },
      "Meeting Saathi: could not process the recording",
      `${reason} — check your internet and your Gemini API key in Settings.`,
    );
  }
  return result;
}

// ------------------------------------------------- resume unfinished on SW spin-up

// Raw one-shot IndexedDB read (the SW is classic — it can't import lib/idb.js).
// Just enough to know whether to wake the offscreen document, which then runs
// the real resume logic (offscreen.js::selfHealOnLoad + the pipeline).
/** @param {string|null} skipRunId  the currently-recording meeting — never resume it */
function idbUnfinishedRunIds(skipRunId = null) {
  return new Promise((resolve) => {
    let open;
    try {
      open = indexedDB.open(IDB_NAME, IDB_VERSION);
    } catch {
      resolve([]);
      return;
    }
    open.onupgradeneeded = () => {
      // If the DB doesn't exist yet there is nothing to resume; don't create schema here.
      try {
        open.transaction.abort();
      } catch {
        /* ignore */
      }
      resolve([]);
    };
    open.onerror = () => resolve([]);
    open.onsuccess = () => {
      const db = open.result;
      if (!db.objectStoreNames.contains("meetings")) {
        db.close();
        resolve([]);
        return;
      }
      try {
        const tx = db.transaction("meetings", "readonly");
        const req = tx.objectStore("meetings").getAll();
        req.onsuccess = () => {
          const ids = (req.result || [])
            .filter((m) => m.runId !== skipRunId)
            // `processing` is always safe to resume. A `recording` row is only
            // resumable if it's genuinely orphaned — NOT the live capture (that
            // would assemble half the audio mid-meeting).
            .filter((m) => m.status === "processing" || m.status === "recording")
            .map((m) => m.runId);
          db.close();
          resolve(ids);
        };
        req.onerror = () => {
          db.close();
          resolve([]);
        };
      } catch {
        db.close();
        resolve([]);
      }
    };
  });
}

// Debounced (not once-per-session) so a mid-session SW respawn can still recover
// a run whose offscreen document died. Also fired on browser start / extension
// (re)load below.
const RESUME_DEBOUNCE_MS = 30000;
async function maybeResumeUnfinished() {
  const { lastResumeCheckMs = 0 } = await chrome.storage.session.get("lastResumeCheckMs");
  if (Date.now() - lastResumeCheckMs < RESUME_DEBOUNCE_MS) return;
  await chrome.storage.session.set({ lastResumeCheckMs: Date.now() });

  const { activeTabId, runId: recordingRunId } = await getState();
  // A live recording is in progress — do NOT touch its meeting row, and don't
  // recreate the offscreen document (that would kill the capture).
  if (activeTabId !== null) return;

  const ids = await idbUnfinishedRunIds(recordingRunId);
  if (ids.length === 0) return;
  bgLog("maybeResumeUnfinished", ids.join(","));
  await recreateOffscreenDocument();
  // Give the fresh document's message listener a beat to register before the
  // immediate resume (offscreen.js's selfHealOnLoad is the +3s backstop).
  await new Promise((r) => setTimeout(r, 500));
  for (const runId of ids) sendOffscreen({ type: "RESUME_PROCESSING", runId }).catch(() => {});
}
maybeResumeUnfinished();
chrome.runtime.onStartup.addListener(maybeResumeUnfinished);
chrome.runtime.onInstalled.addListener(maybeResumeUnfinished);

// --------------------------------------------------------------------- messaging

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.target === "offscreen") return; // offscreen.js handles those

  switch (message.type) {
    // ---- from the offscreen pipeline ----
    case "DEBUG_LOG":
      debugLog(message.source || "offscreen", message.event, message.detail);
      return false;

    case "PROCESSING_PENDING":
      bgLog("PROCESSING_PENDING", `runId=${message.runId} parts=${message.parts} bytes=${message.bytes}`);
      return false;

    case "PROCESSING_PROGRESS":
      setBadge("…", "#e07b00");
      setState({ processingRunId: message.runId, processingStage: message.stage });
      return false;

    case "PROCESSING_DONE":
      (async () => {
        setBadge("");
        await setState({ processingRunId: null, processingStage: null });
        notify(
          `meeting-saathi-done:${message.runId}`,
          "Meeting Saathi: documents ready",
          "Minutes of Meeting and a Meeting Analysis are ready. Click to open them.",
        );
        const { activeTabId } = await getState();
        notifyTab(activeTabId, { type: "SARATHI_PROCESSING_DONE" });
      })();
      return false;

    case "PROCESSING_FAILED":
      (async () => {
        setBadge("");
        await setState({ processingRunId: null, processingStage: null });
        notify(
          "meeting-saathi-error",
          "Meeting Saathi: processing failed",
          `${message.error || "unknown error"} (at ${message.stage}). Open the dashboard to retry.`,
        );
      })();
      return false;

    case "PROCESSING_STOPPED":
      // Soft stop — e.g. Gemini free-tier limit. Transcript is saved; the user
      // Resumes from the dashboard once the limit resets.
      (async () => {
        setBadge("");
        await setState({ processingRunId: null, processingStage: null });
        notify(
          "meeting-saathi-error",
          "Meeting Saathi: paused",
          `${message.error || `Paused at ${message.stage}. Open the dashboard and click Resume.`}`,
        );
      })();
      return false;

    // ---- from the content script ----
    case "MEETING_JOINED":
      (async () => {
        const { activeTabId } = await getState();
        if (activeTabId === null) await armTab(sender.tab.id, message.title);
      })();
      return false;

    case "MEETING_LEFT":
      (async () => {
        const { pendingTabId } = await getState();
        if (pendingTabId !== null && sender.tab && sender.tab.id === pendingTabId) await disarm();
        await stopRecording();
      })();
      return false;

    case "TAB_STREAM_ENDED":
      stopRecording();
      return false;

    case "SPEAKER_ACTIVE":
      (async () => {
        const { activeTabId, recordingStartedAtMs, speakerEvents } = await getState();
        if (activeTabId !== null && sender.tab && sender.tab.id === activeTabId && recordingStartedAtMs !== null) {
          speakerEvents.push({ name: message.name, t_seconds: (message.atMs - recordingStartedAtMs) / 1000 });
          await setState({ speakerEvents });
        }
      })();
      return false;

    case "ROSTER_UPDATE":
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
      return false;

    // ---- pulled by the offscreen pipeline at STOP_AND_PROCESS ----
    case "GET_SPEAKER_EVENTS_SNAPSHOT":
      getState().then(({ speakerEvents }) => sendResponse({ speakerEvents }));
      return true;

    case "GET_ROSTER_SNAPSHOT":
      getState().then(({ attendeeRoster }) => sendResponse({ attendeeRoster }));
      return true;

    // The offscreen document can't reliably use chrome.storage.* (only
    // chrome.runtime is guaranteed there) — it asks the SW for the settings.
    case "GET_SETTINGS":
      chrome.storage.local
        .get(["geminiApiKey", "model", "qualityMode", "languageMode"])
        .then((s) =>
          sendResponse({
            geminiApiKey: s.geminiApiKey || "",
            model: s.model || "gemini-3.6-flash",
            qualityMode: s.qualityMode === true, // opt-in — the extra passes ~2x the requests
            languageMode: s.languageMode || "translate",
          }),
        )
        .catch((err) => sendResponse({ error: String((err && err.message) || err) }));
      return true;

    // ---- from the popup ----
    case "ARM_RECORDING":
      (async () => {
        const { pendingTabId, pendingTitle } = await getState();
        if (pendingTabId === null) {
          sendResponse({ ok: false, reason: "no meeting waiting to be armed" });
          return;
        }
        await setState({ pendingTabId: null, pendingTitle: null });
        sendResponse(await startRecording(pendingTabId, pendingTitle));
      })();
      return true;

    case "MANUAL_START":
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const tab = tabs[0];
        startRecording(tab.id, message.title || tab.title || "Google Meet").then(sendResponse);
      });
      return true;

    case "MANUAL_STOP":
      stopRecording().then((result) => sendResponse(result));
      return true;

    // ---- from the dashboard: Stop a running/stuck processing job ----
    case "STOP_PROCESSING":
      (async () => {
        const { activeTabId } = await getState();
        if (activeTabId !== null) {
          sendResponse({ ok: false, reason: "a recording is in progress" });
          return;
        }
        // Killing the offscreen document kills whatever the pipeline is doing
        // (including a hung fetch). The dashboard has already set the row to
        // `stopped` in IndexedDB.
        try {
          if (await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
        } catch {
          /* nothing to close */
        }
        creatingOffscreenDocument = null;
        setBadge("");
        await setState({ processingRunId: null, processingStage: null });
        bgLog("STOP_PROCESSING", message.runId);
        sendResponse({ ok: true });
      })();
      return true;

    // ---- from the dashboard (Resume / Retry / Regenerate / rename-then-regenerate) ----
    case "REQUEST_PROCESSING":
      (async () => {
        try {
          const { activeTabId } = await getState();
          // A wedged run lives in the current offscreen doc's in-flight guard —
          // give it a fresh one. Never do this mid-recording (would kill capture).
          if (activeTabId === null) await recreateOffscreenDocument();
          else await ensureOffscreenDocument();
          await new Promise((res) => setTimeout(res, 500)); // let the new doc's listener register
          const r = await sendOffscreen({ type: "RESUME_PROCESSING", runId: message.runId });
          sendResponse(r || { ok: true });
        } catch (err) {
          sendResponse({ ok: false, error: String((err && err.message) || err) });
        }
      })();
      return true;

    case "GET_STATUS":
      getState().then(({ activeTabId, currentTitle, pendingTabId, pendingTitle, processingRunId, processingStage }) => {
        sendResponse({
          recording: activeTabId !== null,
          title: currentTitle,
          armable: pendingTabId !== null,
          pendingTitle,
          processing: processingRunId !== null,
          processingStage,
        });
      });
      return true;

    default:
      return false;
  }
});

// Clicking the "documents ready" notification opens the dashboard for that run.
chrome.notifications.onClicked.addListener((notificationId) => {
  if (notificationId.startsWith("meeting-saathi-done:")) {
    openDashboard(notificationId.slice("meeting-saathi-done:".length));
  } else if (notificationId === "meeting-saathi-error") {
    openDashboard();
  }
  chrome.notifications.clear(notificationId);
});

// A keyboard shortcut is a qualifying user gesture for tabCapture (a click on a
// content-script-injected page element is not).
chrome.commands.onCommand.addListener((command, tab) => {
  if (command !== "start-recording" || !tab) return;
  (async () => {
    await setState({ pendingTabId: null, pendingTitle: null });
    await startRecording(tab.id, tab.title || "Google Meet");
  })();
});
