// Runs on meet.google.com pages. Watches for the call actually starting and
// ending. Uses several independent signals rather than one exact string
// match, since Google Meet's DOM/labels change over time and a single wrong
// guess would mean recording silently never starts.

let inCall = false;

// Speaker-name detection (best-effort, only runs while a recording is
// actually in progress -- see extension/DESIGN.md for the full design and
// its fragility caveats). Google Meet has no public API for "who's
// speaking right now" or attendee identity, so this scrapes the visual
// "speaking" indicator Meet applies to a participant's tile.
let recordingActive = false;
let speakerObserver = null;
let activeSpeakerName = null;
let pendingSpeakerName = null;
let pendingSpeakerTimer = null;
const SPEAKING_INDICATOR_MIN_DURATION_MS = 300;

// The local user's own real name, learned from a People-panel roster row
// whose text ends in "(You)" (see readPeoplePanelRoster()/scrapeRoster()
// below). Meet's tile-speaking indicator only ever exposes "You" for the
// local user's own tile -- without this, the local user's own turns are
// silently dropped instead of attributed to a real name (see
// findActiveSpeakerName() below).
let localUserRealName = null;

// Status/badge text Meet commonly renders as a leaf node alongside (and
// sometimes before) a participant's actual name -- e.g. avatar initials,
// a presenting/mute/pin/raised-hand indicator, or a bare role label. None
// of these are ever a person's name; used by pickNameFromCandidates()
// below to avoid mistaking one for the name itself. Deliberately does NOT
// include "you" -- callers (findActiveSpeakerName(), readPeoplePanelRoster())
// need to see that literal value to detect the local user's own
// tile/row and substitute their real name, rather than have it silently
// discarded here.
const NAME_CANDIDATE_IGNORE_RE =
  /^(meeting host|contributors|in this meeting|in the meeting|waiting to join|add others|more options|people|mute all|muted|unmuted|mute|unmute|presenting|pinned|unpinned|pin|unpin|spotlighted|raised hand|hand raised|calling|ringing|host|co-host|verified|[a-z]{1,3})$/i;

// Strips one trailing role/status parenthetical Meet sometimes appends to
// an otherwise-real name (e.g. "Aditya Choudhary (You)", "Priya (Host)").
const NAME_TRAILING_SUFFIX_RE = /\s*\((you|host|co-host|presenting)\)\s*$/i;

function stripNameSuffix(raw) {
  return raw.replace(NAME_TRAILING_SUFFIX_RE, "").trim();
}

// Comparison key for "is this the same person" -- mirrors
// app/pipeline/roster.py's normalize_key() (duplicated rather than shared
// across languages). Two scrapes of the same real person often produce
// slightly different strings (extra whitespace, a "(You)"/"(Host)" suffix,
// different casing); exact-match dedup treats each as a distinct person,
// which is what inflated the attendee count before this fix. Comparison
// only -- never used as the stored/displayed name.
function normalizeKey(name) {
  return stripNameSuffix(name.split(/\s+/).filter(Boolean).join(" ")).toLowerCase();
}

// Picks the most name-shaped candidate out of a list of raw leaf-text
// strings found inside one participant row/tile, instead of blindly
// trusting whichever happens to appear first in the DOM (the previous
// approach, which was inconsistent across re-scrapes whenever a badge/
// status leaf rendered before the actual name). Prefers text containing a
// space (real names are usually first+last) over single-word text, and
// rejects known non-name status/badge tokens entirely.
function pickNameFromCandidates(rawTexts) {
  let best = null;
  for (const raw of rawTexts) {
    const trimmed = raw?.trim();
    if (!trimmed) continue;
    // "you" is a special case, not a status/badge token: it's the literal
    // value Meet renders for the local user's own tile/row, and callers
    // rely on seeing it (rather than having it discarded here) to detect
    // that and substitute the local user's real name. Every other bare
    // short/status token is still rejected below.
    const isYou = trimmed.toLowerCase() === "you";
    if (!isYou && NAME_CANDIDATE_IGNORE_RE.test(trimmed)) continue;
    if (!best) {
      best = trimmed;
      continue;
    }
    const bestHasSpace = best.includes(" ");
    const candidateHasSpace = trimmed.includes(" ");
    if (candidateHasSpace && !bestHasSpace) best = trimmed;
  }
  return best ? stripNameSuffix(best) : null;
}

