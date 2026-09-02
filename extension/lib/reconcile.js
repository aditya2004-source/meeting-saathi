/**
 * Speaker-reconciliation recovery pass — port of app/pipeline/speaker_reconcile.py
 * (pure functions) + app/reconcile.py (the trigger/glue).
 *
 * When Gemini's transcription returns only generic "Speaker N" and the DOM
 * active-speaker scrape captured nothing, buildTranscript() produces a set of
 * "Unidentified speaker N" labels. maybeReconcileSpeakers() runs ONE Gemini call
 * over the whole transcript that collapses those labels to the true participant
 * set, assigning a real name only where the transcript unambiguously reveals it.
 *
 * It only fires when placeholder labels dominate the transcript, so a meeting
 * where speakers are already named costs zero extra Gemini calls. Never throws —
 * a failed recovery pass must not fail the pipeline.
 */

import { RECONCILE_RESPONSE_SCHEMA, RECONCILE_SYSTEM_PROMPT } from "./prompts.js";
import { normalizeKey, renderPlainText } from "./transcript.js";

const UNIDENTIFIED_RE = /^Unidentified speaker \d+$/;

// Fire only when at least this many distinct "Unidentified speaker N" labels
// exist AND they cover at least this fraction of the transcript text. On this
// build the Meet DOM scrape often captures nothing, so nearly every meeting
// crosses both — the dominance gate is what spares an already-named meeting.
export const RECONCILE_MIN_LABELS = 2;
export const RECONCILE_MIN_DOMINANCE = 0.4;

export function isUnidentifiedLabel(label) {
  return UNIDENTIFIED_RE.test(label || "");
}

/** Every "Unidentified speaker N" label present, in first-appearance order. */
export function distinctUnidentifiedLabels(segments) {
  const seen = new Set();
  const out = [];
  for (const s of segments) {
    if (isUnidentifiedLabel(s.speaker) && !seen.has(s.speaker)) {
      seen.add(s.speaker);
      out.push(s.speaker);
    }
  }
  return out;
}

/** Fraction of transcript text (by character count) attributed to an
 *  "Unidentified speaker N" label. ~0 for a well-named meeting, near 1.0 for the
 *  failure case this exists to recover from. */
export function placeholderDominance(segments) {
  let total = 0;
  let unidentified = 0;
  for (const s of segments) {
    const n = (s.text || "").length;
    total += n;
    if (isUnidentifiedLabel(s.speaker)) unidentified += n;
  }
  return total === 0 ? 0 : unidentified / total;
}

/** Invert the Gemini pass's participants[].speaker_numbers into a
 *  { "Unidentified speaker N" -> canonical_label } map. Keeps only entries whose
 *  source label is actually in the transcript and whose target differs from it.
 *  First claim wins if two participants list the same number. Never throws. */
export function buildLabelMap(reconciliation, knownLabels) {
  const known = new Set(knownLabels);
  const map = {};
  for (const p of (reconciliation && reconciliation.participants) || []) {
    if (!p || typeof p !== "object") continue;
    const dst = typeof p.canonical_label === "string" ? p.canonical_label.trim() : "";
    if (!dst) continue;
    for (const num of p.speaker_numbers || []) {
      const n = Number(num);
      if (!Number.isInteger(n)) continue;
      const src = `Unidentified speaker ${n}`;
      if (!known.has(src) || src in map || src === dst) continue;
      map[src] = dst;
    }
  }
  return map;
}

/** Rewrite segments + re-key excerpts with the reconciled labels. Returns
 *  { segments, excerpts, realNames } where realNames is the subset of canonical
 *  labels flagged is_real_name that segments actually ended up using. */
export function applyReconciliation(segments, reconciliation, excerpts = {}) {
  const known = distinctUnidentifiedLabels(segments);
  const map = buildLabelMap(reconciliation, known);

  const realNameLabels = new Set(
    ((reconciliation && reconciliation.participants) || [])
      .filter((p) => p && p.is_real_name && typeof p.canonical_label === "string")
      .map((p) => p.canonical_label.trim())
      .filter(Boolean),
  );

  const newSegments = segments.map((s) => (map[s.speaker] ? { ...s, speaker: map[s.speaker] } : s));

  const newExcerpts = {};
  for (const [oldLabel, quote] of Object.entries(excerpts || {})) {
    const newLabel = map[oldLabel] || oldLabel;
    if (realNameLabels.has(newLabel)) continue; // resolved to a real name -> drop excerpt
    if (!(newLabel in newExcerpts)) newExcerpts[newLabel] = quote;
  }

  const used = new Set(newSegments.map((s) => s.speaker));
  const realNames = [...realNameLabels].filter((l) => used.has(l));
  return { segments: newSegments, excerpts: newExcerpts, realNames };
}

/** Add reconciliation-discovered real names to the attendee list, skipping any
 *  already present (case-insensitively, on the bare name before a parenthetical). */
export function mergeRealNames(attendees, realNames) {
  const key = (n) => normalizeKey(n);
  const seen = new Set((attendees || []).map(key));
  const out = [...(attendees || [])];
  for (const name of realNames || []) {
    const k = key(name);
    if (k && !seen.has(k)) {
      seen.add(k);
      out.push(name);
    }
  }
  return out;
}

/**
 * The trigger + call + apply. Returns { segments, excerpts, attendees } —
 * unchanged when reconciliation isn't warranted or the Gemini call fails.
 *
 * @param {object} opts
 * @param {import("./gemini.js").GeminiClient} opts.client
 * @param {string} opts.meetingTitle
 * @param {Array}  opts.segments
 * @param {object} [opts.excerpts]
 * @param {string[]} [opts.attendees]
 * @param {string[]} [opts.roster]     People-panel names, used as a hint
 * @param {Function} [opts.logger]
 */
export async function maybeReconcileSpeakers({
  client,
  meetingTitle,
  segments,
  excerpts = {},
  attendees = [],
  roster = [],
  logger = () => {},
  minLabels = RECONCILE_MIN_LABELS,
  minDominance = RECONCILE_MIN_DOMINANCE,
}) {
  if (!client || !segments || segments.length === 0) {
    return { segments, excerpts, attendees };
  }
  const labels = distinctUnidentifiedLabels(segments);
  if (labels.length < minLabels) return { segments, excerpts, attendees };
  if (placeholderDominance(segments) < minDominance) return { segments, excerpts, attendees };

  const transcriptText = renderPlainText({ meetingTitle, segments });
  let userContent =
    "These are the distinct unidentified-speaker labels in the transcript; map every one of them:\n" +
    labels.map((l) => `- ${l}`).join("\n") +
    "\n\n";
  const rosterHint = (roster || []).filter(Boolean).join("\n").trim();
  if (rosterHint) {
    userContent +=
      "The meeting organiser says the real attendees were (use these names only where the transcript supports them):\n" +
      rosterHint +
      "\n\n";
  }
  userContent += `FULL TRANSCRIPT:\n${transcriptText}`;

  let reconciliation;
  try {
    logger(`reconcile: ${labels.length} unidentified labels, running recovery pass`);
    reconciliation = await client.generateJson(
      RECONCILE_SYSTEM_PROMPT,
      RECONCILE_RESPONSE_SCHEMA,
      userContent,
      16384,
    );
  } catch (err) {
    logger("speaker reconciliation pass failed (kept the labels)", err);
    return { segments, excerpts, attendees };
  }

  const applied = applyReconciliation(segments, reconciliation, excerpts);
  return {
    segments: applied.segments,
    excerpts: applied.excerpts,
    attendees: mergeRealNames(attendees, applied.realNames),
  };
}
