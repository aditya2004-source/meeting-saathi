/**
 * Prompt strings + Gemini structured-output schemas, ported verbatim from
 * app/docgen/extract_prompt.py and app/docgen/generate_prompt.py, plus a new
 * TRANSCRIBE_PROMPT for the T1 (Gemini audio) transcription step.
 *
 * V1 ships MOM + Meeting Analysis. SOW / Business Process Flow prompts are not
 * ported here yet (coded-but-disabled per the plan).
 *
 * Schema note (from the Python source): Gemini's schema dialect uses UPPERCASE
 * type strings and `nullable: true` for optional fields — it is NOT standard
 * JSON Schema (no `type: ["string","null"]` unions). Passed as
 * generationConfig.responseSchema.
 */

// ---------------------------------------------------------------------------
// Shared rules
// ---------------------------------------------------------------------------

export const LANGUAGE_RULE =
  "Always write the document in clear, simple English. If any transcript " +
  "content is in Hindi or another language, translate it to plain English -- " +
  "never copy non-English text verbatim into the output, except for direct " +
  "proper nouns/names.";

const NO_INVENTION =
  "Never invent anything not clearly present in the transcript. If something is " +
  "ambiguous, still record it but leave the ambiguous field null (or set a " +
  '`status` of "needs_clarification"); never fill a gap with a plausible guess. ' +
  "Attribute each item to the speaker who said it and the timestamp of that " +
  `segment where the field exists. ${LANGUAGE_RULE}`;

const SPEAKER_LABEL_RULE =
  "Use each speaker's exact label as it appears in the transcript. Some labels " +
  "look like `Unidentified speaker 1` -- copy such a label verbatim (with its " +
  "number); never shorten, merge, or replace it with a guessed name.";

const BA_VOICE =
  "You are a senior business analyst writing a client-facing deliverable. Write in " +
  "precise, professional business English -- full sentences, no filler, no " +
  "meeting-chatter phrasing, no first person. The reader was not in the meeting and " +
  "will act on this document as-is.";

function groundingRule(allowMermaid = false) {
  let fenceClause;
  let finishClause;
  if (allowMermaid) {
    fenceClause =
      "A diagram may appear ONLY as a fenced ```mermaid block; emit no other " +
      "kind of code fence, no JSON, and no `<|...|>` marker.";
    finishClause =
      "- Before finishing: check that every heading is present, every Mermaid " +
      "block is simple and valid, and nothing in the document is unsupported by " +
      "the inputs.";
  } else {
    fenceClause = "Never emit a code fence, JSON, or any `<|...|>` marker.";
    finishClause =
      "- Before finishing: check that every heading is present, the points actually " +
      "discussed are all covered, and nothing in the document is unsupported by the " +
      "inputs.";
  }
  return `GROUNDING -- non-negotiable:
- Use ONLY the extracted facts and transcript given below. Never invent a name, date,
  number, requirement, decision, risk, system, or metric that is not present there.
- A \`meeting_date\` field is provided -- use it verbatim wherever a date/time is
  needed; never infer one from transcript content.
- The \`attendees\` field is the authoritative attendee list -- use it verbatim, never
  recompute it from who spoke, never drop a name. If it is a single sentence stating
  only a count of unnamed speakers, reproduce that sentence.
- Some transcript speaker labels are \`Unidentified speaker N\` -- copy such a label
  verbatim (with its number) every time; never shorten, merge, or replace it with a
  guessed name.
- This is a client-facing document. NEVER write an internal requirement id such as
  "REQ-1", "REQ-12", or "[REQ-3]" -- describe the requirement in plain words instead.
  Internal ids, JSON field names, and bracketed tags must not appear in the output.
- If a required section has no supporting material, write exactly
  "Not discussed in this meeting." under it -- do not pad, and do not omit the
  heading.
- Emit ONLY the headings defined in the structure below, in that order and with that
  numbering. Never turn one of these instructions into a heading. ${fenceClause}
${finishClause}
${LANGUAGE_RULE}`;
}

const GROUNDING_RULE = groundingRule(false);
const GROUNDING_RULE_MERMAID = groundingRule(true);

const PRODUCT_GROUPING_RULE =
  "If the meeting covered more than one distinct product or solution, separate the " +
  "relevant discussion by product using the product name exactly as spoken (never " +
  "invent one) -- as a deeper sub-heading or a labelled bullet group WITHIN the " +
  "relevant section, never as a new top-level section. If only one was discussed, " +
  "no product split is needed.";

