const statusEl = document.getElementById("status");
const titleEl = document.getElementById("title");
const toggleBtn = document.getElementById("toggle");
const micSectionEl = document.getElementById("micSection");
const micStatusEl = document.getElementById("micStatus");
const grantMicBtn = document.getElementById("grantMic");

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
      "Microphone is blocked for this extension. Open chrome://settings/content/microphone, find Sarathi Meeting Bot under the blocked list, switch it to Allow, then reopen this popup.";
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

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (res) => {
    if (res && res.recording) {
      statusEl.textContent = `Recording: ${res.title || "meeting"}`;
      statusEl.style.color = "#1a7a3c";
      toggleBtn.textContent = "Stop Recording";
    } else if (res && res.armable) {
      // Opening this popup IS the one click Chrome requires (tabCapture
      // only allows starting in response to a genuine gesture on the
      // extension itself) -- arm immediately, no second click needed.
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

toggleBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (res) => {
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
