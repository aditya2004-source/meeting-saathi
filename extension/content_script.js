// Runs on meet.google.com pages. Watches for the call actually starting and
// ending. Uses several independent signals rather than one exact string
// match, since Google Meet's DOM/labels change over time and a single wrong
// guess would mean recording silently never starts.

let inCall = false;

// Reloading/updating the extension does NOT re-inject this content script
// into tabs that were already open -- the old instance keeps running with a
// now-dead extension context. Every chrome.runtime.sendMessage() call from
// that orphaned instance throws synchronously ("Extension context
// invalidated"), and since poll() below runs every 2s, an unguarded call
// here means that exact error spamming the console forever. Confirmed the
// hard way: this is exactly what a mid-recording extension reload produces.
// safeSendMessage() catches it once, stops all further polling/observing in
// this dead tab (nothing productive can happen from here anymore -- a fresh
// content script instance needs a real page reload), and tells the user so
// via the same on-page banner used for every other real failure here,
// instead of leaving them looking at a silent, confusing recording that can
// never actually finish.
let contextInvalidated = false;
let pollIntervalId = null;

function safeSendMessage(message) {
  if (contextInvalidated) return;
  try {
    chrome.runtime.sendMessage(message);
  } catch (err) {
    const isContextInvalidated = String((err && err.message) || err).includes("Extension context invalidated");
    if (isContextInvalidated) {
      contextInvalidated = true;
      if (pollIntervalId) clearInterval(pollIntervalId);
      stopSpeakerObserver();
      stopRosterScraper();
      showBanner(
        "Meeting Saathi: the extension was updated/reloaded -- refresh this page to keep recording working.",
        true,
        true
      );
    }
    // Any other error here must never crash this tab's page (Meet itself) --
    // there's nothing else productive to do about a one-off messaging
    // failure that isn't a dead context.
  }
}

// Speaker-name detection (best-effort, only runs while a recording is
// actually in progress -- see extension/DESIGN.md for the full design and
// its fragility caveats). Google Meet has no public API for "who's speaking
// right now" or attendee identity, and its class names are obfuscated and
// carry no "speaking" token. What IS observable: the active speaker's tile
// runs a rapid CSS animation on its audio-level bars, firing many `class`
// mutations per second inside that participant's `[data-participant-id]`
// tile. We detect the speaker as "whichever tile's subtree is currently
// churning class mutations" and read the (doubled) name out of that tile.
// Verified live against Meet's DOM on 2026-09-02.
let recordingActive = false;
let speakerObserver = null;
let activeSpeakerName = null;

const SPEAKER_TILE_SELECTOR = "[data-participant-id]";
// A tile whose subtree fires >= THRESHOLD `class` mutations within WINDOW ms
// is treated as actively speaking (the audio-bars animation). Brief cross-talk
// blips don't sustain enough churn to cross it.
const SPEAKER_PULSE_WINDOW_MS = 900;
const SPEAKER_PULSE_THRESHOLD = 5;
// Don't flip the emitted speaker faster than this (cross-talk guard)...
const SPEAKER_SWITCH_MIN_GAP_MS = 700;
// ...but do re-emit the current speaker at least this often, so a long
// monologue stays within lib/transcript.js's staleness handling.
const SPEAKER_KEEPALIVE_MS = 45 * 1000;

// participantId -> recent `class`-mutation timestamps (rolling, pruned to WINDOW)
const _tilePulseTimes = new Map();
let _lastSpeakerEmit = { name: null, at: 0 };

// The local user's own real name, learned from a People-panel roster row
// whose text ends in "(You)" (see readPeoplePanelRoster()/scrapeRoster()
// below). Meet's tile-speaking indicator only ever exposes "You" for the
// local user's own tile -- without this, the local user's own turns are
// silently dropped instead of attributed to a real name (see
// noteTilePulse() below).
let localUserRealName = null;

// Status/badge text Meet commonly renders as a leaf node alongside (and
// sometimes before) a participant's actual name -- e.g. avatar initials,
// a presenting/mute/pin/raised-hand indicator, or a bare role label. None
// of these are ever a person's name; used by pickNameFromCandidates()
// below to avoid mistaking one for the name itself. Deliberately does NOT
// include "you" -- callers (noteTilePulse(), readPeoplePanelRoster())
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
  // other signal thinks. Uses innerText (not textContent), which only
  // ever reflects rendered/visible text -- naturally immune to the same
  // hidden-stale-element problem isVisible() below exists to guard the
  // other three signals against.
  const text = document.body.innerText || "";
  return /you('| ha)?ve? left the meeting|you left the call|return to home screen/i.test(text);
}

