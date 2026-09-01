/**
 * Transcript post-processing — ports of app/pipeline/merge.py, speaker_names.py,
 * and roster.py to plain ES modules.
 *
 * In the standalone extension the transcript comes from Gemini (T1), already
 * segmented as [{ start, end, speaker, text }]. Gemini's own speaker labels are
 * often generic ("Speaker 1", "Speaker 2", ...) — resolveSpeakerNames() is the
 * *fallback* that maps those to real names using the content script's DOM
 * "who's speaking" events, exactly as the Python pipeline did for pyannote's
 * placeholder labels. computeAttendees() merges the People-panel roster with
 * confidently-named speakers.
 *
 * A "segment" here is a plain object { start:number, end:number, speaker:string,
 * text:string } — the JS stand-in for Python's SpeakerSegment.
 */

const REPORT_TIMEZONE = "Asia/Kolkata";

// The local user's own Meet tile is commonly "You" — never a real name.
const IGNORED_NAMES = new Set(["you"]);

// Matches diarize.py's placeholder shapes AND Gemini's generic diarization
// output: "Speaker 1", "Speaker 12", and the legacy chunk-tagged
// "Speaker 1 (chunk@123.4)". Never matches a real name.
const PLACEHOLDER_RE = /^Speaker \d+(?: \(chunk@[0-9.]+\))?$/;

// Strips one trailing role/status parenthetical Meet appends to a name
// ("Priya (Host)", "Aditya Choudhary (You)") — comparison key only.
const TRAILING_PAREN_RE = /\s*\([^()]*\)\s*$/;

// The terminal "we couldn't name this voice" labels (mirrors engine.py's
// _ANON_ATTENDEE_RE) — never a real attendee name.
const ANON_SPEAKER_RE = /^(Unidentified speaker|Participant)\b/i;

export function isPlaceholderSpeaker(label) {
  return label === "Unknown" || PLACEHOLDER_RE.test(label || "");
}

/**
 * Comparison key for "is this the same person" — collapse whitespace, strip one
 * trailing parenthetical, casefold. Never used as a displayed name.
 * (Shared shape of roster.py::normalize_key and speaker_names.py::_normalize_key.)
 */
export function normalizeKey(name) {
  const collapsed = String(name || "").split(/\s+/).filter(Boolean).join(" ");
  const stripped = collapsed.replace(TRAILING_PAREN_RE, "").trim();
  return (stripped || collapsed).toLowerCase();
}

// ---------------------------------------------------------------------------
// Speaker events (DOM "who's speaking" timeline from the content script)
// ---------------------------------------------------------------------------

/**
 * Best-effort parse of the extension's speaker-events list. Accepts an array
 * (already-parsed) or a JSON string. Malformed input yields [] — never throws.
 * Returns [{ name:string, tSeconds:number }].
 */
export function parseSpeakerEvents(raw) {
  let data = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(data)) return [];

  const events = [];
  for (const item of data) {
    if (!item || typeof item !== "object") continue;
    const name = item.name;
    if (typeof name !== "string" || !name.trim()) continue;
    if (IGNORED_NAMES.has(name.trim().toLowerCase())) continue;
    // Python reads `t_seconds`; also accept camelCase `tSeconds`.
    const rawT = item.t_seconds ?? item.tSeconds;
    const tSeconds = Number(rawT);
    if (!Number.isFinite(tSeconds)) continue;
    events.push({ name: name.trim(), tSeconds });
  }
  return events;
}

/**
 * Port of speaker_names.py::resolve_speaker_names. Groups segments by their
 * placeholder label, majority-votes a real name for each group from events
 * whose timestamp falls within tolerance of the group's segments, and merges
 * multiple groups that confidently vote the same name. A group with no
 * confident match keeps its placeholder — the permanent, intentional fallback.
 *
 * Never touches "Unknown" segments or segments that already carry a real name.
 */
