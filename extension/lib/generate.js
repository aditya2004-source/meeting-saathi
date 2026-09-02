/**
 * Document generation — port of app/docgen/engine.py's _generate_document /
 * _refine_markdown_document / _attendees_for_prompt / _placeholder_doc and the
 * generate_mom / generate_meeting_analysis entry points.
 *
 * V1: MOM + Meeting Analysis.
 */

import { cleanMarkdownBody } from "./gemini.js";
import {
  BUSINESS_PROCESS_FLOW_RESPONSE_SCHEMA,
  BUSINESS_PROCESS_FLOW_SYSTEM_PROMPT,
  COMBINED_DOCS_RESPONSE_SCHEMA,
  COMBINED_DOCS_SYSTEM_PROMPT,
  MEETING_ANALYSIS_RESPONSE_SCHEMA,
  MEETING_ANALYSIS_SYSTEM_PROMPT,
  MOM_RESPONSE_SCHEMA,
  MOM_SYSTEM_PROMPT,
} from "./prompts.js";
import { reviewMarkdownDocument } from "./validate.js";

const ANON_ATTENDEE_RE = /^(Unidentified speaker|Participant)\b/i;
const NO_SPEECH_NOTE = "No speech was captured during this meeting, so there is nothing to summarize.";

const REFINE_INSTRUCTION =
  "You produced the DRAFT DOCUMENT below. Produce an improved final version:\n" +
  "- Fix every issue in AUTOMATED CHECK FINDINGS (if any).\n" +
  "- Make every section complete: if a section is thin but the topic clearly got " +
  "real discussion, expand it from the EXTRACTED FACTS and TRANSCRIPT (never invent).\n" +
  "- This is a client-facing document: never write an internal requirement id " +
  "(REQ-1, [REQ-3], ...), JSON field name, or bracketed tag -- use plain words.\n" +
  "- Remove anything not supported by the facts or transcript.\n" +
  "- Keep the exact section structure from your instructions; emit no heading that " +
  "isn't in that structure, no code fence, no JSON, no <|...|> marker.\n" +
  "Return the full corrected document (same schema).\n\n";

const REFINE_INSTRUCTION_WITH_DIAGRAMS =
  "You produced the DRAFT DOCUMENT below. Produce an improved final version:\n" +
  "- Fix every issue in AUTOMATED CHECK FINDINGS (if any).\n" +
  "- Make every section complete: if a section is thin but the topic clearly got " +
  "real discussion, expand it from the EXTRACTED FACTS and TRANSCRIPT (never invent).\n" +
  "- This is a client-facing document: never write an internal requirement id, JSON " +
  "field name, or bracketed tag -- use plain words.\n" +
  "- Remove anything not supported by the facts or transcript.\n" +
  "- Keep the exact section structure from your instructions.\n" +
  "- Keep each ```mermaid diagram as a fenced ```mermaid block; keep every diagram " +
  "dead simple and syntactically valid (about 8 nodes at most, mostly a straight " +
  "top-to-bottom line, short quoted labels). Emit no other code fence, no JSON, no " +
  "<|...|> marker.\n" +
  "Return the full corrected document (same schema).\n\n";

/** Port of engine.py::_attendees_for_prompt. */
export function attendeesForPrompt(attendees) {
  if (attendees && attendees.length && attendees.every((a) => ANON_ATTENDEE_RE.test(a || ""))) {
    const n = attendees.length;
    const speakerWord = n === 1 ? "speaker" : "distinct speakers";
    return [`${n} ${speakerWord} took part; individual names could not be identified from the meeting audio`];
  }
  return attendees;
}

/** Port of engine.py::_placeholder_doc. */
export function placeholderDoc(title, meetingTitle, meetingDate, note) {
  const headingSuffix = meetingDate ? ` (${meetingDate})` : "";
  return { title, markdown_body: `# ${title} — ${meetingTitle}${headingSuffix}\n\n${note}\n` };
}

