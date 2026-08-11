// Polls GET /meetings/{id}/status every few seconds and updates each
// meeting's card in place -- replaces the old blunt 10s full-page
// <meta http-equiv="refresh">, so the page updates live without ever
// flashing/reloading and without losing scroll position or an open
// <details> panel. Kept as one small vanilla-JS file (first JS file in
// this project) rather than pulling in a framework for what's a handful
// of DOM writes -- see app/progress.py for what each "progress" field
// means.
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

  function renderResult(resultEl, run, progress) {
    clearChildren(resultEl);
    if (!progress.completed || !progress.folder_path) return;
    const box = document.createElement("div");
    box.className = "result-box";
    const banner = document.createElement("span");
    banner.className = "completed-banner";
    banner.textContent = "✅ Documents ready";
    const code = document.createElement("code");
    code.textContent = progress.folder_path;
    box.appendChild(banner);
    box.appendChild(code);
    resultEl.appendChild(box);
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

    if (TERMINAL_STATES.has(run.state)) {
      card.dataset.polling = "0";
    }
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
