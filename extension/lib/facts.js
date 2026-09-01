/**
 * Meeting-facts extraction — adapted from app/docgen/engine.py's
 * extract_meeting_facts / _item_key / _union_verified_facts / empty_meeting_facts.
 *
 * Standalone-extension version is call-frugal (the Gemini free tier is tight):
 *   1. combined — the concrete record AND the BA context in ONE call
 *   2. verify   — (quality mode only) re-read against the draft; union kept
 * The verify pass fails soft: its absence never loses the combined pass's facts.
 */

import {
  COMBINED_EXTRACT_RESPONSE_SCHEMA,
  COMBINED_EXTRACT_SYSTEM_PROMPT,
  VERIFY_EXTRACT_RESPONSE_SCHEMA,
  VERIFY_EXTRACT_SYSTEM_PROMPT,
} from "./prompts.js";

export const CORE_LIST_FIELDS = [
  "topics_discussed",
  "decisions",
  "action_items",
  "key_quotes",
  "requirements",
  "risks",
  "assumptions",
  "dependencies",
  "open_questions",
  "commitments",
  "business_processes",
];

const KEY_FIELDS = ["id", "decision", "description", "statement", "question", "commitment", "quote", "process_name"];

function stableStringify(obj) {
  return JSON.stringify(obj, (_k, v) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, v[k]]))
      : v,
  );
}

/** Port of engine.py::_item_key — loose identity key for deduping facts entries. */
export function itemKey(item) {
  let text;
  if (typeof item === "string") {
    text = item;
  } else if (item && typeof item === "object" && !Array.isArray(item)) {
    const hit = KEY_FIELDS.find((k) => item[k]);
    text = hit ? String(item[hit]) : stableStringify(item);
  } else {
    text = String(item);
  }
  return text.toLowerCase().split(/\s+/).filter(Boolean).join(" ").slice(0, 120);
}

/** Port of engine.py::_union_verified_facts. */
export function unionVerifiedFacts(draft, verified) {
  const unsupported = new Set(
    (verified.unsupported_items || []).map((s) => String(s).toLowerCase().split(/\s+/).filter(Boolean).join(" ")),
  );
  const isUnsupported = (item) => {
    const key = itemKey(item);
    for (const u of unsupported) {
      if (u && (key.includes(u) || u.includes(key))) return true;
    }
    return false;
  };

  const merged = { ...draft };
  for (const field of CORE_LIST_FIELDS) {
    const seen = new Map();
    for (const item of [...(draft[field] || []), ...(verified[field] || [])]) {
      if (isUnsupported(item)) continue;
      const k = itemKey(item);
      if (!seen.has(k)) seen.set(k, item);
    }
    merged[field] = [...seen.values()];
  }
  for (const [k, v] of Object.entries(verified)) {
    if (k === "unsupported_items" || CORE_LIST_FIELDS.includes(k)) continue;
    if (v !== null && v !== "" && !(Array.isArray(v) && v.length === 0)) merged[k] = v;
  }
  return merged;
}

/** Port of engine.py::empty_meeting_facts. */
export function emptyMeetingFacts() {
  return {
    attendees: [],
    topics_discussed: [],
    decisions: [],
    action_items: [],
    key_quotes: [],
    requirements: [],
    meeting_purpose: null,
    goals: [],
    current_state: [],
    constraints: [],
    systems_and_integrations: [],
    glossary: [],
    non_functional_notes: [],
    risks: [],
    assumptions: [],
    dependencies: [],
    open_questions: [],
    commitments: [],
    business_processes: [],
  };
}

const EMPTY_CONTEXT = {
  meeting_purpose: null,
  goals: [],
  current_state: [],
  constraints: [],
  systems_and_integrations: [],
  glossary: [],
  non_functional_notes: [],
};

/**
 * @param {import('./gemini.js').GeminiClient} client
 * @param {string} transcriptText
 * @param {{ qualityMode?: boolean, logger?: (msg:string, err?:Error)=>void }} [opts]
 */
export async function extractMeetingFacts(client, transcriptText, { qualityMode = false, logger = console.warn } = {}) {
  let facts = await client.generateJson(
    COMBINED_EXTRACT_SYSTEM_PROMPT,
    COMBINED_EXTRACT_RESPONSE_SCHEMA,
    transcriptText,
    16384,
  );

  if (qualityMode) {
    try {
      const verified = await client.generateJson(
        VERIFY_EXTRACT_SYSTEM_PROMPT,
        VERIFY_EXTRACT_RESPONSE_SCHEMA,
        `DRAFT EXTRACTION:\n${JSON.stringify(facts, null, 2)}\n\nFULL TRANSCRIPT:\n${transcriptText}`,
        16384,
      );
      facts = unionVerifiedFacts(facts, verified);
    } catch (err) {
      logger("extract verify pass failed (kept the draft)", err);
    }
  }

  return { ...EMPTY_CONTEXT, ...emptyMeetingFacts(), ...facts };
}