/** Port of engine.py::_refine_markdown_document. */
async function refineMarkdownDocument(client, {
  systemPrompt,
  responseSchema,
  docKey,
  draft,
  groundingJson,
  transcriptText,
  facts,
  maxOutputTokens,
  withDiagrams = false,
  logger = console.warn,
}) {
  const draftBody = draft.markdown_body;
  if (typeof draftBody !== "string" || !draftBody.trim()) return draft;

  const findings = reviewMarkdownDocument(docKey, draftBody, facts);
  const findingsBlock = findings.length ? findings.map((f) => `- ${f}`).join("\n") : "- (no automated findings)";
  const userContent =
    `${withDiagrams ? REFINE_INSTRUCTION_WITH_DIAGRAMS : REFINE_INSTRUCTION}` +
    `AUTOMATED CHECK FINDINGS:\n${findingsBlock}\n\n` +
    `DRAFT DOCUMENT:\n${draftBody}\n\n` +
    `EXTRACTED FACTS:\n${groundingJson}\n\n` +
    `FULL TRANSCRIPT:\n${transcriptText}`;

  let refined;
  try {
    refined = await client.generateJson(systemPrompt, responseSchema, userContent, maxOutputTokens);
  } catch (err) {
    logger("document refine pass failed (kept the draft)", err);
    return draft;
  }
  const refinedBody = refined.markdown_body;
  if (typeof refinedBody !== "string" || refinedBody.trim().length < 0.6 * draftBody.trim().length) {
    return draft;
  }
  refined.markdown_body = cleanMarkdownBody(refinedBody);
  return refined;
}

/** Port of engine.py::_generate_document. */
export async function generateDocument(client, {
  systemPrompt,
  responseSchema,
  meetingTitle,
  meetingDate,
  attendees,
  facts,
  transcriptText,
  maxOutputTokens = 8192,
  docKey = "",
  qualityMode = true,
  withDiagrams = false,
  logger = console.warn,
}) {
  const grounding = {
    meeting_title: meetingTitle,
    meeting_date: meetingDate,
    attendees: attendeesForPrompt(attendees),
    extracted_facts: facts,
  };
  const groundingJson = JSON.stringify(grounding, null, 2);
  const userContent =
    "Here is the grounding data (JSON) and the full transcript. Only use facts present here.\n\n" +
    `EXTRACTED FACTS:\n${groundingJson}\n\n` +
    `FULL TRANSCRIPT:\n${transcriptText}`;

  let result = await client.generateJson(systemPrompt, responseSchema, userContent, maxOutputTokens);
  if (typeof result.markdown_body === "string") {
    result.markdown_body = cleanMarkdownBody(result.markdown_body);
    if (qualityMode && docKey) {
      result = await refineMarkdownDocument(client, {
        systemPrompt,
        responseSchema,
        docKey,
        draft: result,
        groundingJson,
        transcriptText,
        facts,
        maxOutputTokens,
        withDiagrams,
        logger,
      });
    }
  }
  return result;
}

/** Port of engine.py::generate_mom. */
export async function generateMom(client, { meetingTitle, meetingDate, attendees, facts, transcriptText, qualityMode = true, logger }) {
  if (!transcriptText || !transcriptText.trim()) {
    return { mom: placeholderDoc("Minutes of Meeting", meetingTitle, meetingDate, NO_SPEECH_NOTE) };
  }
  const doc = await generateDocument(client, {
    systemPrompt: MOM_SYSTEM_PROMPT,
    responseSchema: MOM_RESPONSE_SCHEMA,
    meetingTitle,
    meetingDate,
    attendees,
    facts,
    transcriptText,
    docKey: "mom",
    qualityMode,
    logger,
  });
  return { mom: doc };
}

/** Port of engine.py::generate_meeting_analysis. */
export async function generateMeetingAnalysis(client, { meetingTitle, meetingDate, attendees, facts, transcriptText, qualityMode = true, logger }) {
  if (!transcriptText || !transcriptText.trim()) {
    return { meeting_analysis: placeholderDoc("Meeting Analysis", meetingTitle, meetingDate, NO_SPEECH_NOTE) };
  }
  const doc = await generateDocument(client, {
    systemPrompt: MEETING_ANALYSIS_SYSTEM_PROMPT,
    responseSchema: MEETING_ANALYSIS_RESPONSE_SCHEMA,
    meetingTitle,
    meetingDate,
    attendees,
    facts,
    transcriptText,
    docKey: "meeting_analysis",
    qualityMode,
    logger,
  });
  return { meeting_analysis: doc };
}

/**
 * Business Process Flow (As-Is + proposed) — a prose doc with fenced ```mermaid
 * flowchart blocks. Port of engine.py::generate_business_process_flow. Returns
 * `{ business_process_flow: { title, markdown_body } }`.
 */
