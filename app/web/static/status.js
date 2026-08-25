// Polls GET /meetings/{id}/status every few seconds and updates each
// meeting's card in place -- replaces the old blunt 10s full-page
// <meta http-equiv="refresh">, so the page updates live without ever
// flashing/reloading and without losing scroll position or an open
// <details> panel. Kept as one small vanilla-JS file rather than pulling in
// a framework for what's a handful of DOM writes -- see app/progress.py for
// what each "progress" field means, and app/docgen/registry.py for the
// per-document catalogue (progress.documents) this also renders.
(function () {
  "use strict";

  const POLL_INTERVAL_MS = 3000;
  const ELAPSED_TICK_MS = 1000;
  const TERMINAL_STATES = new Set(["saved", "failed"]);

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const minutes = Math.floor(total / 60);
    const secs = total % 60;
    return minutes > 0 ? `${minutes}m ${secs}s` : `${secs}s`;
  }

  function formatEta(seconds) {
    if (seconds == null) return "";
    return `~${formatDuration(seconds)} remaining`;
  }

  function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  // Every document (MOM, BRD, FRD, User Stories, Acceptance Criteria,
  // Business Process Flow, ...) is generated on demand now, not
  // automatically -- see app/docgen/registry.py. This renders one row per
  // document with a status-appropriate action: a "Generate"/"Retry" button,
  // "Generating…", a download link, or "Not applicable to this meeting".
  function renderDocumentCatalogue(container, runId, documents) {
    clearChildren(container);
    for (const doc of documents || []) {
      const row = document.createElement("div");
      row.className = `document-row document-status-${doc.status}`;
      row.dataset.docKey = doc.key;

      const label = document.createElement("span");
      label.className = "document-label";
      label.textContent = doc.label;
      row.appendChild(label);

      const action = document.createElement("span");
      action.className = "document-action";
      if (doc.status === "ready") {
        const a = document.createElement("a");
        a.href = `/meetings/${encodeURIComponent(runId)}/files/${doc.pdf_filename}`;
        a.textContent = "Download";
        action.appendChild(a);
      } else if (doc.status === "generating") {
        const span = document.createElement("span");
        span.className = "muted";
        span.textContent = "Generating…";
        action.appendChild(span);
      } else if (doc.status === "unavailable") {
        const span = document.createElement("span");
        span.className = "muted";
        span.textContent = "Not applicable to this meeting";
        action.appendChild(span);
      } else {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "generate-btn";
        btn.dataset.runId = runId;
        btn.dataset.docKey = doc.key;
        btn.textContent = doc.status === "failed" ? "Retry" : "Generate";
        action.appendChild(btn);
        if (doc.status === "failed" && doc.error) {
          const err = document.createElement("span");
          err.className = "document-error";
          err.textContent = doc.error;
          action.appendChild(err);
        }
      }
      row.appendChild(action);
      container.appendChild(row);
    }
  }

  function renderResult(resultEl, run, progress) {
    clearChildren(resultEl);
    if (!progress.folder_path) return;

    const box = document.createElement("div");
    box.className = "result-box";

    const availableFiles = progress.available_files || [];
    if (availableFiles.includes("transcript.txt")) {
      const links = document.createElement("div");
      links.className = "download-links";
      const a = document.createElement("a");
      a.href = `/meetings/${encodeURIComponent(run.id)}/files/transcript.txt`;
      a.textContent = "Full transcript";
      links.appendChild(a);
      box.appendChild(links);
    }

    if (progress.documents && progress.documents.length) {
      const catalogue = document.createElement("div");
      catalogue.className = "document-catalogue";
      catalogue.dataset.runId = run.id;
      box.appendChild(catalogue);
      renderDocumentCatalogue(catalogue, run.id, progress.documents);
    }

    resultEl.appendChild(box);
  }

  // A run's own DB state reaches "saved" as soon as the transcript + facts
  // exist -- much earlier than before, since no document generates
  // automatically anymore. Polling must keep going past that point while any
  // document is actively generating, or a "Generate" click's result would
  // never show up without a manual page reload.
  function anyDocumentGenerating(progress) {
    return (progress.documents || []).some((doc) => doc.status === "generating");
  }

  function applyRun(card, run) {
    const progress = run.progress || {};

    card.className = `meeting-card state-${run.state}`;

    const badge = card.querySelector(".status-badge");
    if (badge) {
      badge.textContent = progress.status || run.state;
      badge.className = `badge status-badge status-${(progress.status || run.state).toLowerCase()}`;
    }

    const label = card.querySelector(".status-label");
    if (label) label.textContent = run.state !== "failed" && run.state !== "saved" ? progress.label || "" : "";

    const bar = card.querySelector(".progress-bar");
    const fill = card.querySelector(".progress-bar-fill");
    if (bar && fill) {
      const hasPercent = progress.percent != null;
      bar.classList.toggle("hidden", !hasPercent);
      fill.style.width = `${hasPercent ? progress.percent : 0}%`;
    }

    const percentEl = card.querySelector(".status-percent");
    if (percentEl) percentEl.textContent = progress.percent != null ? `${progress.percent}%` : "";

    const etaEl = card.querySelector(".status-eta");
    if (etaEl) etaEl.textContent = formatEta(progress.eta_seconds);

    const recordedEl = card.querySelector(".status-recorded");
    if (recordedEl) {
      const noAudio = progress.recorded === false;
      recordedEl.textContent = noAudio ? "⚠ No audio was captured for this meeting" : "";
      recordedEl.classList.toggle("warning", noAudio);
    }

    const errorEl = card.querySelector(".status-error");
    if (errorEl) errorEl.textContent = run.error_message || "";

    const resultEl = card.querySelector(".status-result");
    if (resultEl) renderResult(resultEl, run, progress);

    card.dataset.polling = TERMINAL_STATES.has(run.state) && !anyDocumentGenerating(progress) ? "0" : "1";
  }

  async function pollCard(card) {
    const runId = card.dataset.runId;
    try {
      const response = await fetch(`/meetings/${encodeURIComponent(runId)}/status`);
      if (!response.ok) return;
      const run = await response.json();
      applyRun(card, run);
    } catch (err) {
      // Transient network hiccup (server restarting mid-meeting, etc.) --
      // the next interval retries on its own, nothing to surface here.
    }
  }

  function poll() {
    document.querySelectorAll(".meeting-card[data-run-id]").forEach((card) => {
      if (card.dataset.polling !== "0") pollCard(card);
    });
  }

  // Ticks every second (independent of the 3s network poll above) so
  // "how long has this taken so far" feels live rather than jumping in
  // 3-second steps -- purely a DOM text update, no network cost.
  function tickElapsed() {
    const now = Date.now();
    document.querySelectorAll(".meeting-card[data-run-id]").forEach((card) => {
      if (card.dataset.polling === "0") return; // terminal -- elapsed no longer meaningful
      const elapsedEl = card.querySelector(".status-elapsed");
      if (!elapsedEl) return;
      const createdAt = Date.parse(card.dataset.createdAt);
      if (Number.isNaN(createdAt)) return;
      elapsedEl.textContent = `${formatDuration((now - createdAt) / 1000)} elapsed`;
    });
  }

  // Event delegation on the whole document -- "Generate"/"Retry" buttons are
  // re-created on every renderDocumentCatalogue() call, so a per-element
  // listener would need re-attaching each time; one listener up front avoids
  // that entirely.
  document.addEventListener("click", async (event) => {
    const btn = event.target.closest(".generate-btn");
    if (!btn) return;
    const runId = btn.dataset.runId;
    const docKey = btn.dataset.docKey;
    btn.disabled = true;
    btn.textContent = "Generating…";
    try {
      await fetch(`/meetings/${encodeURIComponent(runId)}/documents/${encodeURIComponent(docKey)}/generate`, {
        method: "POST",
      });
    } catch (err) {
      // Ignored -- the next poll will show whatever the server's actual
      // state is, including a "failed" status with an error if this request
      // itself didn't even land.
    }
    const card = document.querySelector(`.meeting-card[data-run-id="${runId}"]`);
    if (card) {
      card.dataset.polling = "1"; // resume polling even if this card's run was terminal
      pollCard(card);
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".meeting-card[data-run-id]").forEach((card) => {
      card.dataset.polling = TERMINAL_STATES.has(card.dataset.state) ? "0" : "1";
    });
    poll();
    tickElapsed();
    setInterval(poll, POLL_INTERVAL_MS);
    setInterval(tickElapsed, ELAPSED_TICK_MS);
  });
})();