// ---------------------------------------------------------------------------
// Extraction prompts
// ---------------------------------------------------------------------------

export const CORE_EXTRACT_SYSTEM_PROMPT = `You are the extraction layer for Meeting Saathi. You
are given a full, speaker-labeled, timestamped transcript of a business meeting.

YOUR JOB
Extract a structured, faithful record of what was actually said. This is the ONLY
input to the Minutes, Meeting Analysis, BRD, FRD, User Stories, Acceptance Criteria,
and Business Process Flow -- anything you leave out is lost for good.

COMPLETENESS -- be thorough, not brief:
- Capture EVERY distinct topic, sub-topic, and tangent actually discussed. A
  substantive 60-90 minute meeting usually yields 12-25 \`topics_discussed\` entries.
- Record every decision, action item, and commitment -- including small or in-passing
  ones. Never merge two distinct items into one line.
- Fill \`risks\`, \`assumptions\`, \`dependencies\`, and \`open_questions\` with the SAME
  diligence as the rest -- these are routinely under-captured. Anything voiced as a
  concern, a "we're assuming...", a "this needs X first", or an unanswered question
  belongs in one of them.
- Prefer recording a borderline item (ambiguous field null / status
  "needs_clarification") over dropping it. This never overrides the no-invention rule.

FIELD NOTES
- Disambiguate: \`action_items\` are internal follow-up work with no firm promise;
  \`commitments\` are an explicit promise made to someone; \`decisions\` are something the
  group agreed on (not a task or promise).
- On each \`requirements\` entry fill \`rationale\` (the business reason the client gave)
  and \`acceptance_hint\` (any "done when..." / "must be able to..." signal) when the
  transcript provides them; null otherwise.
- \`business_processes\` is a LIST: one entry per distinct AS-IS process a participant
  walked through (inbound leads, field-visit attendance, tour planning, expense
  approval, back-office follow-up calls are separate processes). Record every step,
  actor, hand-off, and branch outcome actually described -- the full sequence, not a
  4-5 step summary. Mark a step "needs_clarification" rather than guessing a branch.

${SPEAKER_LABEL_RULE}
${NO_INVENTION}
`;

export const VERIFY_EXTRACT_SYSTEM_PROMPT = `You are the extraction QA reviewer for Meeting
Saathi. You are given the full transcript of a business meeting AND a draft
structured extraction of it. Your job is to return a MORE COMPLETE and MORE ACCURATE
version of that extraction.

DO:
- Add every topic, decision, action item, commitment, requirement, risk, assumption,
  dependency, and open question that was actually discussed but is missing or
  under-captured in the draft. Re-read the transcript specifically hunting for these
  -- the draft is routinely thin on risks/assumptions/dependencies/open_questions and
  on small/in-passing action items.
- Keep every correct item from the draft. Keep requirement \`id\` values stable; if you
  add a requirement, give it the next free REQ-n.
- Split a draft requirement that actually bundles two distinct needs into two.
- List in \`unsupported_items\` the exact text of any draft item you believe the
  transcript does NOT support, so it can be removed. Be conservative here -- only
  flag something clearly unsupported, not merely terse.

Return the full corrected extraction in the same shape as the draft, plus
\`unsupported_items\`. ${NO_INVENTION}
`;

export const CONTEXT_EXTRACT_SYSTEM_PROMPT = `You are the business-analysis context extractor for
Meeting Saathi. You are given a full, speaker-labeled transcript of a business meeting
whose concrete facts (topics, decisions, requirements, risks, ...) are already being
captured separately.

YOUR JOB
Extract only the higher-level business-analysis context a BA needs to frame a BRD/FRD:
- \`meeting_purpose\`: one sentence on why this meeting was held; null if not clear.
- \`goals\`: the business outcomes the client wants (the "why", not the system "what"),
  with the client's stated rationale where given.
- \`current_state\`: how things work today and where they hurt -- one entry per distinct
  area of the as-is situation, with the pain point if voiced. Capture this even when
  no step-by-step process was walked through.
- \`constraints\`: hard limits stated -- budget, timeline, technology, policy, staffing,
  existing-tool lock-in. Distinct from assumptions (things taken as true) and risks.
- \`systems_and_integrations\`: every external system/tool/software named (ERPs,
  accounting, telephony, maps, messaging, existing apps), what it's used for, and
  whether an integration with it was asked for.
- \`glossary\`: domain terms, acronyms, product names, and jargon a reader outside the
  room wouldn't know, each with a plain-English definition grounded in how it was
  used. Skip a term if you're unsure of its meaning.
- \`non_functional_notes\`: short phrases on performance, security, reliability,
  availability, scale, usability, offline behaviour, or data retention -- only if
  actually mentioned.

Be thorough but strict: ${NO_INVENTION}
`;