export async function generateBusinessProcessFlow(client, { meetingTitle, meetingDate, attendees, facts, transcriptText, qualityMode = true, logger }) {
  if (!transcriptText || !transcriptText.trim()) {
    return {
      business_process_flow: placeholderDoc(
        "Business Process Flow",
        meetingTitle,
        meetingDate,
        "No speech was captured during this meeting, so there is no process to describe.",
      ),
    };
  }
  const doc = await generateDocument(client, {
    systemPrompt: BUSINESS_PROCESS_FLOW_SYSTEM_PROMPT,
    responseSchema: BUSINESS_PROCESS_FLOW_RESPONSE_SCHEMA,
    meetingTitle,
    meetingDate,
    attendees,
    facts,
    transcriptText,
    maxOutputTokens: 16384,
    docKey: "business_process_flow",
    qualityMode,
    withDiagrams: true,
    logger,
  });
  return { business_process_flow: doc };
}

/**
 * Both documents in ONE Gemini call (the free-tier-frugal default). Returns
 * `{ mom: {title, markdown_body}, meeting_analysis: {title, markdown_body} }`.
 * `qualityMode` adds one combined refine pass.
 */
export async function generateBothDocuments(client, {
  meetingTitle,
  meetingDate,
  attendees,
  facts,
  transcriptText,
  qualityMode = false,
  logger = console.warn,
}) {
  if (!transcriptText || !transcriptText.trim()) {
    return {
      mom: placeholderDoc("Minutes of Meeting", meetingTitle, meetingDate, NO_SPEECH_NOTE),
      meeting_analysis: placeholderDoc("Meeting Analysis", meetingTitle, meetingDate, NO_SPEECH_NOTE),
    };
  }

  const grounding = {
    meeting_title: meetingTitle,
    meeting_date: meetingDate,
    attendees: attendeesForPrompt(attendees),
    extracted_facts: facts,
  };
  const groundingJson = JSON.stringify(grounding, null, 2);
  const userContent =
    "Grounding data (JSON) + full transcript. Use ONLY facts present here.\n\n" +
    `EXTRACTED FACTS:\n${groundingJson}\n\nFULL TRANSCRIPT:\n${transcriptText}`;

  let out = await client.generateJson(COMBINED_DOCS_SYSTEM_PROMPT, COMBINED_DOCS_RESPONSE_SCHEMA, userContent, 16384);
  for (const key of ["mom", "meeting_analysis"]) {
    if (out[key] && typeof out[key].markdown_body === "string") {
      out[key].markdown_body = cleanMarkdownBody(out[key].markdown_body);
    }
  }

  if (qualityMode) {
    try {
      const momFindings = reviewMarkdownDocument("mom", out.mom?.markdown_body || "", facts);
      const maFindings = reviewMarkdownDocument("meeting_analysis", out.meeting_analysis?.markdown_body || "", facts);
      const refined = await client.generateJson(
        COMBINED_DOCS_SYSTEM_PROMPT,
        COMBINED_DOCS_RESPONSE_SCHEMA,
        `${REFINE_INSTRUCTION}` +
          `AUTOMATED CHECK FINDINGS (mom):\n${momFindings.map((f) => `- ${f}`).join("\n") || "- none"}\n` +
          `AUTOMATED CHECK FINDINGS (meeting_analysis):\n${maFindings.map((f) => `- ${f}`).join("\n") || "- none"}\n\n` +
          `DRAFT mom:\n${out.mom?.markdown_body || ""}\n\nDRAFT meeting_analysis:\n${out.meeting_analysis?.markdown_body || ""}\n\n` +
          `EXTRACTED FACTS:\n${groundingJson}\n\nFULL TRANSCRIPT:\n${transcriptText}`,
        16384,
      );
      for (const key of ["mom", "meeting_analysis"]) {
        const rb = refined[key]?.markdown_body;
        if (typeof rb === "string" && rb.trim().length >= 0.6 * (out[key]?.markdown_body || "").trim().length) {
          out[key] = { title: refined[key].title || out[key].title, markdown_body: cleanMarkdownBody(rb) };
        }
      }
    } catch (err) {
      logger("combined refine pass failed (kept the draft)", err);
    }
  }

  return out;
}

export { NO_SPEECH_NOTE };