// Signals 1-3 in isInCall() below only ever checked whether a matching
// element exists in the DOM at all -- never whether it's actually
// rendered/visible right now. Google Meet is a heavy SPA; hiding a call's
// leave-button/timer/mic-control (display:none or similar) rather than
// removing it from the DOM after you leave is a completely ordinary SPA
// pattern -- confirmed as the likely cause of a real production bug
// (recording never auto-stopping, requiring a manual click every time):
// a stale, invisible element kept matching forever, so isInCall() could
// never flip back to false once it had returned true. offsetParent is
// null for any element that's display:none (on itself or an ancestor) or
// detached from the document -- a cheap, standard "is this actually
// rendered right now" check.
function isVisible(el) {
  return !!el && el.offsetParent !== null;
}

let _lastIsInCallResult = null;

function isInCall() {
  if (hasLeftMeetingScreen()) {
    _logIsInCallVerdict(false, { hasLeftMeetingScreen: true });
    return false;
  }

  // Signal 1: a VISIBLE control whose label mentions "leave"
  // (case-insensitive, partial match -- covers "Leave call", "Leave the
  // call", wording changes).
  const signal1 = Array.from(document.querySelectorAll('[aria-label*="leave" i]')).some(isVisible);

  // Signal 2: a VISIBLE element showing a live call-duration timer (format
  // like "12:34" or "1:02:03" that Meet shows once you're actually in a
  // call).
  let signal2 = false;
  const timerCandidates = document.querySelectorAll('[jsname], span, div');
  for (const el of timerCandidates) {
    const text = el.textContent?.trim();
    if (text && /^\d{1,2}:\d{2}(:\d{2})?$/.test(text) && el.children.length === 0 && isVisible(el)) {
      signal2 = true;
      break;
    }
  }

  // Signal 3: URL is a real meeting code (meet.google.com/xxx-xxxx-xxx) AND
  // the pre-join "Ready to join" / "Join now" screen is no longer present
  // AND a VISIBLE mic/camera control exists.
  const isMeetingUrl = /^\/[a-z]{3}-[a-z]{4}-[a-z]{3}/i.test(location.pathname);
  const onPreJoinScreen = !!Array.from(document.querySelectorAll("button, span")).find((el) =>
    /join now|ask to join|ready to join/i.test(el.textContent || "")
  );
  const hasVisibleMicOrCameraControl = Array.from(
    document.querySelectorAll('[aria-label*="microphone" i], [aria-label*="camera" i]')
  ).some(isVisible);
  const signal3 = isMeetingUrl && !onPreJoinScreen && hasVisibleMicOrCameraControl;

  const result = signal1 || signal2 || signal3;
  _logIsInCallVerdict(result, { signal1, signal2, signal3, isMeetingUrl, onPreJoinScreen });
  return result;
}

// Logs only on a verdict change (plus once on the very first call, so a
// fresh page load's initial state is visible too) -- poll() runs every 2s
// forever, so logging unconditionally would spam the console into being
// useless. Lets a DevTools console check on the Meet tab show exactly
// which signal is (still) misfiring if this ever needs debugging again,
// instead of guessing blind against a DOM nobody here can see live.
function _logIsInCallVerdict(result, details) {
  if (result === _lastIsInCallResult) return;
  _lastIsInCallResult = result;
  console.debug("[Meeting Saathi] isInCall ->", result, details);
}

// Extracts the participant name from an actively-speaking tile. Verified
// live (2026-09-02): Meet renders a video tile's name twice, newline-joined
// ("Kailash Gupta\nKailash Gupta"). A presentation/screen-share tile that is
// also a [data-participant-id] element instead shows icon labels
// ("zoom_in\nZoom in\nopen_in_new\n..."), so requiring the first two lines
// to be identical both identifies a real participant tile and hands back a
// clean name -- and deliberately returns null for the presentation tile
// rather than risk pulling a name out of one of its tooltips.
function nameFromSpeakerTile(tile) {
  const lines = (tile.innerText || "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  if (lines.length >= 2 && lines[0] && lines[0] === lines[1]) {
    return stripNameSuffix(lines[0]);
  }
  if (
    lines.length === 1 &&
    lines[0].includes(" ") &&
    !NAME_CANDIDATE_IGNORE_RE.test(lines[0])
  ) {
    return stripNameSuffix(lines[0]);
  }
  return null;
}