// Picks a name out of one row/tile element: prefers its own aria-label
// (Meet commonly puts the full accessible name there, sometimes with a
// trailing status clause like ", presenting" -- stripped below) over
// scanning leaf text nodes, since an aria-label is far less likely to be a
// stray badge/status string than an arbitrarily-ordered child leaf.
function pickNameFromElement(el) {
  const ariaLabel = el.getAttribute?.("aria-label")?.trim();
  if (ariaLabel) {
    // Meet's aria-labels are often "<name> is speaking" or "<name>,
    // presenting"/"muted"/"pinned" -- strip the trailing status clause
    // (comma- or space-joined) so only the name itself remains. The
    // "is speaking" case matters here specifically: the tile-speaking
    // indicator elements this function is called on are found via
    // `[aria-label*="is speaking" i]`, so their own aria-label commonly
    // carries exactly that suffix.
    const withoutStatusClause = ariaLabel
      .replace(/\s+is speaking\s*$/i, "")
      .replace(/,\s*(presenting|muted|unmuted|pinned).*$/i, "")
      .trim();
    const fromAria = pickNameFromCandidates([withoutStatusClause]);
    if (fromAria) return fromAria;
  }
  const leafTexts = Array.from(el.querySelectorAll?.("span, div") || [])
    .filter((leaf) => leaf.children.length === 0)
    .map((leaf) => leaf.textContent);
  return pickNameFromCandidates(leafTexts);
}

// People-panel attendee roster scrape (best-effort, only runs while a
// recording is in progress -- see extension/DESIGN.md). The speaking-
// indicator scrape above only ever sees someone who actually spoke; a
// participant who joined but stayed silent for the whole meeting is
// otherwise invisible to this extension entirely. Meet's People panel is
// the only place that lists every attendee regardless of whether they
// spoke, but it isn't normally open during a call -- this periodically
// opens it briefly, reads the roster, and restores whatever open/closed
// state it found (never closes a panel the user opened themselves).
let rosterInterval = null;
let rosterInitialTimer = null;
let sentRosterNames = new Set();
const ROSTER_SCRAPE_INTERVAL_MS = 4 * 60 * 1000; // every ~4 minutes
const ROSTER_SCRAPE_INITIAL_DELAY_MS = 10 * 1000; // ~10s after recording starts
const ROSTER_PANEL_SETTLE_MS = 400; // let the panel render rows after opening

function getMeetingTitle() {
  // Meet's document.title is usually "Meet - <name or code>", or just "Meet"
  // before a name is set. Best-effort only -- the popup lets you fix it up
  // manually if this guesses wrong.
  const raw = document.title.replace(/^Meet\s*-\s*/i, "").trim();
  if (raw && raw.toLowerCase() !== "meet") return raw;
  return `Google Meet ${new Date().toLocaleString()}`;
}

