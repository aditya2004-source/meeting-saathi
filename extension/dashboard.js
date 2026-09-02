// The Meeting Saathi dashboard — reads IndexedDB, renders documents, and drives
// Regenerate / Rename speakers / Retry / Delete (all local; regeneration is
// forwarded to the offscreen pipeline via the service worker). No network.

import { MEETING_STATUS, MeetingStore } from "./lib/idb.js";
import { distinctSpeakers, renameSpeakers } from "./lib/transcript.js";

const listEl = document.getElementById("list");
const printArea = document.getElementById("printArea");
const focusRunId = new URLSearchParams(location.search).get("m");

const DOC_META = [
  ["mom", "Minutes of Meeting", "MOM"],
  ["meeting_analysis", "Meeting Analysis", "Meeting_Analysis"],
  ["business_process_flow", "Business Process Flow", "Business_Process_Flow"],
];
const MERMAID_DOCS = new Set(["business_process_flow"]);

// Disable Mermaid's DOMContentLoaded auto-run: it would render each pre.mermaid
// to an <svg>, and a later render pass would then read that SVG's text back as
// diagram source and fail. We call mermaid.run() explicitly instead. (This
// module executes before DOMContentLoaded, so the config lands in time.)
if (window.mermaid) {
  try {
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral" });
  } catch {
    /* mermaid missing / older build — BPF diagrams just won't render */
  }
}

/** Render every un-processed <pre class="mermaid"> inside `root`. Never throws —
 *  a bad diagram falls back to Mermaid's own inline error graphic. */
async function renderMermaidIn(root) {
  if (!window.mermaid || !root) return;
  const nodes = [...root.querySelectorAll("pre.mermaid:not([data-processed])")];
  if (!nodes.length) return;
  try {
    await window.mermaid.run({ nodes, suppressErrors: true });
  } catch {
    /* leave the raw fence text visible rather than blow up the page */
  }
}
const STAGE_PCT = { assembling: 8, uploading: 22, transcribing: 45, extracting: 65, generating: 85, done: 100 };

let store = null;
const getStore = async () => (store ||= await MeetingStore.open());

// per-session view state, preserved across re-renders
const openRun = new Set(focusRunId ? [focusRunId] : []);
const activeTab = new Map(); // runId -> docKey | "transcript"
const renaming = new Set();
const confirmingDelete = new Set();

// --------------------------------------------------------------- markdown → safe HTML

const ALLOWED = new Set([
  "H1", "H2", "H3", "H4", "H5", "H6", "P", "UL", "OL", "LI", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD",
  "STRONG", "EM", "B", "I", "CODE", "PRE", "BLOCKQUOTE", "A", "HR", "BR", "DEL", "SPAN",
]);

function sanitize(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const walk = (node) => {
    for (const child of [...node.childNodes]) {
      if (child.nodeType === Node.ELEMENT_NODE) {
        if (!ALLOWED.has(child.tagName)) {
          // keep text, drop the tag
          child.replaceWith(...child.childNodes);
          continue;
        }
        for (const attr of [...child.attributes]) {
          const ok =
            child.tagName === "A" && attr.name === "href" && /^(https?:|mailto:)/i.test(attr.value.trim());
          if (!ok) child.removeAttribute(attr.name);
        }
        if (child.tagName === "A") {
          child.setAttribute("target", "_blank");
          child.setAttribute("rel", "noopener noreferrer");
        }
        walk(child);
      } else if (child.nodeType === Node.COMMENT_NODE) {
        child.remove();
      }
    }
  };
  walk(tpl.content);
  return tpl.innerHTML;
}

const MERMAID_FENCE_RE = /(?:^|\n)[ \t]*```mermaid[ \t]*\n([\s\S]*?)\n[ \t]*```[ \t]*(?=\n|$)/g;

function renderMarkdown(md, { mermaid = false } = {}) {
  let source = md || "";
  const diagrams = [];
  if (mermaid) {
    source = source.replace(MERMAID_FENCE_RE, (_m, body) => {
      diagrams.push(body);
      return `\n\n@@MERMAID_${diagrams.length - 1}@@\n\n`;
    });
  }
  const raw = window.marked ? window.marked.parse(source, { gfm: true, breaks: false }) : source;
  let html = sanitize(raw);
  if (diagrams.length) {
    html = html.replace(/<p>\s*@@MERMAID_(\d+)@@\s*<\/p>|@@MERMAID_(\d+)@@/g, (_m, a, b) => {
      const src = diagrams[Number(a ?? b)] || "";
      return `<pre class="mermaid">${esc(src)}</pre>`;
    });
  }
  return html;
}