// Called for every `class` mutation whose target is inside a
// [data-participant-id] tile. Counts how much class churn that tile has seen
// in the last SPEAKER_PULSE_WINDOW_MS; once it crosses SPEAKER_PULSE_THRESHOLD
// the tile's participant is "actively speaking" and we emit a SPEAKER_ACTIVE
// (throttled by SPEAKER_SWITCH_MIN_GAP_MS / SPEAKER_KEEPALIVE_MS).
function noteTilePulse(tile) {
  const pid = tile.getAttribute("data-participant-id");
  if (!pid) return;
  const now = Date.now();
  const times = (_tilePulseTimes.get(pid) || []).filter(
    (t) => now - t < SPEAKER_PULSE_WINDOW_MS
  );
  times.push(now);
  _tilePulseTimes.set(pid, times);
  if (times.length < SPEAKER_PULSE_THRESHOLD) return;

  let name = nameFromSpeakerTile(tile);
  if (!name) return;
  // The local user's own tile can read "You" -- substitute the real name
  // learned from the People-panel scrape, or skip rather than misattribute.
  if (name.toLowerCase() === "you") {
    if (!localUserRealName) return;
    name = localUserRealName;
  }

  const sameSpeaker =
    _lastSpeakerEmit.name && normalizeKey(name) === normalizeKey(_lastSpeakerEmit.name);
  if (sameSpeaker && now - _lastSpeakerEmit.at < SPEAKER_KEEPALIVE_MS) return;
  if (!sameSpeaker && now - _lastSpeakerEmit.at < SPEAKER_SWITCH_MIN_GAP_MS) return;

  _lastSpeakerEmit = { name, at: now };
  activeSpeakerName = name;
  console.log("[Meeting Saathi] active speaker:", name);
  safeSendMessage({ type: "SPEAKER_ACTIVE", name, atMs: now });
}

function onSpeakerMutations(mutations) {
  for (const mutation of mutations) {
    const el = mutation.target;
    if (!el || el.nodeType !== 1 || typeof el.closest !== "function") continue;
    const tile = el.closest(SPEAKER_TILE_SELECTOR);
    if (tile) noteTilePulse(tile);
  }
}

function startSpeakerObserver() {
  if (speakerObserver) return;
  activeSpeakerName = null;
  _tilePulseTimes.clear();
  _lastSpeakerEmit = { name: null, at: 0 };
  speakerObserver = new MutationObserver(onSpeakerMutations);
  speakerObserver.observe(document.body, {
    subtree: true,
    attributes: true,
    attributeFilter: ["class"],
  });
}

function stopSpeakerObserver() {
  if (speakerObserver) {
    speakerObserver.disconnect();
    speakerObserver = null;
  }
  _tilePulseTimes.clear();
  activeSpeakerName = null;
  _lastSpeakerEmit = { name: null, at: 0 };
}

function findPeoplePanelToggleButton() {
  // Best-effort heuristic, NOT a stable API -- same caveat as
  // the speaker detection above: must be re-verified against a live
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
      if (!raw) console.debug("[Meeting Saathi] readPeoplePanelRoster: no name-shaped candidate in row", row);
      continue;
    }

    // This row's un-stripped text carries a "(You)" suffix -- it's the
    // local user's own row. Remember their real (suffix-stripped) name so
    // noteTilePulse() above can substitute it whenever the local
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
  safeSendMessage({ type: "ROSTER_UPDATE", names, atMs: Date.now() });
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
    showBanner("🔴 Meeting Saathi is recording this meeting", false);
    // Only observe for speaker names once a recording is confirmed
    // started (not just "in call") -- ties the timeline's t=0 to the same
    // event background.js anchors recordingStartedAtMs to, and covers the
    // manual-start path for free since it broadcasts this same message.
    recordingActive = true;
    startSpeakerObserver();
    startRosterScraper();
  } else if (message.type === "SARATHI_RECORDING_FAILED") {
    showBanner(`Meeting Saathi could not start recording: ${message.reason}`, true);
    recordingActive = false;
    stopSpeakerObserver();
    stopRosterScraper();
  } else if (message.type === "SARATHI_PROCESSING_STARTED") {
    showBanner("Recording done — transcribing and generating documents with your Gemini key…", false);
    recordingActive = false;
    stopSpeakerObserver();
    stopRosterScraper();
  } else if (message.type === "SARATHI_PROCESSING_DONE") {
    showBanner("Your Minutes of Meeting and Meeting Analysis are ready — open Meeting Saathi.", false);
  } else if (message.type === "SARATHI_UPLOAD_FAILED") {
    showBanner(
      `Meeting Saathi: couldn't process the recording (${message.reason}) — check your internet and your Gemini API key in Settings.`,
      true
    );
    recordingActive = false;
    stopSpeakerObserver();
    stopRosterScraper();
  }
});

function poll() {
  const nowInCall = isInCall();
  if (nowInCall && !inCall) {
    inCall = true;
    safeSendMessage({ type: "MEETING_JOINED", title: getMeetingTitle() });
    // Chrome only allows tabCapture to start in direct response to the user
    // invoking the extension itself -- clicking the toolbar icon, a
    // chrome.commands keyboard shortcut, or a context menu item. A click on
    // a page element injected by this content script does NOT qualify
    // (confirmed the hard way: Chrome rejected it with "Extension has not
    // been invoked for the current page"), so this can only ever be a
    // reminder, never a working button.
    showBanner("👆 Click the Meeting Saathi icon to start recording", false, true);
  } else if (!nowInCall && inCall) {
    inCall = false;
    safeSendMessage({ type: "MEETING_LEFT" });
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

pollIntervalId = setInterval(poll, 2000);
