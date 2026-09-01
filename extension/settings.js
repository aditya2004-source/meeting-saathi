// Standalone settings page. Everything is stored in chrome.storage.local and
// read by offscreen.js (makeClientFactory) + background.js. No network calls.

const KNOWN_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"];
const DEFAULT_MODEL = "gemini-3.6-flash";

const $ = (id) => document.getElementById(id);
const apiKeyEl = $("apiKey");
const modelEl = $("model");
const modelCustomEl = $("modelCustom");
const languageEl = $("language");
const qualityEl = $("qualityMode");
const userNameEl = $("userName");
const savedEl = $("saved");

function reflectModelSelect(model) {
  if (KNOWN_MODELS.includes(model)) {
    modelEl.value = model;
    modelCustomEl.style.display = "none";
    modelCustomEl.value = "";
  } else {
    modelEl.value = "__custom__";
    modelCustomEl.style.display = "block";
    modelCustomEl.value = model || "";
  }
}

modelEl.addEventListener("change", () => {
  modelCustomEl.style.display = modelEl.value === "__custom__" ? "block" : "none";
  if (modelEl.value === "__custom__") modelCustomEl.focus();
});

async function load() {
  const s = await chrome.storage.local.get(["geminiApiKey", "model", "languageMode", "qualityMode", "userName"]);
  apiKeyEl.value = s.geminiApiKey || "";
  reflectModelSelect(s.model || DEFAULT_MODEL);
  languageEl.value = s.languageMode || "translate";
  qualityEl.checked = s.qualityMode === true; // off by default — opt-in
  userNameEl.value = s.userName || "";
}

$("save").addEventListener("click", async () => {
  const model =
    modelEl.value === "__custom__" ? modelCustomEl.value.trim() || DEFAULT_MODEL : modelEl.value;
  await chrome.storage.local.set({
    geminiApiKey: apiKeyEl.value.trim(),
    model,
    languageMode: languageEl.value,
    qualityMode: qualityEl.checked,
    userName: userNameEl.value.trim(),
  });
  reflectModelSelect(model);
  savedEl.hidden = false;
  // Flash "Saved ✓" briefly, then close the tab (it was opened by
  // chrome.tabs.create / the options link, so window.close() is allowed).
  setTimeout(() => window.close(), 700);
});

$("testKey").addEventListener("click", async () => {
  const key = apiKeyEl.value.trim();
  const model = modelEl.value === "__custom__" ? modelCustomEl.value.trim() || DEFAULT_MODEL : modelEl.value;
  const out = $("testResult");
  if (!key) {
    out.textContent = " paste a key first";
    out.style.color = "#c0392b";
    return;
  }
  out.textContent = " testing…";
  out.style.color = "";
  try {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(key)}`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ contents: [{ parts: [{ text: "reply with: ok" }] }] }) },
    );
    if (res.ok) {
      out.textContent = ` ✓ key + "${model}" work`;
      out.style.color = "#1a7a3c";
    } else {
      const msg = await res.json().then((j) => j?.error?.message || "").catch(() => "");
      out.textContent =
        res.status === 429
          ? ` ⚠ key works but quota is exhausted right now (${res.status})`
          : ` ✗ HTTP ${res.status}: ${(msg || "").slice(0, 120)}`;
      out.style.color = res.status === 429 ? "#b9770e" : "#c0392b";
    }
  } catch (err) {
    out.textContent = ` ✗ ${err.message || err}`;
    out.style.color = "#c0392b";
  }
});

$("copyLog").addEventListener("click", async () => {
  const { debugLog = [] } = await chrome.storage.local.get("debugLog");
  const text = debugLog.map((e) => `${e.t}\t${e.source}\t${e.event}\t${e.detail}`).join("\n") || "(empty)";
  try {
    await navigator.clipboard.writeText(text);
    $("copyLog").textContent = "Copied ✓";
    setTimeout(() => ($("copyLog").textContent = "Copy debug log"), 2000);
  } catch {
    // Clipboard can be blocked — fall back to a prompt the user can copy from.
    window.prompt("Debug log — copy this:", text);
  }
});

load();
