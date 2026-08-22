# `app/docgen/` — transcript → MOM / Meeting Analysis

Turns a speaker-labeled transcript into the documents saved to the
meeting's folder, using Gemini (Google's API, free tier) as the only
external API in the system.

**Phase 1 status (sharing with BA testers):** only MOM and a new "Meeting
Analysis" doc (`generate_prompt.py: MEETING_ANALYSIS_SYSTEM_PROMPT`) are
actually generated right now — see `engine.py: generate_documents()`. The
Requirement Gathering Sheet and Action Points prompts/schemas below are
still fully implemented and described as before, just not called from
`generate_documents()` for the moment (to save Gemini quota while a small
group of testers tries this out); re-enabling them is a small addition to
that function's `ThreadPoolExecutor` block, not a rewrite. Everything else
in this doc describes the underlying mechanism, which is unchanged.

**Provider history:** this originally used Claude (Anthropic) via forced
tool-calling. Switched to Gemini specifically to avoid any paid API at
all, at the user's explicit request — Anthropic's API has no free tier for
real use, while Gemini's does. The underlying design (two-call
extract-then-generate, grounding rules, structured schemas) is unchanged;
only the SDK and schema *format* changed, documented below where it
matters. `app/DESIGN.md` covers the `config.py`/`.env` side of this
switch.

## The two-call extract-then-generate pattern

Modeled on a pattern already used in the `sarathi-ai-copilot-demo` project
(`backend/src/ai/`), originally with Claude — the pattern itself ported
cleanly to Gemini's structured-output mode.

1. **Call 1 — extraction** (`extract_prompt.py` + `engine.py:
   extract_meeting_facts`): Gemini reads the full transcript and pulls out
   structured facts only — attendees, decisions, action items with owners,
   topics, key quotes — via a **forced JSON schema**
   (`response_mime_type="application/json"` + `response_json_schema` in
   `GenerateContentConfig`), so the output is always valid structured
   data, never free-form text that could drift off-schema.
2. **Call 2 — generation, run 3 times** (`generate_prompt.py` +
   `engine.py: _generate_document`): Gemini writes the actual documents,
   but is only given Call 1's facts plus transcript excerpts, with an
   explicit instruction to never invent a name, date, or fact that isn't
   actually there — and to write `"Not discussed in this meeting"` rather
   than fabricate content for an uncovered section (`_GROUNDING_RULE` in
   `generate_prompt.py`, shared by all three documents).

This keeps every document grounded in what was actually said, rather than
letting Gemini fill gaps with plausible-sounding invented content.

The same grounding instinct extends to speaker labels: `pipeline/
speaker_names.py`'s terminal guarantee (`fill_unresolved_with_excerpts()`)
means some transcript speaker labels aren't a real name at all but a
quoted excerpt shaped like `Unidentified speaker ("...")`. Both
`EXTRACT_SYSTEM_PROMPT` and `generate_prompt.py`'s shared `_GROUNDING_RULE`
explicitly tell Gemini to copy that whole label verbatim rather than
shortening it or inventing a plausible-sounding real name to replace it —
without that instruction, an LLM asked to write "professional" minutes
would very likely try to clean up what looks like a malformed name field,
defeating the entire point of the excerpt fallback.

## Two different output shapes, both forced-JSON-schema

- **MOM and Action Points** stay genuinely prose-shaped documents, so they
  share one simple `DOCUMENT_RESPONSE_SCHEMA` (`{title, markdown_body}`) —
  Gemini returns that JSON directly and `markdown_body` is rendered as-is.
- **The Requirement Gathering Sheet** has a hard, non-negotiable contract
  instead: an exact 4-column table
  (`Requirements | Mapping of Requirements | Customisation for Mapping /
  Some Requirements in Sarathi | Queries regarding Requirements or
  Mapping`) with 8 fixed standard rows (Organisation Structure, Customer
  Base & New Acquisition, Order Management, Visit & Route Planning,
  Attendance, Expense Management, Payment Collection, Current System &
  Integration) that must always be present, plus extra rows for whatever
  else was actually discussed. A system-prompt instruction alone can only
  *hope* Gemini keeps that structure every time; `REQUIREMENT_GATHERING_
  RESPONSE_SCHEMA` instead forces a `rows: [{area, discussion_points,
  sarathi_mapping, open_queries}]` schema, so the 4-column/8-row contract
  is guaranteed by the schema, not by prompt-following luck.
  `render_tables.py` then turns those structured rows into the actual
  markdown table.

## Gemini's schema format is not standard JSON Schema

Worth calling out explicitly since it's an easy thing to get subtly wrong:
Gemini's `response_json_schema` uses **UPPERCASE type strings**
(`"OBJECT"`, `"ARRAY"`, `"STRING"`, `"NUMBER"`, not lowercase), and
**doesn't support JSON Schema's nullable-union syntax**
(`"type": ["string", "null"]`, which the old Anthropic-flavored schemas
used) — nullable fields instead need `"type": "STRING", "nullable":
true`. All three schema modules (`extract_prompt.py`,
`generate_prompt.py`) already follow this; if you add a new field, match
the existing style rather than copying generic JSON Schema examples from
elsewhere.

## `render_tables.py`

Pure function (`render_requirement_gathering_markdown`), no Gemini/
Playwright dependency, unit-tested in `tests/test_render_tables.py` (same
spirit as `pipeline/merge.py:render_plain_text`). Two correctness details
that are easy to miss and were deliberately handled:
- Markdown table cells can't contain a literal newline, so multi-bullet
  fields (`discussion_points`, `open_queries`) are joined with `<br>`
  instead — `render_pdf.py`'s `"tables"` markdown extension renders that
  inline HTML fine.
- A literal `|` character anywhere in a cell (a client requirement phrased
  with a pipe, or Gemini echoing one) would otherwise corrupt the table
  structure, so every cell is escaped (`|` → `\|`) before assembly.

`engine.py:generate_documents()` calls the Requirement Gathering Sheet
schema, then immediately renders its `rows` into `markdown_body` and
stores that alongside the raw structured data under the
`"requirement_gathering"` key — this means `orchestrator.py` and
`scripts/regenerate_docs.py` only ever need to read
`docs[key]["markdown_body"]`, identical to how they already read
MOM/Action Points, with no branching logic for the differently-shaped
schema underneath.

## `render_pdf.py`

Markdown → HTML (via `python-markdown`, `"tables"` extension) → PDF (via
Playwright + headless Chrome). Tries `channel="chrome"` (the system
install) first and falls back to Playwright's bundled Chromium if that
channel isn't registered — avoids requiring a separate Chromium download
on a machine that already has Chrome. Provider-agnostic, unaffected by the
Claude → Gemini switch.

## `scripts/regenerate_docs.py`

Re-runs `generate_documents()` + `markdown_to_pdf()` against an existing
`transcript.json` without re-transcribing or re-diarizing — useful for
iterating on prompts, and the documented recovery path when the pipeline
died after transcription but before doc generation. It must be kept in
sync with `generate_documents()`'s returned dict shape (currently `mom`
and `meeting_analysis`, each exposing `markdown_body`) — there's no
indirection between the two, by design, to keep this script trivial to
read.
