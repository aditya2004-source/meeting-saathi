# `app/docgen/` — transcript → structured facts → on-demand documents

Turns a speaker-labeled transcript into structured facts, then — only when a
user explicitly asks — into the actual documents saved to the meeting's
folder, using Gemini (Google's API, free tier) as the only external API in
the system.

**Provider history:** this originally used Claude (Anthropic) via forced
tool-calling. Switched to Gemini specifically to avoid any paid API at all,
at the user's explicit request — Anthropic's API has no free tier for real
use, while Gemini's does. `app/DESIGN.md` covers the `config.py`/`.env` side
of this switch.

**Product history:** originally Sarathi-specific (a Requirement Gathering
Sheet hardcoded 8 fixed Sarathi product areas). Genericized to a
product-agnostic AI Business Analysis copilot at the user's explicit
request — the Sarathi-specific prompt/schema/renderer were deleted outright
(confirmed dead code at the time: never wired into generation, never in any
filename allowlist), replaced by a generic FRD derived from
`facts["requirements"]`.

## On-demand document generation — the core architecture

**No document generates automatically.** Earlier versions of this project
generated MOM + Meeting Analysis right after every meeting; that's gone.
`orchestrator.py`/`orchestrator_streaming.py` now stop at: transcript
assembled → **one** Gemini call (extraction) → `facts.json` written → run
reaches `state="saved"`. Every actual document (MOM, Meeting Analysis, BRD,
FRD, User Stories, Acceptance Criteria, Business Process Flow) is generated
only when a user clicks "Generate" on the dashboard for that specific
document — see `app/main.py`'s `POST /meetings/{run_id}/documents/{doc_key}/generate`
route and `app/document_generation_state.py` for the in-flight tracking.
This is the project's actual Gemini-quota-management mechanism now — the
user is the throttle, not a config flag or a disabled-by-default document.

`app/docgen/registry.py` is the single source of truth for "what documents
exist, what group generates them, and what files they write":

```python
DOCUMENTS = {
    "mom":                   {...},
    "meeting_analysis":      {...},
    "brd":                   {...},
    "frd":                   {...},   # zero-Gemini-call, see below
    "user_stories":          {...},   # shares one Gemini call with...
    "acceptance_criteria":   {...},   # ...this one
    "business_process_flow":{...},   # zero-Gemini-call, see below
}
```
`GROUPS` maps a group key to its generator function (in `engine.py`) and
which document key(s) it produces. `kind` (on each group) distinguishes:
- `local` — no Gemini call at all, instant, can't fail on quota/network
  (FRD, Business Process Flow).
- everything else — one real Gemini call, either producing a single
  document (MOM, Meeting Analysis, BRD) or a shared pair (User Stories +
  Acceptance Criteria, from one call).

Adding a new document type means adding one entry to `DOCUMENTS` (and to
`GROUPS` if it's not sharing an existing group) — nothing else in `main.py`,
`orchestrator.py`, or `scripts/regenerate_docs.py` needs to change, since
all of them are registry-driven now.

## The extraction call (Call 1) — still the one automatic Gemini call

`extract_prompt.py`'s `EXTRACT_RESPONSE_SCHEMA` + `EXTRACT_SYSTEM_PROMPT`,
run via `engine.extract_meeting_facts()`. Forced JSON schema
(`response_mime_type="application/json"` + `response_json_schema`), so the
output is always valid structured data. Fields: `attendees`,
`topics_discussed`, `decisions`, `action_items`, `key_quotes` (original set),
plus `requirements`, `risks`, `assumptions`, `dependencies`,
`open_questions`, `commitments`, and an optional `business_process` object —
all added for the generic BA-copilot expansion. Same grounding discipline
throughout: never invent, empty list over fabrication, copy an
`Unidentified speaker N` label verbatim, and (new) mark a `requirements[]`/
`business_process.steps[]` entry's `status` as `"needs_clarification"`
rather than guessing when the transcript left something ambiguous.

`requirements[]` is the first-class list every derived document reads from
— each has a stable `id` (e.g. `REQ-1`), so BRD/FRD/User Stories/Acceptance
Criteria can all reference "REQ-3" and mean the same thing.

`business_process` is `null` unless a participant actually walked through
an AS-IS process on the call — see "Business Process Flow" below.

Disambiguation the prompt spells out explicitly (these three otherwise
blur together): `action_items` = internal follow-up work, no firm promise;
`commitments` = an explicit promise made *to someone*; `decisions` =
something the group agreed on, not a task.

## Document generation (Call 2, per document/group) — `engine.py`

Every generator function shares one signature —
`(meeting_title, meeting_date, attendees, facts, transcript_text,
recorder=None) -> dict | None` — which is what lets `registry.py` dispatch
to any of them the same way from one "Generate" click:

- **`generate_mom` / `generate_meeting_analysis` / `generate_brd`** — prose
  `{title, markdown_body}` via `DOCUMENT_RESPONSE_SCHEMA`, grounded via
  `_generate_document()` (shared helper: builds `{meeting_title,
  meeting_date, attendees, extracted_facts}` + the full transcript as the
  Gemini call's content). Each is only given Call 1's facts plus transcript
  excerpts, with an explicit instruction to never invent a name/date/number/
  decision, and to write `"Not discussed in this meeting"` rather than
  fabricate an uncovered section (`_GROUNDING_RULE` in `generate_prompt.py`,
  shared by every document).
- **`generate_frd`** — **zero Gemini calls.** A pure Python render of
  `facts["requirements"]` (`render_tables.py:render_frd_markdown()`),
  replacing the deleted Sarathi-specific Requirement Gathering Sheet.
- **`generate_user_stories_and_acceptance_criteria`** — **one shared Gemini
  call** producing `{stories: [{requirement_id, as_a, i_want, so_that,
  priority, acceptance_criteria}]}`, then rendered into *two* separate
  documents (`render_user_stories_markdown()`, `render_acceptance_criteria_markdown()`)
  reading the same list — saves a call versus generating each independently.
  A requirement with `status: "needs_clarification"` still gets a story, but
  its `acceptance_criteria` is a single `"Needs Clarification: ..."` entry
  rather than invented criteria papering over an unclear requirement.
- **`generate_business_process_flow`** — see its own section below.

Every generator that skips Gemini for lack of material (empty transcript, no
`requirements[]`, no `business_process`) returns either a clearly-labeled
placeholder document (`_placeholder_doc()`, e.g. "No speech was captured...")
or, for Business Process Flow specifically, `None` — the registry's caller
(`main.py`'s `_generate_document_group()`) treats `None` as "genuinely
nothing to generate," not a failure, and the dashboard shows "Not applicable
to this meeting" instead of a Generate button.

## Business Process Flow — the newest document, and the only diagram

**Purpose:** a client verbally walks through their current (AS-IS) business
process on the call; this turns that into a proper flowchart — steps,
actors, decision points with Yes/No branches, approvals, inputs/outputs,
system interactions, exceptions, and alternate flows — without a BA manually
re-listening to the recording and drawing it in Visio/Lucidchart. **V1 scope
is AS-IS only** — a TO-BE (redesigned/future) process is an explicit
non-goal for now.

**Deliberately zero Gemini calls beyond the shared extraction call.** The
"never invent a step" grounding rule lives entirely in Call 1's prompt/schema
(`extract_prompt.py`'s `business_process` field, described above) — turning
already-extracted, already-grounded structured data into diagram syntax is a
mechanical, deterministic transformation
(`render_diagram.py:render_business_process_mermaid()`, pure function, no
I/O), not a second LLM call asked to both interpret the transcript *and*
produce syntactically valid Mermaid. This is both safer (no risk of a second
invented step, or of the model producing invalid Mermaid syntax mid-diagram)
and free (same tier as `generate_frd`).

**Rendering rules** (`render_diagram.py`): node shape by `type` — `start`/
`end` → stadium, `process` → rectangle, `decision` → rhombus, `approval` →
parallelogram, `system` → subroutine (double-bordered rectangle) — so a
system interaction is visually distinct from a human step at a glance.
Decision nodes get `Yes`/`No`-labeled edges from `on_yes_step_id`/
`on_no_step_id`; every other step follows `next_step_id`. An
`alternate_flow`/`exception_notes` value becomes a dashed side-note node
attached to its step, rather than silently folding into the main path or
being dropped. **Any step with `status == "needs_clarification"` gets a
distinct dashed/warning `classDef` and a "⚠ Needs Clarification: " label
prefix** — the concrete implementation of "never invent, mark unclear parts"
for a diagram specifically; visually impossible to miss, unlike a footnote.

**PDF export** (`render_diagram.py:render_mermaid_to_pdf()`): reuses the
project's existing Playwright/headless-Chrome machinery
(`render_pdf.py`'s `channel="chrome"`-first-then-bundled-Chromium fallback,
and its `_RENDER_LOCK`, since Playwright's sync API isn't thread-safe for
concurrent calls in one process — confirmed the hard way once already for
markdown PDF rendering; the same constraint applies here). Renders via a
**vendored** local copy of `mermaid.js`
(`app/web/static/vendor/mermaid.min.js` — no CDN dependency, consistent with
this project's "runs locally, Gemini is the only external API" philosophy).

Two mechanics worth knowing before touching this function:
- **The HTML page is loaded via a real temp `.html` file + `page.goto("file://...")`,
  not `page.set_content()`.** Confirmed empirically: a page loaded with
  `set_content()` has no real origin (effectively `about:blank`), and
  headless Chrome silently refuses to load a `<script src="file://...">`
  from a non-`file://` page — `mermaid` stayed `undefined` and
  `wait_for_function()` timed out. Giving the HTML document itself a
  `file://` origin (written to a temp file next to the destination PDF)
  fixes this.
- **PDF page size is computed dynamically from the rendered SVG's actual
  bounding box** (`getBBox()`, via `page.evaluate()`), not a fixed page
  size — a flow with many steps/branches can render very tall or wide, and
  a naive fixed size would clip it. Capped (4–60in wide, 4–200in tall) so
  one pathologically large diagram can't produce an unusable
  multi-hundred-inch PDF.

The raw `.mmd` (editable Mermaid source) is written directly via the
existing `write_meeting_file()` — no rendering involved, and it's the
reusable/editable artifact if someone wants to tweak the diagram (or build a
TO-BE version from it) later, opening it in any Mermaid-compatible editor.

## Gemini's schema format is not standard JSON Schema

Worth calling out explicitly since it's an easy thing to get subtly wrong:
Gemini's `response_json_schema` uses **UPPERCASE type strings**
(`"OBJECT"`, `"ARRAY"`, `"STRING"`, `"NUMBER"`, not lowercase), and
**doesn't support JSON Schema's nullable-union syntax**
(`"type": ["string", "null"]`, which the old Anthropic-flavored schemas
used) — nullable fields instead need `"type": "STRING", "nullable":
true`. Every schema module (`extract_prompt.py`, `generate_prompt.py`)
already follows this; if you add a new field, match the existing style
rather than copying generic JSON Schema examples from elsewhere.

## `render_tables.py`

Pure functions, no Gemini/Playwright dependency, unit-tested in
`tests/test_render_tables.py`: `render_frd_markdown()`,
`render_user_stories_markdown()`, `render_acceptance_criteria_markdown()`
(the Mermaid-specific renderer lives in `render_diagram.py` instead, since
it produces diagram syntax, not a markdown table). Two correctness details
that are easy to miss and were deliberately handled:
- Markdown table cells can't contain a literal newline, so multi-line
  fields are joined with `<br>` instead — `render_pdf.py`'s `"tables"`
  markdown extension renders that inline HTML fine.
- A literal `|` character anywhere in a cell would otherwise corrupt the
  table structure, so every cell is escaped (`|` → `\|`) before assembly.

## `render_pdf.py`

Markdown → HTML (via `python-markdown`, `"tables"` extension) → PDF (via
Playwright + headless Chrome). Tries `channel="chrome"` (the system
install) first and falls back to Playwright's bundled Chromium if that
channel isn't registered — avoids requiring a separate Chromium download
on a machine that already has Chrome. Provider-agnostic, unaffected by the
Claude → Gemini switch. `render_diagram.py`'s `render_mermaid_to_pdf()`
reuses this module's `_RENDER_LOCK` directly (same process-wide Playwright
concurrency constraint applies to both).

## `scripts/regenerate_docs.py`

Re-runs one or more registry documents against an existing
`transcript.json` (extracting `facts.json` first if it doesn't already
exist next to it) without re-transcribing or re-diarizing — useful for
iterating on prompts, and the documented recovery path when the pipeline
died after transcription but before extraction. Registry-driven (see
`app/docgen/registry.py`) rather than a separate hardcoded doc list, so it
never drifts from what the dashboard's "Generate" buttons actually do.