// --------------------------------------------------------------------- helpers

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmtDate = (iso) => {
  try {
    return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return iso || "";
  }
};
const agoLabel = (ms) => {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};

function downloadText(filename, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function savePdf(titleHtml, bodyHtml) {
  printArea.innerHTML = `<h1>${titleHtml}</h1>` + bodyHtml;
  await renderMermaidIn(printArea);
  document.body.classList.add("print-mode");
  const cleanup = () => {
    document.body.classList.remove("print-mode");
    printArea.innerHTML = "";
    window.removeEventListener("afterprint", cleanup);
  };
  window.addEventListener("afterprint", cleanup);
  window.print();
  // Safari/others may not fire afterprint — belt & braces
  setTimeout(cleanup, 60000);
}

// --------------------------------------------------------------------- rendering

async function cardBody(m, db) {
  if (m.status === MEETING_STATUS.RECORDING) {
    return `<p class="stage">Recording in progress…</p>`;
  }

  if (m.status === MEETING_STATUS.PROCESSING) {
    const pct = STAGE_PCT[m.stage] ?? 5;
    const idleMs = Date.now() - Date.parse(m.updatedAt || m.startedAt || 0);
    const stale = idleMs > 6 * 60 * 1000;
    return (
      `<p class="stage">Processing — <strong>${esc(m.stage || "starting")}</strong> · using your Gemini key` +
      ` <span style="opacity:.7">(updated ${agoLabel(idleMs)})</span></p>` +
      `<div class="progress"><i style="width:${pct}%"></i></div>` +
      (stale
        ? `<p class="err">This looks stuck. Gemini may be rate-limiting, or the connection stalled — Resume kicks it again, or Stop it.</p>`
        : `<p class="stage">You can close this tab; it keeps going in the background.</p>`) +
      `<div class="actions">` +
      `<button data-action="stop" data-run="${m.runId}">Stop</button>` +
      (stale ? `<button data-action="resume" data-run="${m.runId}">Resume</button>` : "") +
      deleteButtons(m.runId) +
      `</div>`
    );
  }

  if (m.status === MEETING_STATUS.STOPPED) {
    const msg = m.error
      ? esc(m.error)
      : `Stopped at <strong>${esc(m.stage || "?")}</strong>. Nothing has been lost — Resume continues from here.`;
    return (
      `<p class="stage">${msg}</p>` +
      `<div class="actions">` +
      `<button data-action="resume" data-run="${m.runId}">Resume</button>` +
      deleteButtons(m.runId) +
      `</div>`
    );
  }

  if (m.status === MEETING_STATUS.FAILED) {
    return (
      `<p class="err">Failed at ${esc(m.stage || "?")}: ${esc(m.error || "unknown error")}</p>` +
      `<div class="actions">` +
      `<button data-action="retry" data-run="${m.runId}">Retry</button>` +
      deleteButtons(m.runId) +
      `</div>`
    );
  }

  // ready
  const docs = {};
  for (const [key] of DOC_META) docs[key] = await db.getDocument(m.runId, key);
  const transcript = await db.getTranscript(m.runId);
  const tab = activeTab.get(m.runId) || "mom";

  const tabsHtml =
    `<div class="tabs">` +
    DOC_META.map(([key, label]) => `<button class="tab ${tab === key ? "active" : ""}" data-action="tab" data-run="${m.runId}" data-tab="${key}">${label}</button>`).join("") +
    `<button class="tab ${tab === "transcript" ? "active" : ""}" data-action="tab" data-run="${m.runId}" data-tab="transcript">Transcript</button>` +
    `</div>`;

  let paneHtml;
  if (tab === "transcript") {
    paneHtml = `<div class="transcript">${esc(transcript?.plainText || "(no transcript)")}</div>`;
  } else {
    const doc = docs[tab];
    paneHtml = doc
      ? `<div class="doc">${renderMarkdown(doc.markdown, { mermaid: MERMAID_DOCS.has(tab) })}</div>`
      : `<p class="stage">This document isn't available.</p>`;
  }

  const renameHtml = renaming.has(m.runId) ? renameFormHtml(m.runId, transcript) : "";

  const actions =
    `<div class="actions">` +
    (tab === "transcript"
      ? `<button data-action="download-md" data-run="${m.runId}" data-tab="transcript">Download transcript.txt</button>`
      : `<button data-action="download-md" data-run="${m.runId}" data-tab="${tab}">Download .md</button>` +
        `<button data-action="save-pdf" data-run="${m.runId}" data-tab="${tab}">Save as PDF</button>`) +
    `<button data-action="open-rename" data-run="${m.runId}">Rename speakers</button>` +
    `<button data-action="regenerate" data-run="${m.runId}">Regenerate documents</button>` +
    deleteButtons(m.runId) +
    `</div>`;

  return tabsHtml + paneHtml + renameHtml + actions;
}

function deleteButtons(runId) {
  return confirmingDelete.has(runId)
    ? `<button class="danger" data-action="confirm-delete" data-run="${runId}">Confirm delete</button>` +
        `<button data-action="cancel-delete" data-run="${runId}">Cancel</button>`
    : `<button class="danger" data-action="delete" data-run="${runId}">Delete</button>`;
}

function renameFormHtml(runId, transcript) {
  const speakers = distinctSpeakers(transcript?.segments || []);
  if (speakers.length === 0) return `<div class="rename"><p class="stage">No transcript to rename.</p></div>`;
  return (
    `<div class="rename">` +
    `<p class="stage">Fix any speaker labels, then regenerate the documents from the corrected transcript.</p>` +
    speakers
      .map(
        (s, i) =>
          `<div class="row"><span class="old">${esc(s)}</span>` +
          `<input data-rename-run="${runId}" data-rename-old="${esc(s)}" value="${esc(s)}" placeholder="real name" ${i === 0 ? "autofocus" : ""}></div>`,
      )
      .join("") +
    `<div class="actions">` +
    `<button data-action="apply-rename" data-run="${runId}">Apply &amp; regenerate</button>` +
    `<button data-action="cancel-rename" data-run="${runId}">Cancel</button>` +
    `</div></div>`
  );
}

async function render() {
  let db;
  let meetings;
  try {
    db = await getStore();
    meetings = await db.listMeetings();
  } catch (err) {
    listEl.innerHTML = `<p class="empty">Couldn't read local storage: ${esc(err.message || err)}<br><button data-action="reload-page">Reload</button></p>`;
    return;
  }
  if (meetings.length === 0) {
    listEl.innerHTML = `<p class="empty">No meetings yet.<br>Join a Google Meet and click the Meeting Saathi toolbar icon to record one.</p>`;
    return;
  }

  const frag = document.createDocumentFragment();
  for (const m of meetings) {
    const open = openRun.has(m.runId);
    const div = document.createElement("div");
    div.className = "meeting" + (open ? " open" : "") + (m.runId === focusRunId ? " focus" : "");
    div.dataset.run = m.runId;
    const statusText = m.status === MEETING_STATUS.PROCESSING ? `processing · ${m.stage || "…"}` : m.status;
    let body = "";
    if (open) {
      try {
        body = await cardBody(m, db);
      } catch (err) {
        body = `<p class="err">Couldn't render this meeting: ${esc(err.message || err)}</p>` +
          `<div class="actions">${deleteButtons(m.runId)}</div>`;
      }
    }
    div.innerHTML =
      `<div class="mhead" data-action="toggle" data-run="${m.runId}">` +
      `<span class="caret">▶</span>` +
      `<h3>${esc(m.title || "Google Meet")}</h3>` +
      `<span class="date">${esc(fmtDate(m.startedAt))}</span>` +
      `<span class="pill ${esc(m.status)}">${esc(statusText)}</span>` +
      `</div>` +
      `<div class="mbody">${body}</div>`;
    frag.appendChild(div);
  }
  listEl.replaceChildren(frag);
  await renderMermaidIn(listEl);
}

// --------------------------------------------------------------------- actions

async function regenerate(runId, { fromRename = false } = {}) {
  const db = await getStore();
  await db.deleteDocuments(runId);
  await db.updateMeeting(runId, { status: MEETING_STATUS.PROCESSING, stage: "generating", error: null });
  renaming.delete(runId);
  await render();
  chrome.runtime.sendMessage({ type: "REQUEST_PROCESSING", runId }, () => {});
  schedulePoll();
  void fromRename;
}

async function applyRename(runId) {
  const db = await getStore();
  const meeting = await db.getMeeting(runId);
  const transcript = await db.getTranscript(runId);
  if (!transcript) return;
  const map = {};
  for (const input of document.querySelectorAll(`input[data-rename-run="${CSS.escape(runId)}"]`)) {
    map[input.dataset.renameOld] = input.value;
  }
  const updated = renameSpeakers(transcript, map, meeting.title || "");
  await db.putTranscript(runId, updated);
  await regenerate(runId, { fromRename: true });
}

async function handleAction(action, runId, extra) {
  const db = await getStore();
  switch (action) {
    case "toggle":
      openRun.has(runId) ? openRun.delete(runId) : openRun.add(runId);
      return render();
    case "tab":
      activeTab.set(runId, extra.tab);
      return render();
    case "download-md": {
      if (extra.tab === "transcript") {
        const t = await db.getTranscript(runId);
        return downloadText("Transcript.txt", t?.plainText || "");
      }
      const doc = await db.getDocument(runId, extra.tab);
      const base = DOC_META.find(([k]) => k === extra.tab)?.[2] || extra.tab;
      return downloadText(`${base}.md`, doc?.markdown || "");
    }
    case "save-pdf": {
      const doc = await db.getDocument(runId, extra.tab);
      if (doc) await savePdf(esc(doc.title || ""), renderMarkdown(doc.markdown, { mermaid: MERMAID_DOCS.has(extra.tab) }));
      return;
    }
    case "open-rename":
      renaming.add(runId);
      return render();
    case "cancel-rename":
      renaming.delete(runId);
      return render();
    case "apply-rename":
      return applyRename(runId);
    case "regenerate":
      return regenerate(runId);
    case "stop":
      // Flip the row first, then tell the SW to kill the offscreen document
      // (which kills a hung fetch). The row keeps its `stage` — Resume continues.
      await db.updateMeeting(runId, { status: MEETING_STATUS.STOPPED, error: null });
      await render();
      chrome.runtime.sendMessage({ type: "STOP_PROCESSING", runId }, () => {});
      return;
    case "retry":
    case "resume":
      // `retry` (failed row) / `resume` (stopped or wedged-processing row) both
      // flip the row back to `processing` and re-kick — the SW recreates the
      // offscreen doc so a stuck run's in-flight guard can't block it. Stages
      // are idempotent (G4), so a completed step is never repeated.
      await db.updateMeeting(runId, { status: MEETING_STATUS.PROCESSING, error: null });
      await render();
      chrome.runtime.sendMessage({ type: "REQUEST_PROCESSING", runId }, () => {});
      return schedulePoll();
    case "delete":
      confirmingDelete.add(runId);
      return render();
    case "cancel-delete":
      confirmingDelete.delete(runId);
      return render();
    case "confirm-delete":
      confirmingDelete.delete(runId);
      openRun.delete(runId);
      await db.deleteMeeting(runId);
      return render();
    case "reload-page":
      return location.reload();
    default:
      return;
  }
}

listEl.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  e.stopPropagation();
  handleAction(el.dataset.action, el.dataset.run, { tab: el.dataset.tab });
});