export function resolveSpeakerNames(segments, events, { toleranceSeconds = 1.0, minConfidence = 0.5 } = {}) {
  if (!events || events.length === 0) return segments;

  const groups = new Map(); // placeholder -> [segment]
  for (const seg of segments) {
    if (!isPlaceholderSpeaker(seg.speaker) || seg.speaker === "Unknown") continue;
    if (!groups.has(seg.speaker)) groups.set(seg.speaker, []);
    groups.get(seg.speaker).push(seg);
  }

  const globalDisplay = new Map(); // normKey -> first-seen original-cased name
  const candidateKeys = new Map(); // placeholder -> winning normKey

  for (const [placeholder, groupSegments] of groups) {
    const counts = new Map();
    let total = 0;
    for (const seg of groupSegments) {
      const windowStart = seg.start - toleranceSeconds;
      const windowEnd = seg.end + toleranceSeconds;
      for (const event of events) {
        if (windowStart <= event.tSeconds && event.tSeconds <= windowEnd) {
          const key = normalizeKey(event.name);
          counts.set(key, (counts.get(key) || 0) + 1);
          if (!globalDisplay.has(key)) globalDisplay.set(key, event.name);
          total += 1;
        }
      }
    }
    if (total === 0) continue;
    let winningKey = null;
    let votes = -1;
    for (const [key, n] of counts) {
      if (n > votes) {
        votes = n;
        winningKey = key;
      }
    }
    if (votes / total >= minConfidence) candidateKeys.set(placeholder, winningKey);
  }

  if (candidateKeys.size === 0) return segments;

  const resolved = new Map();
  for (const [placeholder, key] of candidateKeys) {
    resolved.set(placeholder, globalDisplay.get(key));
  }

  return segments.map((seg) => (resolved.has(seg.speaker) ? { ...seg, speaker: resolved.get(seg.speaker) } : seg));
}

// ---------------------------------------------------------------------------
// Roster / attendees
// ---------------------------------------------------------------------------

/**
 * Port of roster.py::parse_attendee_roster. Accepts an array or JSON string;
 * a flat list of name strings or a list of { name } objects. Filters "You",
 * dedupes by normalizeKey. Malformed input -> [].
 */
export function parseAttendeeRoster(raw) {
  let data = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(data)) return [];

  const names = [];
  const seen = new Set();
  for (const item of data) {
    let name;
    if (typeof item === "string") name = item;
    else if (item && typeof item === "object") name = item.name;
    else continue;
    if (typeof name !== "string" || !name.trim()) continue;
    const cleaned = name.trim();
    const key = normalizeKey(cleaned);
    if (IGNORED_NAMES.has(key) || seen.has(key)) continue;
    seen.add(key);
    names.push(cleaned);
  }
  return names;
}

/**
 * Port of roster.py::compute_attendees. Roster first (in Meet's reported
 * order), then any additionally-detected real speaker name not already on it.
 * Never includes an unresolved placeholder ("Speaker N" / "Unknown").
 */