// One combined extraction — the concrete record AND the BA-analysis context in a
// single call. Halves the extraction cost (matters on the Gemini free tier). The
// separate core/context prompts above are kept for the verify pass + reference.
export const COMBINED_EXTRACT_SYSTEM_PROMPT = `You are the extraction layer for Meeting Saathi. You
are given a full, speaker-labeled, timestamped transcript of a business meeting.

YOUR JOB
Extract ONE structured, faithful record of what was said. It is the sole input to
the meeting's minutes and its analysis. Two parts, in one output:

PART A -- the concrete record. Be thorough, not brief:
- Capture EVERY distinct topic, sub-topic, and tangent actually discussed (a
  substantive 60-90 min meeting yields 12-25 \`topics_discussed\`).
- Record every decision, action item, and commitment -- including small/in-passing
  ones. Never merge two distinct items into one line.
- Fill \`risks\`, \`assumptions\`, \`dependencies\`, \`open_questions\` with the same
  diligence -- anything voiced as a concern, a "we're assuming...", a "this needs X
  first", or an unanswered question.
- \`action_items\` = internal follow-up with no firm promise; \`commitments\` = an
  explicit promise to someone; \`decisions\` = something the group agreed.
- On each \`requirements\` entry fill \`rationale\` and \`acceptance_hint\` when the
  transcript gives them; null otherwise. Stable ids in order of first mention (REQ-1).
- \`business_processes\` is a LIST: one entry per distinct AS-IS process a participant
  walked through, every step/actor/hand-off/branch. Empty list if none.

PART B -- the higher-level context:
- \`meeting_purpose\`: one sentence on why the meeting was held; null if unclear.
- \`goals\`: business outcomes the client wants (the "why"), with rationale where given.
- \`current_state\`: how things work today and where they hurt, per area, with the pain
  point if voiced.
- \`constraints\`: hard limits stated (budget, timeline, technology, policy, staffing).
- \`systems_and_integrations\`: external systems/tools named, purpose, and whether an
  integration was asked for.
- \`glossary\`: domain terms/acronyms/jargon with plain-English definitions grounded in
  use. Skip a term you're unsure of.
- \`non_functional_notes\`: short phrases on performance/security/reliability/scale/
  usability, only if mentioned.

${SPEAKER_LABEL_RULE}
${NO_INVENTION}
`;

// ---------------------------------------------------------------------------
// Generation prompts (V1: MOM + Meeting Analysis)
// ---------------------------------------------------------------------------

export const MOM_SYSTEM_PROMPT = `${BA_VOICE}

Write clear, concise Minutes of Meeting a reader can scan in two minutes. Keep it
plain and uncluttered -- short sentences, no jargon, no internal codes. Use EXACTLY
these headings, in this order:

## Meeting Overview
A two-column Markdown table. It MUST begin with the header row \`| Field | Detail |\`
and the separator row \`| --- | --- |\`, then exactly these four data rows: Title;
Date & Time (verbatim from meeting_date); Purpose (from \`meeting_purpose\`, else one
plain line); Attendees (the authoritative attendees list, comma-separated -- add a
person's role or company only where the facts make it clear).

## Discussion Highlights
One \`### \` sub-section per topic in \`extracted_facts.topics_discussed\` (same topics,
same order). Under each heading write 2-4 plain sentences: what was discussed and the
key points, figures, or examples. Be concise -- capture the substance, not every
sentence. Where the facts are thin for a topic that clearly got real airtime, draw
the substance from the transcript (still no invention).

## Decisions
A plain bullet list -- one bullet per \`extracted_facts.decisions\` entry, phrased as
"<the decision> (decided by <name>)". "Not discussed in this meeting." if none.

## Action Items
A plain bullet list -- one bullet per \`extracted_facts.action_items\` AND per
\`extracted_facts.commitments\` entry, phrased as "<the action> -- <owner>, by <due
date>" (use "owner not assigned" / "no date set" when the facts don't say).
"Not discussed in this meeting." if none.

## Open Questions
A plain bullet list from \`extracted_facts.open_questions\` -- questions raised but not
resolved. "None." if there are none.

## Next Steps
A short plain bullet list of what happens after this meeting, drawn from the
decisions, action items, and commitments. "Not discussed in this meeting." if the
meeting gave no forward direction.

${PRODUCT_GROUPING_RULE}

${GROUNDING_RULE}`;