function hasLeftMeetingScreen() {
  // Meet's post-call screen ("You've left the meeting", with a "Rejoin"
  // option) stays on the SAME URL as the call itself, and often still
  // shows a mic/camera preview tile for rejoining -- which can otherwise
  // satisfy Signal 3 below and get misread as still being in the call
  // (this is the actual bug that left recordings stuck in "REC" forever
  // after leaving). Checked first, as a hard override: if this screen is
  // showing, we are definitely not in the call, regardless of what any
  // other signal thinks.
  const text = document.body.innerText || "";
  return /you('| ha)?ve? left the meeting|you left the call|return to home screen/i.test(text);
}

function isInCall() {
  if (hasLeftMeetingScreen()) return false;

  // Signal 1: a control whose label mentions "leave" (case-insensitive,
  // partial match -- covers "Leave call", "Leave the call", wording changes).
  if (document.querySelector('[aria-label*="leave" i]')) return true;

  // Signal 2: a control whose label mentions "more options"/"call controls"
  // combined with a live call-duration timer element (format like "12:34" or
  // "1:02:03" that Meet shows once you're actually in a call).
  const timerCandidates = document.querySelectorAll('[jsname], span, div');
  for (const el of timerCandidates) {
    const text = el.textContent?.trim();
    if (text && /^\d{1,2}:\d{2}(:\d{2})?$/.test(text) && el.children.length === 0) {
      return true;
    }
  }

  // Signal 3: URL is a real meeting code (meet.google.com/xxx-xxxx-xxx) AND
  // the pre-join "Ready to join" / "Join now" screen is no longer present.
  const isMeetingUrl = /^\/[a-z]{3}-[a-z]{4}-[a-z]{3}/i.test(location.pathname);
  const onPreJoinScreen = !!Array.from(document.querySelectorAll("button, span")).find((el) =>
    /join now|ask to join|ready to join/i.test(el.textContent || "")
  );
  if (isMeetingUrl && !onPreJoinScreen && document.querySelector('[aria-label*="microphone" i], [aria-label*="camera" i]')) {
    return true;
  }

  return false;
}

function findActiveSpeakerName() {
  // Best-effort heuristic, NOT a stable API -- Meet's class names are
  // obfuscated and change across releases. Anyone maintaining this must
  // re-verify against a live call (devtools open, speak, inspect what
  // actually toggles) before trusting it; this is the same "several
  // independent signals, tolerate wrong guesses" philosophy as isInCall()
  // above, just applied to a harder problem (a specific name tied to a
  // specific moment, not a binary state).
  //
  // Primary signal: Meet toggles some class/attribute containing
  // "speaking" (or an explicit data-is-speaking flag) on the actively
  // talking participant's tile. This may not fire at all during
  // screen-share (tiles reflow/shrink) or for off-screen/virtualized
  // tiles when there are many participants -- both are expected
  // degradations, not bugs; the caller falls back to "Speaker N".
  const indicators = document.querySelectorAll(
    '[class*="speaking" i], [data-is-speaking="true"], [aria-label*="is speaking" i]'
  );
  for (const indicator of indicators) {
    const tile =
      indicator.closest('[data-participant-id], [data-self-name], [data-participant-name], [role="listitem"]') ||
      indicator;
    const explicitName =
      tile.getAttribute?.("data-self-name") || tile.getAttribute?.("data-participant-name");
    const name = (explicitName && explicitName.trim()) || pickNameFromElement(tile);
    if (!name) {
      console.debug("[Sarathi] findActiveSpeakerName: no name-shaped candidate in tile", tile);
      continue;
    }
    // The local user's own tile is commonly labeled "You" -- never a real
    // name to emit as-is. Substitute the real name learned from the
    // People-panel roster scrape (see localUserRealName above) if it's
    // known yet; otherwise skip this indicator entirely rather than
    // silently misattributing the local user's speech to whoever spoke
    // last (the previous behavior).
    if (name.toLowerCase() === "you") {
      if (localUserRealName) return localUserRealName;
      console.debug("[Sarathi] findActiveSpeakerName: local user speaking, real name not yet known");
      continue;
    }
    return name;
  }
  return null;
}

function onPossibleSpeakerChange() {
  const name = findActiveSpeakerName();
  if (name === activeSpeakerName) return;

  // Debounce: require the new state to hold for a short minimum duration
  // before trusting it, so DOM flicker/reflow doesn't emit noisy events.
  if (pendingSpeakerTimer) clearTimeout(pendingSpeakerTimer);
  pendingSpeakerName = name;
  pendingSpeakerTimer = setTimeout(() => {
    pendingSpeakerTimer = null;
    activeSpeakerName = pendingSpeakerName;
    if (activeSpeakerName) {
      chrome.runtime.sendMessage({
        type: "SPEAKER_ACTIVE",
        name: activeSpeakerName,
        atMs: Date.now(),
      });
    }
  }, SPEAKING_INDICATOR_MIN_DURATION_MS);
}

function startSpeakerObserver() {
  if (speakerObserver) return;
  activeSpeakerName = null;
  pendingSpeakerName = null;
  speakerObserver = new MutationObserver(onPossibleSpeakerChange);
  speakerObserver.observe(document.body, {
    subtree: true,
    attributes: true,
    attributeFilter: ["class", "data-is-speaking", "aria-label"],
  });
}

function stopSpeakerObserver() {
  if (speakerObserver) {
    speakerObserver.disconnect();
    speakerObserver = null;
  }
  if (pendingSpeakerTimer) {
    clearTimeout(pendingSpeakerTimer);
    pendingSpeakerTimer = null;
  }
  activeSpeakerName = null;
  pendingSpeakerName = null;
}

function findPeoplePanelToggleButton() {
  // Best-effort heuristic, NOT a stable API -- same caveat as
  // findActiveSpeakerName() above: must be re-verified against a live
  // multi-participant call before being fully trusted.
  return (
    document.querySelector('button[aria-label*="show everyone" i]') ||
    document.querySelector('button[aria-label*="people" i]') ||
    null
  );
}

function findPeoplePanelContainer() {
  return (
    document.querySelector('[role="list"][aria-label*="participants" i]') ||
    document.querySelector('[role="list"][aria-label*="people" i]') ||
    document.querySelector('[aria-label*="participants" i]')
  );
}

function isPeoplePanelOpen() {
  const btn = findPeoplePanelToggleButton();
  if (btn && btn.getAttribute("aria-pressed") === "true") return true;
  return !!findPeoplePanelContainer();
}

function readPeoplePanelRoster() {
  const container = findPeoplePanelContainer();
  if (!container) return [];

  const rows = container.querySelectorAll('[role="listitem"]');
  const names = [];
  const seen = new Set();
  for (const row of rows) {
    const raw = pickNameFromElement(row);
    if (!raw || raw.toLowerCase() === "you") {
      // Bare "You" (no name alongside it) isn't a usable attendee entry --
      // Meet's People panel normally shows the local user's full name with
      // a "(You)" suffix (handled below), this only guards the rare case
      // where nothing else was found in the row.
      if (!raw) console.debug("[Sarathi] readPeoplePanelRoster: no name-shaped candidate in row", row);
      continue;
    }

    // This row's un-stripped text carries a "(You)" suffix -- it's the
    // local user's own row. Remember their real (suffix-stripped) name so
    // findActiveSpeakerName() above can substitute it whenever the local
    // user's tile is the one speaking, instead of silently dropping those
    // turns.
    const rowText = row.getAttribute?.("aria-label")?.trim() || row.textContent || "";
    if (/\(you\)/i.test(rowText)) localUserRealName = raw;

    const key = normalizeKey(raw);
    if (seen.has(key)) continue;
    seen.add(key);
    names.push(raw);
  }
  return names;
}

function sendRosterUpdate(names) {
  const fresh = names.filter((n) => !sentRosterNames.has(normalizeKey(n)));
  if (fresh.length === 0) return;
  fresh.forEach((n) => sentRosterNames.add(normalizeKey(n)));
  // Sends the full current roster each time (not just the new names) --
  // background.js keeps the latest snapshot, so a name dropping off Meet's
  // panel between scrapes (e.g. it briefly failed to render) doesn't lose
  // a name already captured.
  chrome.runtime.sendMessage({ type: "ROSTER_UPDATE", names, atMs: Date.now() });
}

function scrapeRoster() {
  const btn = findPeoplePanelToggleButton();
  if (!btn) {
    // No toggle found -- fall back to a passive read in case the panel is
    // already open some other way (e.g. the user opened it themselves).
    if (isPeoplePanelOpen()) {
      const names = readPeoplePanelRoster();
      if (names.length) sendRosterUpdate(names);
    }
    return;
  }
  const wasOpen = isPeoplePanelOpen();
  if (!wasOpen) btn.click();
  setTimeout(() => {
    const names = readPeoplePanelRoster();
    if (names.length) sendRosterUpdate(names);
    if (!wasOpen) btn.click(); // restore whatever state we found it in
  }, ROSTER_PANEL_SETTLE_MS);
}

function startRosterScraper() {
  if (rosterInterval || rosterInitialTimer) return;
  sentRosterNames = new Set();
  rosterInitialTimer = setTimeout(() => {
    rosterInitialTimer = null;
    scrapeRoster();
  }, ROSTER_SCRAPE_INITIAL_DELAY_MS);
  rosterInterval = setInterval(scrapeRoster, ROSTER_SCRAPE_INTERVAL_MS);
}

function stopRosterScraper() {
  if (rosterInterval) {
    clearInterval(rosterInterval);
    rosterInterval = null;
  }
  if (rosterInitialTimer) {
    clearTimeout(rosterInitialTimer);
    rosterInitialTimer = null;
  }
}

// isError: red, stays until dismissed. persistent (default: same as
// isError): stays until dismissed but isn't styled as an error -- used for
// the "how to start" reminder below, which needs to survive longer than 6s
// without looking like something went wrong.
function showBanner(message, isError, persistent = isError) {
  const existing = document.getElementById("sarathi-meeting-bot-banner");
  if (existing) existing.remove();

  const banner = document.createElement("div");
  banner.id = "sarathi-meeting-bot-banner";
  banner.style.cssText = `
    position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
    z-index: 2147483647; padding: 10px 18px; border-radius: 8px;
    font-family: -apple-system, Helvetica, Arial, sans-serif; font-size: 14px;
    color: white; background: ${isError ? "#c0392b" : persistent ? "#e07b00" : "#1a7a3c"};
    box-shadow: 0 2px 10px rgba(0,0,0,0.3);
    display: flex; align-items: center; gap: 12px;
  `;

  const text = document.createElement("span");
  text.textContent = message;
  banner.appendChild(text);

  if (persistent) {
    // Stays on screen until the user actively dismisses it -- confirmed by
    // direct testing that the old fixed 6s auto-dismiss let a real
    // server-down failure go unnoticed mid-meeting.
    const closeBtn = document.createElement("button");
    closeBtn.textContent = "✕";
    closeBtn.setAttribute("aria-label", "Dismiss");
    closeBtn.style.cssText = `
      background: transparent; border: none; color: white; cursor: pointer;
      font-size: 14px; line-height: 1; padding: 0;
    `;
    closeBtn.addEventListener("click", () => banner.remove());
    banner.appendChild(closeBtn);
  } else {
    setTimeout(() => banner.remove(), 6000);
  }

  document.body.appendChild(banner);
}

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "SARATHI_RECORDING_STARTED") {
    showBanner("🔴 Sarathi Meeting Bot is recording this meeting", false);
    // Only observe for speaker names once a recording is confirmed
    // started (not just "in call") -- ties the timeline's t=0 to the same
    // event background.js anchors recordingStartedAtMs to, and covers the
    // manual-start path for free since it broadcasts this same message.
    recordingActive = true;
    startSpeakerObserver();
    startRosterScraper();
  } else if (message.type === "SARATHI_RECORDING_FAILED") {
    showBanner(`Sarathi Meeting Bot could not start recording: ${message.reason}`, true);
    recordingActive = false;
    stopSpeakerObserver();
    stopRosterScraper();
  } else if (message.type === "SARATHI_UPLOAD_DONE") {
    showBanner("Recording saved — documents are being generated", false);
    recordingActive = false;
    stopSpeakerObserver();
    stopRosterScraper();
  } else if (message.type === "SARATHI_UPLOAD_FAILED") {
    showBanner(`Sarathi Meeting Bot: upload failed (${message.reason}) — is the local server running?`, true);
    recordingActive = false;
    stopSpeakerObserver();
    stopRosterScraper();
  } else if (message.type === "SARATHI_CHUNK_UPLOAD_FAILED") {
    showBanner(
      `Sarathi Meeting Bot: chunk ${message.sequence} failed to upload (${message.reason}) — some audio may be missing`,
      true
    );
  }
});

function poll() {
  const nowInCall = isInCall();
  if (nowInCall && !inCall) {
    inCall = true;
    chrome.runtime.sendMessage({ type: "MEETING_JOINED", title: getMeetingTitle() });
    // Chrome only allows tabCapture to start in direct response to the user
    // invoking the extension itself -- clicking the toolbar icon, a
    // chrome.commands keyboard shortcut, or a context menu item. A click on
    // a page element injected by this content script does NOT qualify
    // (confirmed the hard way: Chrome rejected it with "Extension has not
    // been invoked for the current page"), so this can only ever be a
    // reminder, never a working button.
    showBanner("👆 Click the Sarathi Meeting Bot icon to start recording", false, true);
  } else if (!nowInCall && inCall) {
    inCall = false;
    chrome.runtime.sendMessage({ type: "MEETING_LEFT" });
    const banner = document.getElementById("sarathi-meeting-bot-banner");
    if (banner) banner.remove(); // left before starting -- nothing left to remind about
    // Safety net in case a STARTED/DONE/FAILED message was ever missed --
    // never leave the observer running once we're clearly out of the call.
    if (recordingActive) {
      recordingActive = false;
      stopSpeakerObserver();
      stopRosterScraper();
    }
  }
}

setInterval(poll, 2000);