export function computeAttendees(roster, segments) {
  const seen = new Set(roster.map((n) => normalizeKey(n)));
  const extra = [];
  for (const seg of segments) {
    const name = seg.speaker;
    if (isPlaceholderSpeaker(name)) continue;
    const key = normalizeKey(name);
    if (seen.has(key)) continue;
    seen.add(key);
    extra.push(name);
  }
  return [...roster, ...extra];
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

/** Port of merge.py::_format_timestamp — seconds -> "HH:MM:SS". */
export function formatTimestamp(seconds) {
  const total = Math.trunc(seconds) || 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(hours)}:${pad(minutes)}:${pad(secs)}`;
}

/**
 * Port of merge.py::render_plain_text — the `[HH:MM:SS] Speaker: text` body
 * that every Gemini generation/extraction call is grounded on.
 * `transcript` is { meetingTitle, segments: [{ start, speaker, text }] }.
 */
export function renderPlainText(transcript) {
  const lines = [transcript.meetingTitle || transcript.meeting_title || "", ""];
  for (const seg of transcript.segments) {
    lines.push(`[${formatTimestamp(seg.start)}] ${seg.speaker}: ${seg.text}`);
  }
  return lines.join("\n");
}

/** Distinct speaker labels in first-appearance order. */
export function distinctSpeakers(segments) {
  const seen = new Set();
  const out = [];
  for (const s of segments || []) {
    if (!seen.has(s.speaker)) {
      seen.add(s.speaker);
      out.push(s.speaker);
    }
  }
  return out;
}

/**
 * Apply a { oldLabel: newName } map to a stored transcript — used by the
 * dashboard's "rename speakers" step before regenerating the documents.
 * Blank/whitespace target names and no-op entries are ignored. Rebuilds
 * `attendees` (renamed + deduped, then any newly-real name that wasn't there)
 * and `plainText`.
 *
 * @param {{segments:Array,attendees?:Array,excerpts?:object}} transcript
 * @param {Record<string,string>} map
 * @param {string} meetingTitle
 */
export function renameSpeakers(transcript, map, meetingTitle = "") {
  const clean = {};
  for (const [k, v] of Object.entries(map || {})) {
    const name = String(v || "").trim();
    if (name && name !== k) clean[k] = name;
  }
  const segments = (transcript.segments || []).map((s) => (clean[s.speaker] ? { ...s, speaker: clean[s.speaker] } : s));

  const seen = new Set();
  const attendees = [];
  for (const a of transcript.attendees || []) {
    const renamed = clean[a] || a;
    const key = normalizeKey(renamed);
    if (!seen.has(key)) {
      seen.add(key);
      attendees.push(renamed);
    }
  }
  for (const seg of segments) {
    if (isPlaceholderSpeaker(seg.speaker) || ANON_SPEAKER_RE.test(seg.speaker)) continue;
    const key = normalizeKey(seg.speaker);
    if (!seen.has(key)) {
      seen.add(key);
      attendees.push(seg.speaker);
    }
  }

  return {
    segments,
    attendees,
    excerpts: transcript.excerpts || {},
    plainText: renderPlainText({ meetingTitle: meetingTitle || transcript.meetingTitle || "", segments }),
  };
}

// ---------------------------------------------------------------------------
// Unresolved-placeholder fill (port of speaker_names.py::fill_unresolved_with_excerpts)
// ---------------------------------------------------------------------------

const EXCERPT_MAX_CHARS = 160;

function makeExcerpt(texts, maxLines = 3) {
  const lines = texts.slice(0, maxLines).map((t) => String(t).trim()).filter(Boolean);
  let excerpt = lines.length ? lines.join(" / ") : "(no transcribed speech)";
  if (excerpt.length > EXCERPT_MAX_CHARS) excerpt = excerpt.slice(0, EXCERPT_MAX_CHARS - 3).replace(/\s+$/, "") + "...";
  return excerpt;
}

/**
 * Terminal guarantee, called after resolveSpeakerNames(): no bare "Speaker N"
 * or "Unknown" placeholder reaches the grounded transcript. Each still-generic
 * "Speaker N" group becomes one "Unidentified speaker K"; each "Unknown"
 * segment gets its own. Returns { segments, excerpts } — excerpts is for a
 * human's later reference only, never fed to Gemini or a client PDF.
 */
export function fillUnresolvedWithExcerpts(segments) {
  const groupTexts = new Map();
  for (const seg of segments) {
    if (isPlaceholderSpeaker(seg.speaker) && seg.speaker !== "Unknown") {
      if (!groupTexts.has(seg.speaker)) groupTexts.set(seg.speaker, []);
      groupTexts.get(seg.speaker).push(seg.text);
    }
  }

  let counter = 0;
  const groupLabels = new Map();
  const excerpts = {};
  for (const [placeholder, texts] of groupTexts) {
    counter += 1;
    const label = `Unidentified speaker ${counter}`;
    groupLabels.set(placeholder, label);
    excerpts[label] = makeExcerpt(texts);
  }

  const result = [];
  for (const seg of segments) {
    if (seg.speaker === "Unknown") {
      counter += 1;
      const label = `Unidentified speaker ${counter}`;
      excerpts[label] = makeExcerpt([seg.text]);
      result.push({ ...seg, speaker: label });
    } else if (groupLabels.has(seg.speaker)) {
      result.push({ ...seg, speaker: groupLabels.get(seg.speaker) });
    } else {
      result.push(seg);
    }
  }
  return { segments: result, excerpts };
}

/**
 * End-to-end: raw Gemini transcribe JSON -> the grounded transcript object the
 * extract/generate calls consume.
 *
 * @param {object} raw   Gemini output: { segments:[{start_seconds,end_seconds,speaker,text}], attendees? }
 * @param {object} opts
 * @param {string} opts.meetingTitle
 * @param {Array}  [opts.speakerEvents]  DOM "who's speaking" events (name + t_seconds)
 * @param {Array}  [opts.roster]         People-panel names
 * @returns {{ segments, attendees, plainText, excerpts }}
 */
export function buildTranscript(raw, { meetingTitle, speakerEvents = [], roster = [] } = {}) {
  let segments = (raw.segments || [])
    .map((s) => ({
      start: Number(s.start_seconds ?? s.start ?? 0),
      end: Number(s.end_seconds ?? s.end ?? 0),
      speaker: String(s.speaker ?? "Speaker 1").trim() || "Speaker 1",
      text: String(s.text ?? "").trim(),
    }))
    .filter((s) => s.text);

  const events = parseSpeakerEvents(speakerEvents);
  segments = resolveSpeakerNames(segments, events);

  // Attendees must be computed from the resolved-but-not-yet-filled segments —
  // compute_attendees() must never see an "Unidentified speaker N" label
  // (matches app/orchestrator_streaming.py's ordering).
  const rosterNames = parseAttendeeRoster(roster);
  let attendees = computeAttendees(rosterNames, segments);
  if (attendees.length === 0 && Array.isArray(raw.attendees)) {
    // Last resort: Gemini's own attendee guess, minus anything placeholder-shaped.
    attendees = raw.attendees.map((a) => String(a).trim()).filter((a) => a && !isPlaceholderSpeaker(a));
  }

  const { segments: filled, excerpts } = fillUnresolvedWithExcerpts(segments);
  segments = filled;

  const plainText = renderPlainText({ meetingTitle, segments });
  return { segments, attendees, plainText, excerpts };
}

const TZ_ABBREV = { "Asia/Kolkata": "IST" };

/**
 * Port of merge.py::format_meeting_date. Renders a UTC ISO8601 timestamp as a
 * clean local string, e.g. "28 July 2026, 2:30 PM IST". Malformed input is
 * returned unchanged (display convenience, never fails).
 */
export function formatMeetingDate(startedAtIso, timeZone = REPORT_TIMEZONE) {
  if (typeof startedAtIso !== "string" || !startedAtIso.trim()) return startedAtIso;
  const dt = new Date(startedAtIso);
  if (Number.isNaN(dt.getTime())) return startedAtIso;

  const datePart = new Intl.DateTimeFormat("en-GB", {
    timeZone,
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(dt);

  let timePart = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(dt);
  // en-US gives "2:30 PM"; normalise any narrow no-break space.
  timePart = timePart.replace(/ /g, " ");

  let abbrev = TZ_ABBREV[timeZone];
  if (!abbrev) {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "short" }).formatToParts(dt);
    abbrev = parts.find((p) => p.type === "timeZoneName")?.value || "";
  }

  return abbrev ? `${datePart}, ${timePart} ${abbrev}` : `${datePart}, ${timePart}`;
}