export const MEETING_ANALYSIS_SYSTEM_PROMPT = `${BA_VOICE}

Produce a one-page Meeting Analysis a busy stakeholder can absorb in under a minute
without opening any other document. Use EXACTLY these headings:

## Executive Snapshot
3-4 sentences: what this meeting was, why it happened, and where it landed.

## Key Discussion Points
Bullet points grouped by theme (not a replay of every sentence -- the substance).

## Decisions & Direction
What was decided or agreed, and the direction things are heading. "Nothing formally
decided in this meeting." if that is the case.

## What Needs To Happen Next
A consolidated, action-oriented bullet list merging decisions, action items, and
commitments into plain next steps, naming the owner where known.

## Risks & Open Questions
Bullet list of anything raised but unresolved, from \`risks\` and \`open_questions\`.
"None raised in this meeting." if empty.

## Analyst's Note
2-4 sentences: readiness to proceed, the biggest gaps or ambiguities, and what should
be clarified before the next step. Ground this in what was (and wasn't) said.

${PRODUCT_GROUPING_RULE}

${GROUNDING_RULE}`;

// Both documents in ONE call — a third of the per-meeting cost vs a MOM call +
// an Analysis call + refines. Same section specs as the two prompts above.
export const COMBINED_DOCS_SYSTEM_PROMPT = `${BA_VOICE}

Produce BOTH client-facing documents for this meeting in one JSON object with keys
\`mom\` and \`meeting_analysis\`, each { "title": ..., "markdown_body": ... }.

=== mom (Minutes of Meeting) — clear and concise, scannable in two minutes, no
jargon, no internal codes. markdown_body uses EXACTLY these headings, in order:
## Meeting Overview
A two-column Markdown table starting with \`| Field | Detail |\` then \`| --- | --- |\`,
then four rows: Title; Date & Time (verbatim from meeting_date); Purpose (from
\`meeting_purpose\`, else one plain line); Attendees (comma-separated authoritative
list; role/company only where the facts make it clear).
## Discussion Highlights
One \`### \` sub-section per \`extracted_facts.topics_discussed\` item (same order):
2-4 plain sentences on what was discussed and the key points/figures. Concise -- the
substance, not every sentence. Thin facts for a topic that got real airtime → draw
from the transcript (no invention).
## Decisions
Plain bullet list — one per \`extracted_facts.decisions\`, as
"<decision> (decided by <name>)". "Not discussed in this meeting." if none.
## Action Items
Plain bullet list — one per \`extracted_facts.action_items\` and per
\`extracted_facts.commitments\`, as "<action> -- <owner>, by <due date>" ("owner not
assigned" / "no date set" when unknown). "Not discussed in this meeting." if none.
## Open Questions
Plain bullet list from \`extracted_facts.open_questions\`. "None." if none.
## Next Steps
Short plain bullets of what happens next, from decisions/action items/commitments.
"Not discussed in this meeting." if no forward direction.

=== meeting_analysis (Meeting Analysis) — markdown_body uses EXACTLY these headings:
## Executive Snapshot
3-4 sentences: what this meeting was, why it happened, where it landed.
## Key Discussion Points
Bullet points grouped by theme — the substance, not a replay.
## Decisions & Direction
What was decided/agreed and where things are heading. "Nothing formally decided in
this meeting." if so.
## What Needs To Happen Next
Consolidated action-oriented bullets merging decisions/action items/commitments,
naming the owner where known.
## Risks & Open Questions
Bullet list from \`risks\` and \`open_questions\`. "None raised in this meeting." if empty.
## Analyst's Note
2-4 sentences: readiness to proceed, biggest gaps/ambiguities, what to clarify next.

${PRODUCT_GROUPING_RULE}

${GROUNDING_RULE}`;

