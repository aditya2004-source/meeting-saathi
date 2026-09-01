const statusEl = document.getElementById("status");
const titleEl = document.getElementById("title");
const toggleBtn = document.getElementById("toggle");
const micSectionEl = document.getElementById("micSection");
const micStatusEl = document.getElementById("micStatus");
const grantMicBtn = document.getElementById("grantMic");
const openDashboardBtn = document.getElementById("openDashboard");
const apiKeyNagEl = document.getElementById("apiKeyNag");
const openSettingsBtn = document.getElementById("openSettings");
const settingsLink = document.getElementById("settingsLink");
const userNameEl = document.getElementById("userName");

let hasKey = false;

function openSettings() {
  chrome.tabs.create({ url: chrome.runtime.getURL("settings.html") });
}
openSettingsBtn.addEventListener("click", openSettings);
settingsLink.addEventListener("click", openSettings);

openDashboardBtn.addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("dashboard.html") });
});

// userName is optional now — a plain label for the local user's own voice.
async function loadUserName() {
  const { userName } = await chrome.storage.local.get("userName");
  userNameEl.value = userName || "";
}
userNameEl.addEventListener("change", () => {
  chrome.storage.local.set({ userName: userNameEl.value.trim() });
});

async function refreshApiKey() {
  const { geminiApiKey } = await chrome.storage.local.get("geminiApiKey");
  hasKey = Boolean(geminiApiKey && geminiApiKey.trim());
  apiKeyNagEl.hidden = hasKey;
}

// Offscreen documents can't show the native mic-permission prompt, so it must
// be granted from a real surface once. See permissions.js.
async function checkMicPermission() {
  try {
    return (await navigator.permissions.query({ name: "microphone" })).state;
  } catch {
    return "unknown";
  }
}

async function refreshMicSection() {
  const state = await checkMicPermission();
  if (state === "granted") {
    micSectionEl.hidden = true;
    return;
  }
  micSectionEl.hidden = false;
  if (state === "denied") {
    micStatusEl.textContent =
      "Microphone is blocked for this extension. Open chrome://settings/content/microphone, set Meeting Saathi to Allow, then reopen this popup.";
    grantMicBtn.hidden = true;
  } else {
    micStatusEl.textContent = "Your own voice won't be recorded until you grant microphone access (one-time).";
    grantMicBtn.hidden = false;
  }
}

grantMicBtn.addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("permissions.html") });
});

const MEETING_URL_RE = /^https:\/\/meet\.google\.com\/[a-z]{3}-[a-z]{4}-[a-z]{3}/i;
async function onAMeetCall() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return MEETING_URL_RE.test(tab?.url || "");
  } catch {
    return false;
  }
}

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, async (res) => {
    toggleBtn.disabled = false;
    if (res && res.recording) {
      statusEl.textContent = `Recording: ${res.title || "meeting"}`;
      statusEl.style.color = "#1a7a3c";
      toggleBtn.textContent = "Stop Recording";
    } else if (res && res.processing) {
      statusEl.textContent = `Processing (${res.processingStage || "…"}) — you can close this.`;
      statusEl.style.color = "#e07b00";
      toggleBtn.textContent = "Start Recording";
    } else if (res && res.armable) {
      statusEl.textContent = "Ready — click Start Recording";
      statusEl.style.color = "#2b6cb0";
      toggleBtn.textContent = "Start Recording";
      if (titleEl.value.trim() === "" && res.pendingTitle) titleEl.value = res.pendingTitle;
    } else if (!(await onAMeetCall())) {
      statusEl.textContent = "Join a Google Meet call, then click the icon.";
      statusEl.style.color = "#555";
      toggleBtn.textContent = "Join a Meet call first";
      toggleBtn.disabled = true;
    } else {
      statusEl.textContent = "Not recording";
      statusEl.style.color = "#555";
      toggleBtn.textContent = "Start Recording";
    }
  });
}

toggleBtn.addEventListener("click", () => {
  if (toggleBtn.disabled) return;
  toggleBtn.disabled = true;

  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (res) => {
    if (res && res.recording) {
      statusEl.textContent = "Stopping…";
      statusEl.style.color = "#1a7a3c";
      chrome.runtime.sendMessage({ type: "MANUAL_STOP" }, (result) => {
        if (result && result.ok === false) {
          statusEl.textContent = `Failed: ${result.error || result.reason || "unknown error"}`;
          statusEl.style.color = "#c0392b";
          toggleBtn.disabled = false;
        } else {
          refreshStatus();
        }
      });
      return;
    }

    if (!hasKey) {
      apiKeyNagEl.hidden = false;
      toggleBtn.disabled = false;
      openSettings();
      return;
    }

    const type = res && res.armable ? "ARM_RECORDING" : "MANUAL_START";
    statusEl.textContent = "Starting recording…";
    statusEl.style.color = "#1a7a3c";
    chrome.runtime.sendMessage({ type, title: titleEl.value }, (result) => {
      if (result && result.ok === false) {
        const e = result.error || result.reason || "unknown error";
        statusEl.textContent =
          e === "not-a-meeting"
            ? "Open a Google Meet call first."
            : e === "no-api-key"
              ? "Add your Gemini API key in Settings."
              : `Could not start: ${e}`;
        statusEl.style.color = "#c0392b";
        toggleBtn.textContent = "Start Recording";
        toggleBtn.disabled = false;
      } else {
        refreshStatus();
      }
    });
  });
});

refreshApiKey();
refreshStatus();
refreshMicSection();
loadUserName();
