const statusEl = document.getElementById("status");
const titleEl = document.getElementById("title");
const toggleBtn = document.getElementById("toggle");
const micSectionEl = document.getElementById("micSection");
const micStatusEl = document.getElementById("micStatus");
const grantMicBtn = document.getElementById("grantMic");
const openDashboardBtn = document.getElementById("openDashboard");
const setupSectionEl = document.getElementById("setupSection");
const userNameEl = document.getElementById("userName");
const saveSetupBtn = document.getElementById("saveSetup");
const setupStatusEl = document.getElementById("setupStatus");

// Same default as background.js/offscreen.js -- the current Cloudflare
// Tunnel URL, so a customer install works without ever typing a server
// address. There's deliberately no UI for this any more (see below) --
// update all three files' DEFAULT_SERVER_BASE_URL and repackage if the
// tunnel URL ever changes, rather than asking a non-technical customer to
// know what to paste into a "server" field.
const DEFAULT_SERVER_BASE_URL = "https://significance-gpl-evaluating-element.trycloudflare.com";

// Phase 1 (sharing with BA testers): a plain self-reported name, no
// login/password -- sent with every /meetings/start call so the backend can
// enforce a per-person daily meeting limit and show a "who's using this"
// table on the dashboard (see app/db.py's count_runs_today()/usage_summary()).
async function loadSetup() {
  const { userName } = await chrome.storage.local.get("userName");
  userNameEl.value = userName || "";
  // First-time users (no name saved yet) get the section expanded so they
  // notice it, instead of it staying collapsed and silently blocking
  // recording later with no clue why.
  if (!userName) setupSectionEl.open = true;
}

saveSetupBtn.addEventListener("click", async () => {
  const userName = userNameEl.value.trim();
  await chrome.storage.local.set({ userName });
  setupStatusEl.textContent = "Saved.";
  setupStatusEl.style.color = "#1a7a3c";
  if (userName) setupSectionEl.open = false;
});

// Offscreen documents can't show the native microphone permission prompt
// (a Chrome restriction on that document type) -- so without this,
// offscreen.js's getUserMedia({audio:true}) call for your own voice
// silently fails every time (console: "microphone unavailable, recording
// tab audio only") and only other participants get recorded. Requesting
// it here, from this visible popup, is the one-time fix: Chrome shows the
// real permission prompt, and once granted, it's remembered for the
// extension's origin -- offscreen.js's later getUserMedia calls then
// succeed without prompting again.
async function checkMicPermission() {
  try {
    const status = await navigator.permissions.query({ name: "microphone" });
    return status.state; // "granted" | "denied" | "prompt"
  } catch {
    return "unknown";
  }
}

async function refreshMicSection() {
  const state = await checkMicPermission();
  if (state === "granted") {
    micSectionEl.style.display = "none";
    return;
  }
  micSectionEl.style.display = "block";
  if (state === "denied") {
    micStatusEl.textContent =
      "Microphone is blocked for this extension. Open chrome://settings/content/microphone, find Meeting Saathi under the blocked list, switch it to Allow, then reopen this popup.";
    grantMicBtn.style.display = "none";
  } else {
    micStatusEl.textContent = "Your own voice won't be recorded until you grant microphone access (one-time).";
    grantMicBtn.style.display = "block";
  }
}

grantMicBtn.addEventListener("click", () => {
  // Requesting getUserMedia directly in this popup is unreliable -- the
  // popup can close the instant Chrome's native permission prompt takes
  // focus, cutting off the request. Opening a real tab gives the prompt a
  // stable place to appear; re-opening this popup afterward re-checks the
  // permission (refreshMicSection() runs fresh on every popup open).
  chrome.tabs.create({ url: chrome.runtime.getURL("permissions.html") });
});

openDashboardBtn.addEventListener("click", async () => {
  const { serverBaseUrl } = await chrome.storage.local.get("serverBaseUrl");
  chrome.tabs.create({ url: serverBaseUrl || DEFAULT_SERVER_BASE_URL });
});

async function hasUserName() {
  const { userName } = await chrome.storage.local.get("userName");
  return Boolean(userName && userName.trim());
}

function blockForMissingName() {
  setupSectionEl.open = true;
  statusEl.textContent = "Pehle apna naam Setup section me save karo.";
  statusEl.style.color = "#c0392b";
  toggleBtn.textContent = "Start Recording";
}

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, async (res) => {
    if (res && res.recording) {
      statusEl.textContent = `Recording: ${res.title || "meeting"}`;
      statusEl.style.color = "#1a7a3c";
      toggleBtn.textContent = "Stop Recording";
    } else if (res && res.armable) {
      // Opening this popup IS the one click Chrome requires (tabCapture
      // only allows starting in response to a genuine gesture on the
      // extension itself) -- arm immediately, no second click needed.
      // Skipped entirely if no name is set yet -- starting a recording with
      // no identity would bypass the daily-limit tracking on the backend.
      if (!(await hasUserName())) {
        blockForMissingName();
        return;
      }
      statusEl.textContent = "Starting recording…";
      statusEl.style.color = "#1a7a3c";
      toggleBtn.textContent = "Start Recording";
      if (titleEl.value.trim() === "" && res.pendingTitle) {
        titleEl.value = res.pendingTitle;
      }
      chrome.runtime.sendMessage({ type: "ARM_RECORDING" }, (result) => {
        if (result && result.ok === false) {
          statusEl.textContent = `Could not start: ${result.error || result.reason || "unknown error"}`;
          statusEl.style.color = "#c0392b";
          toggleBtn.textContent = "Start Recording";
        } else {
          refreshStatus();
        }
      });
    } else {
      statusEl.textContent = "Not recording";
      statusEl.style.color = "#555";
      toggleBtn.textContent = "Start Recording";
    }
  });
}

toggleBtn.addEventListener("click", async () => {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, async (res) => {
    if (res && res.recording) {
      chrome.runtime.sendMessage({ type: "MANUAL_STOP" }, (result) => {
        if (result && result.ok === false) {
          statusEl.textContent = `Upload failed: ${result.error || result.reason || "unknown error"}`;
          statusEl.style.color = "#c0392b";
        } else {
          refreshStatus();
        }
      });
    } else {
      if (!(await hasUserName())) {
        blockForMissingName();
        return;
      }
      chrome.runtime.sendMessage({ type: "MANUAL_START", title: titleEl.value }, (result) => {
        if (result && result.ok === false) {
          statusEl.textContent = `Could not start: ${result.error || "unknown error"}`;
          statusEl.style.color = "#c0392b";
        } else {
          refreshStatus();
        }
      });
    }
  });
});

refreshStatus();
refreshMicSection();
loadSetup();