export const COMBINED_DOCS_RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    mom: { type: "OBJECT", properties: { title: { type: "STRING" }, markdown_body: { type: "STRING" } }, required: ["title", "markdown_body"] },
    meeting_analysis: { type: "OBJECT", properties: { title: { type: "STRING" }, markdown_body: { type: "STRING" } }, required: ["title", "markdown_body"] },
  },
  required: ["mom", "meeting_analysis"],
};

// ---------------------------------------------------------------------------
// Business Process Flow (As-Is + proposed) — port of generate_prompt.py
// ---------------------------------------------------------------------------

export const BUSINESS_PROCESS_FLOW_SYSTEM_PROMPT = `${BA_VOICE}

Explain, in plain language a non-technical reader immediately understands, how this
part of the business works today -- and, ONLY if the meeting actually discussed or
demonstrated a proposed new way of working, how it would work after. Keep every
diagram dead simple. Use EXACTLY these headings, in this order:

## Business Summary
2-4 plain sentences: what this area of the business does and who is involved. From
\`meeting_purpose\`, \`topics_discussed\`, and \`current_state\`.

## How It Works Today
A short numbered walk-through of the current process (4-8 steps, plain phrasing),
then ONE fenced \`\`\`mermaid flowchart of the same steps. Draw from
\`extracted_facts.business_processes\` (primary source), \`extracted_facts.current_state\`,
and the transcript.

## Key Pain Points
3-6 bullets -- only problems actually voiced in the meeting. From \`current_state\`
pain points, \`extracted_facts.risks\`, and the transcript.
"Not discussed in this meeting." if none.

## Proposed Way of Working
ONLY if the meeting actually discussed or demonstrated a proposed solution or process
change (for example a vendor demo). Then: a short plain-language description of what
changes, then ONE fenced \`\`\`mermaid flowchart of the proposed process -- strictly
from \`topics_discussed\`, \`extracted_facts.requirements\`,
\`extracted_facts.systems_and_integrations\`, and the transcript. Never invent a future
step. If no new way of working was discussed, write exactly
"Not discussed in this meeting." under this heading and nothing else.

## What Changes
ONLY if "Proposed Way of Working" has real content: a short list or a 2-column table
(Today -> Proposed) of the meaningful differences. Otherwise write exactly
"Not discussed in this meeting."

Every Mermaid diagram MUST be dead simple:
- Start with \`flowchart TD\`. At most about 8 nodes.
- Prefer one straight top-to-bottom line of steps.
- Use a decision node like X{{"Approved?"}} ONLY where the meeting described a real
  either/or; label its edges in plain words (for example: -->|approved|).
- Node labels are short plain-language phrases (3-6 words), each wrapped in double
  quotes, for example A["Receive customer enquiry"].
- No role annotations, no input/output sub-lines, no <br/>, no subgraph, no
  classDef or styling, no side notes. Put nothing but valid Mermaid inside the fence.

${PRODUCT_GROUPING_RULE}

${GROUNDING_RULE_MERMAID}`;

// ---------------------------------------------------------------------------
// Transcription (T1)
// ---------------------------------------------------------------------------

/**
 * Builds the transcribe prompt. `speakerEvents` / `roster` are the content
 * script's DOM scrape (may be empty). `languageMode` mirrors the old
 * whisper_task=translate default.
 */
export function buildTranscribePrompt({ speakerEvents = [], roster = [], languageMode = "translate" } = {}) {
  const languageClause =
    languageMode === "original"
      ? "Keep each utterance in the language it was spoken in."
      : languageMode === "english"
        ? "Translate every non-English utterance to natural English."
        : "The meeting may mix Hindi and English. Translate everything to natural English.";

  const eventsClause = speakerEvents.length
    ? `Speaker-change hints detected from the meeting UI (a name and the time in seconds it became active). Use these names where a segment's time falls at or after a hint and before the next one; otherwise label "Speaker 1", "Speaker 2", ... consistently per voice:\n${JSON.stringify(speakerEvents)}`
    : 'The meeting UI gave no speaker hints. Label speakers "Speaker 1", "Speaker 2", ... consistently for the same voice.';

  const rosterClause = roster.length ? `Known participants (People panel): ${JSON.stringify(roster)}.` : "";

  return `Produce a faithful, timestamped, speaker-labelled transcript of this meeting recording.

Rules:
- Transcribe what was actually said. Do NOT summarise, paraphrase, or skip sections. A 60-90 minute meeting produces a long transcript.
- ${languageClause}
- One segment per continuous utterance by one speaker.
- ${eventsClause}
- ${rosterClause}

Output JSON: {"segments": [{"start_seconds": number, "end_seconds": number, "speaker": string, "text": string}], "attendees": [string], "detected_language": string, "notes": string}.`;
}