document.getElementById("refreshBtn").addEventListener("click", () => render());

// --- diagnostics panel ---
const diagLogEl = document.getElementById("diagLog");
async function loadDiag() {
  try {
    const { debugLog = [] } = await chrome.storage.local.get("debugLog");
    diagLogEl.textContent = debugLog.length
      ? debugLog.map((e) => `${(e.t || "").slice(11, 19)} ${e.source} ${e.event} ${e.detail}`).join("\n")
      : "(empty — record a meeting to populate this)";
  } catch (err) {
    diagLogEl.textContent = "could not read debug log: " + (err.message || err);
  }
}
document.getElementById("diag").addEventListener("toggle", (e) => {
  if (e.target.open) loadDiag();
});
document.getElementById("copyLog").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(diagLogEl.textContent);
    document.getElementById("copyLog").textContent = "Copied ✓";
    setTimeout(() => (document.getElementById("copyLog").textContent = "Copy"), 1500);
  } catch {
    window.prompt("Copy the debug log:", diagLogEl.textContent);
  }
});
document.getElementById("clearLog").addEventListener("click", async () => {
  await chrome.storage.local.set({ debugLog: [] });
  loadDiag();
});
document.getElementById("settingsLink").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: chrome.runtime.getURL("settings.html") });
});
// The idle poll stops once nothing is processing — re-sync whenever the tab
// regains focus (a meeting may have finished in the background).
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) render();
});
window.addEventListener("focus", () => render());

// --------------------------------------------------------------------- live updates

let pollTimer = null;
async function pollOnce() {
  const db = await getStore();
  const busy = (await db.listMeetings()).some((m) => m.status === MEETING_STATUS.PROCESSING || m.status === MEETING_STATUS.RECORDING);
  await render();
  if (!busy && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}
function schedulePoll() {
  if (!pollTimer) pollTimer = setInterval(pollOnce, 4000);
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && /^PROCESSING_/.test(msg.type || "")) {
    render();
    if (msg.type !== "PROCESSING_DONE" && msg.type !== "PROCESSING_FAILED") schedulePoll();
  }
});

// --------------------------------------------------------------------- boot

render().then(pollOnce);