export const TRANSCRIBE_RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    segments: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          start_seconds: { type: "NUMBER" },
          end_seconds: { type: "NUMBER" },
          speaker: { type: "STRING" },
          text: { type: "STRING" },
        },
        required: ["start_seconds", "end_seconds", "speaker", "text"],
      },
    },
    attendees: { type: "ARRAY", items: { type: "STRING" } },
    detected_language: { type: "STRING" },
    notes: { type: "STRING" },
  },
  required: ["segments"],
};

// ---------------------------------------------------------------------------
// Speaker reconciliation (recovery pass — port of reconcile_prompt.py)
// ---------------------------------------------------------------------------

// When Gemini's transcription returns only generic "Speaker N" and the DOM
// active-speaker scrape captured nothing, buildTranscript() ends up with a set
// of "Unidentified speaker N" labels. This one call reads the whole transcript
// back and maps every such label to a real participant, assigning a real name
// only where the transcript unambiguously reveals it (self-intros, forms of
// address), else a stable "Participant N".
export const RECONCILE_SYSTEM_PROMPT = `You are the speaker-reconciliation layer for Meeting
Saathi. You are given the full transcript of ONE business meeting in which automatic
speaker diarization failed: a single real person is scattered across many different
"Unidentified speaker N" labels.

YOUR JOB
Work out how many real people actually spoke, and map EVERY "Unidentified speaker N"
label in the transcript to one of them.

HOW TO DECIDE WHO IS WHO
- Conversational continuity: consecutive lines that answer each other, finish a
  sentence, or hold one point of view are usually the same person.
- Forms of address: when someone is addressed by name ("Mustafa bhai", "Dhaval ji",
  "Aditya"), the person being replied to is very likely that named person.
- Self-introduction ("this is X from Y", "I'm X").
- Role/side: a client asking questions vs. a vendor demoing; a presenter driving the
  agenda vs. participants reacting.
- Real meetings usually have 2-8 distinct speakers, not dozens. Prefer the smallest
  participant set the transcript actually supports.

NAMING RULES -- STRICT
- Assign a real name ONLY when the transcript unambiguously reveals it (addressed by
  that name, or self-introduced). Never guess or invent a name.
- With a real first name, use it as the canonical label; add a side/company in
  parentheses only if the transcript makes it clear ("Mustafa (Imdadi BuildMart)").
- With no real name, use a stable role label "Participant 1", "Participant 2", ... in
  order of first appearance, optionally with a side you are confident about.

OUTPUT RULES
- Return a \`participants\` list -- one entry per real person, usually 2-8.
- \`speaker_numbers\` is the list of integer N values ("Unidentified speaker N") that
  are that person. Every N in the transcript appears in exactly ONE participant.
- If you genuinely cannot tell who a label is, give it its own "Participant N" entry.
- No prose, no per-speaker explanations.
- ${LANGUAGE_RULE}
`;

export const RECONCILE_RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    participants: {
      type: "ARRAY",
      description: "One entry per real person who spoke (usually 2-8).",
      items: {
        type: "OBJECT",
        properties: {
          canonical_label: {
            type: "STRING",
            description: 'A real first name (optionally "Name (Side)"), or "Participant N".',
          },
          is_real_name: {
            type: "BOOLEAN",
            description: "True only if canonical_label is a name the transcript unambiguously revealed.",
          },
          speaker_numbers: {
            type: "ARRAY",
            items: { type: "INTEGER" },
            description: 'The N values ("Unidentified speaker N") that are this person.',
          },
        },
        required: ["canonical_label", "is_real_name", "speaker_numbers"],
      },
    },
  },
  required: ["participants"],
};

// ---------------------------------------------------------------------------
// Schemas — extraction (ported from extract_prompt.py)
// ---------------------------------------------------------------------------

export const CORE_EXTRACT_RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    attendees: {
      type: "ARRAY",
      items: { type: "STRING" },
      description:
        "Distinct speaker names/labels who spoke. Internal cross-check only -- the authoritative attendee list comes from a roster provided outside this extraction.",
    },
    topics_discussed: {
      type: "ARRAY",
      items: { type: "STRING" },
      description: "Short phrases naming the topics/agenda items actually discussed.",
    },
    decisions: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          decision: { type: "STRING" },
          made_by: { type: "STRING", nullable: true },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["decision"],
      },
    },
    action_items: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          description: { type: "STRING" },
          owner: { type: "STRING", nullable: true, description: "Name of the person responsible, or null if unclear." },
          due_date_mentioned: { type: "STRING", nullable: true },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["description", "owner"],
      },
    },
    key_quotes: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          speaker: { type: "STRING" },
          quote: { type: "STRING" },
          timestamp: { type: "NUMBER" },
        },
        required: ["speaker", "quote", "timestamp"],
      },
    },
    requirements: {
      type: "ARRAY",
      description:
        "The first-class list every downstream document (BRD, FRD, User Stories, Acceptance Criteria) derives from.",
      items: {
        type: "OBJECT",
        properties: {
          id: { type: "STRING", description: "Stable id in order of first mention, e.g. REQ-1." },
          statement: { type: "STRING" },
          category: {
            type: "STRING",
            description: "e.g. functional, non_functional, integration, reporting, data, security, other.",
          },
          priority: { type: "STRING", nullable: true, description: "e.g. high, medium, low." },
          stakeholder: {
            type: "STRING",
            nullable: true,
            description: "Person/role who raised or owns this requirement.",
          },
          status: {
            type: "STRING",
            description: '"clear" if actionable without follow-up, else "needs_clarification".',
          },
          rationale: {
            type: "STRING",
            nullable: true,
            description: "The business reason the client gave for wanting this; null if not stated.",
          },
          acceptance_hint: {
            type: "STRING",
            nullable: true,
            description: 'Any "done when..." / "must be able to..." signal; null if none.',
          },
          source_speaker: { type: "STRING", nullable: true },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["id", "statement", "category", "status"],
      },
    },
    risks: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          description: { type: "STRING" },
          impact: { type: "STRING", nullable: true },
          raised_by: { type: "STRING", nullable: true },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["description"],
      },
    },
    assumptions: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          statement: { type: "STRING" },
          made_by: { type: "STRING", nullable: true },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["statement"],
      },
    },
    dependencies: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          description: { type: "STRING" },
          depends_on: { type: "STRING", nullable: true },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["description"],
      },
    },
    open_questions: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          question: { type: "STRING" },
          raised_by: { type: "STRING", nullable: true },
          related_requirement_id: {
            type: "STRING",
            nullable: true,
            description: "Links back to requirements[].id, if this question blocks a specific requirement.",
          },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["question"],
      },
    },
    commitments: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          commitment: { type: "STRING" },
          committed_by: { type: "STRING", nullable: true },
          committed_to: { type: "STRING", nullable: true },
          due_date_mentioned: { type: "STRING", nullable: true },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["commitment"],
      },
    },
    business_processes: {
      type: "ARRAY",
      description:
        "One entry per distinct AS-IS/current business process a participant actually walked through. Empty list if no process walkthrough happened.",
      items: {
        type: "OBJECT",
        properties: {
          process_name: { type: "STRING" },
          steps: {
            type: "ARRAY",
            items: {
              type: "OBJECT",
              properties: {
                id: { type: "STRING", description: "Stable id, unique within this process, e.g. STEP-1." },
                type: { type: "STRING", description: "One of: start, end, process, decision, approval, system." },
                description: { type: "STRING" },
                actor: { type: "STRING", nullable: true },
                inputs: { type: "ARRAY", items: { type: "STRING" } },
                outputs: { type: "ARRAY", items: { type: "STRING" } },
                system_interaction: { type: "STRING", nullable: true },
                next_step_id: { type: "STRING", nullable: true, description: "For non-decision steps." },
                on_yes_step_id: { type: "STRING", nullable: true, description: "Decision steps only." },
                on_no_step_id: { type: "STRING", nullable: true, description: "Decision steps only." },
                alternate_flow: { type: "STRING", nullable: true },
                exception_notes: { type: "STRING", nullable: true },
                status: { type: "STRING", description: '"clear" or "needs_clarification".' },
              },
              required: ["id", "type", "description", "status"],
            },
          },
        },
        required: ["process_name", "steps"],
      },
    },
  },
  required: [
    "attendees",
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
  ],
};

export const VERIFY_EXTRACT_RESPONSE_SCHEMA = structuredClone(CORE_EXTRACT_RESPONSE_SCHEMA);
VERIFY_EXTRACT_RESPONSE_SCHEMA.properties.unsupported_items = {
  type: "ARRAY",
  items: { type: "STRING" },
  description: "Exact text of any draft item the transcript does not support; empty if none.",
};
VERIFY_EXTRACT_RESPONSE_SCHEMA.required = [...CORE_EXTRACT_RESPONSE_SCHEMA.required, "unsupported_items"];

export const CONTEXT_EXTRACT_RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    meeting_purpose: {
      type: "STRING",
      nullable: true,
      description: "One sentence on why this meeting was held; null if not stated or obvious.",
    },
    goals: {
      type: "ARRAY",
      description: "Business goals/outcomes the client wants (the 'why'), distinct from requirements.",
      items: {
        type: "OBJECT",
        properties: {
          statement: { type: "STRING" },
          rationale: { type: "STRING", nullable: true },
        },
        required: ["statement"],
      },
    },
    current_state: {
      type: "ARRAY",
      description: "How things work today and where they hurt -- raw material for a BRD Current-State section.",
      items: {
        type: "OBJECT",
        properties: {
          area: { type: "STRING", description: "Which part of the business this describes." },
          description: { type: "STRING", description: "How it works today." },
          pain_point: { type: "STRING", nullable: true, description: "The problem with it, if voiced." },
        },
        required: ["area", "description"],
      },
    },
    constraints: {
      type: "ARRAY",
      description: "Hard limits stated in the meeting (budget, timeline, technology, policy, staffing).",
      items: {
        type: "OBJECT",
        properties: {
          description: { type: "STRING" },
          type: { type: "STRING", nullable: true, description: "e.g. budget, timeline, technical, policy, resource." },
          timestamp: { type: "NUMBER", nullable: true },
        },
        required: ["description"],
      },
    },
    systems_and_integrations: {
      type: "ARRAY",
      description: "External systems/tools named, what they're used for, and whether an integration was requested.",
      items: {
        type: "OBJECT",
        properties: {
          name: { type: "STRING" },
          purpose: { type: "STRING", nullable: true },
          integration_need: { type: "STRING", nullable: true },
        },
        required: ["name"],
      },
    },
    glossary: {
      type: "ARRAY",
      description: "Domain terms/acronyms/product names used in the meeting, with plain-English definitions.",
      items: {
        type: "OBJECT",
        properties: {
          term: { type: "STRING" },
          definition: { type: "STRING" },
        },
        required: ["term", "definition"],
      },
    },
    non_functional_notes: {
      type: "ARRAY",
      items: { type: "STRING" },
      description: "Short phrases on performance/security/reliability/scale/usability, only if mentioned.",
    },
  },
  required: [
    "meeting_purpose",
    "goals",
    "current_state",
    "constraints",
    "systems_and_integrations",
    "glossary",
    "non_functional_notes",
  ],
};

// Concrete record + BA context in one schema (for COMBINED_EXTRACT_SYSTEM_PROMPT).
export const COMBINED_EXTRACT_RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    ...CORE_EXTRACT_RESPONSE_SCHEMA.properties,
    ...CONTEXT_EXTRACT_RESPONSE_SCHEMA.properties,
  },
  required: [...CORE_EXTRACT_RESPONSE_SCHEMA.required, ...CONTEXT_EXTRACT_RESPONSE_SCHEMA.required],
};

// ---------------------------------------------------------------------------
// Schemas — generation
// ---------------------------------------------------------------------------

export const DOCUMENT_RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    title: { type: "STRING" },
    markdown_body: { type: "STRING" },
  },
  required: ["title", "markdown_body"],
};

export const MOM_RESPONSE_SCHEMA = DOCUMENT_RESPONSE_SCHEMA;
export const MEETING_ANALYSIS_RESPONSE_SCHEMA = DOCUMENT_RESPONSE_SCHEMA;
export const BUSINESS_PROCESS_FLOW_RESPONSE_SCHEMA = DOCUMENT_RESPONSE_SCHEMA;
