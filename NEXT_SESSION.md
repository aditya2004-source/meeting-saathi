# NEXT SESSION — start here

Updated **2026-08-29**. Two rounds of work on top of the 2026-08-27 first-test
findings:
- **Round 1 (2026-08-28):** the 5 flagged defects — fixed + verified.
- **Round 2 (2026-08-29):** a full BA-professional enhancement pass on every
  document (free tier — prompt engineering + deterministic checks, no extra
  Gemini calls per meeting).

This file records what changed and what is still open.

**Round 3 (2026-08-29):** user will process **one meeting per day** and wants that
one done as well as possible, so `docgen_quality_mode` (default **on**) spends the
free-tier budget on quality: extraction gets a **verification/gap-fill pass** (union
merge, `unsupported_items` removal), and every Gemini-written document gets a
**refine pass** against the deterministic validator's findings ("complete every
section from facts/transcript, cover every requirement, drop anything unsupported").
Every extra pass fails soft — a 429 or a suspiciously short result keeps the prior
version. Set `DOCGEN_QUALITY_MODE=false` to go back to the minimal-calls behaviour.

---

## ✅ DONE 2026-08-31 — Round 2/3 verification regen complete

Clean full regen ran on the Imdadi BuildMart meeting (`gemini-3.5-flash` was
throwing intermittent **503 "high demand"** — not quota — so it took a retry
loop; all 8 docs + `facts.json` regenerated, `write_generated_document()`
validator produced **no `.review.md` sidecars** = deterministic checks all pass).

**Result — quality is strong. Extraction (quality mode):** 13 topics, 18
requirements, 4 risks, 3 assumptions, 3 dependencies, 2 open questions, 2 business
processes, all Round-2 context fields (goals 4, current_state 5, glossary 5,
systems 6, constraints 2). `decisions` = 1 (meeting genuinely had ~1 firm
decision; MOM/Analysis synthesise "direction" points) — watch item D still
inconclusive, likely fine.
- **MOM** — thorough: 16 agenda topics, per-topic discussion, decision log +
  action table + commitments + parking lot + next steps. Clean.
- **Meeting Analysis** — strong; Analyst's Note correctly nails the Busy
  integration as the key ambiguity.
- **FRD** — 11 sections, FRs traced to REQ-ids, traceability table. Minor
  over-reach: §7 adds a speculative "Tally ERP" integration row (Tally was only
  named once, as a comparison inside an open question).
- **User Stories** — grouped by epic, roles correctly inferred (no "As a
  Mustafa"). Clean.
- **Acceptance Criteria** — clean.
- **Traceability Matrix** — 18/18 covered; REQ-13 correctly flagged "Needs
  clarification".
- **Business Process Flow** — 2 titled subgraphs, decision diamonds, exception
  side-notes, dead-end markers, inputs/outputs/system annotations. Big jump from
  the shallow Round-1 version the user flagged.

**⚠ One substantive defect — BRD §6 "Future considerations":**
> "Integration with Tally accounting software as an alternative to check
> outstanding payment details."

Wrong twice: (a) it's **Busy**, not Tally — every other BRD section (§8 REQ-13,
§10 constraint, §11 dependency, §13) says Busy correctly; (b) it's framed as a
future/alternative item when the accounting integration is a live requirement
(REQ-13) + hard constraint — internally contradicts its own §8/§10/§11.
Root cause: the one-off comparison mention of "Tally" in an open question is
being promoted into scope/integration content by the BRD (and mildly the FRD)
generation. MOM / Meeting Analysis / User Stories / Traceability all handle it
right. **Fix candidate:** add a `_GROUNDING_RULE` clause in `generate_prompt.py`
— "a system/tool named only as a comparison or in a question is not in scope;
never list it as a requirement, integration, or scope item" — then regenerate
BRD + FRD only (`regenerate_docs.py "<t>.json" brd frd`, ~4 calls with quality
mode). Minor polish: MOM commitment prose ends "...by Not specified" when no due
date — drop the clause when absent.

---

## Original regen instructions (kept for reference)

```bash
cd "/home/enjay/Downloads/Meeting Saathi/Sangam + Sarathi Demo - Imdadi BuildMart - 2026-08-27 1153"
rm -f facts.json *.review.md
python scripts/regenerate_docs.py "$PWD/transcript.json"
```

Original note follows:

```bash
cd "/home/enjay/Downloads/Meeting Saathi/Sangam + Sarathi Demo - Imdadi BuildMart - 2026-08-27 1153"
rm -f facts.json *.review.md
python /home/enjay/projects/meeting-saathi/scripts/regenerate_docs.py "$PWD/transcript.json"
```

**With `docgen_quality_mode` on (the default — see Round 3 below), this is ~14 calls
(worst case ~17 with MAX_TOKENS retries), sized for one meeting/day on the free
tier:** extraction 3 (core + verify + context), then generate+refine ×5 (MOM, Meeting
Analysis, BRD, FRD, stories), Business Process Flow 0. Reconciliation (already done
for this meeting) is +1 when it fires. Then review all 8 `.md` outputs and any
`*.review.md` sidecars.

If the quota is tight, set `DOCGEN_QUALITY_MODE=false` in `.env` for a ~7-call run
(no verify/refine passes), or regenerate specific docs:
`python scripts/regenerate_docs.py "$PWD/transcript.json" brd frd`.

**Verified so far (2026-08-29):**
- 2-call extraction split fixed recall: topics 9→14, decisions 1→3, risks/assumptions/
  dependencies/open_questions 1/1/1/1 → 2/3/3/2. (The context call — goals/glossary/
  current_state/etc — 429'd; graceful degradation kept the core facts.)
- Business Process Flow renderer, traceability matrix, and the `validate.py` quality
  checker all work (0 Gemini calls). The validator correctly flags the currently
  on-disk MOM/BRD (generated from an older facts.json with 11 reqs) as citing
  REQ-10/REQ-11 that the new 9-req facts.json doesn't have — that resolves on the
  clean regen above. The on-disk FRD/User Stories/Acceptance Criteria are still the
  Round 1 versions.
- Round 3 (quality mode: verify + refine passes) is code-complete + unit-tested but
  has had NO live Gemini run at all yet.

---

## Round 2 — BA-professional enhancement (2026-08-29)

**Richer extraction, split into 2 focused calls** (`extract_prompt.py` +
`engine.extract_meeting_facts()`): `facts.json` now also carries `meeting_purpose`,
`goals` (with rationale), `current_state` (area / how-it-works / pain),
`constraints`, `systems_and_integrations`, `glossary`, `non_functional_notes`, and
per-requirement `rationale` + `acceptance_hint`.
- **Why two calls:** bolting all of that onto one schema measurably *dropped*
  recall on the plain lists (risks/assumptions/dependencies/open_questions fell from
  ~3 each to ~1 each). `CORE_EXTRACT_*` = the concrete record; `CONTEXT_EXTRACT_*` =
  BA-analysis context; merged into one `facts.json`. If call 2 fails, call 1's facts
  are still returned (context fields empty). Cost: 2 one-time calls per meeting,
  still well within the free tier; doc generation stays on-demand.
- `empty_meeting_facts()` and `business_processes_from_facts()` updated; old
  `facts.json` files still render. Token budgets: core 16384, context 8192, BRD
  16384, FRD 24576, `_MAX_OUTPUT_TOKENS_CAP` 32768 → 49152.
- Markdown post-processing hardened: collapses over-escaped `\\\\n`, decodes leaked
  HTML entities (`&amp;` → `&`).

**Every document prompt rewritten to a senior-BA standard** (`generate_prompt.py`,
shared `_BA_VOICE` + hardened `_GROUNDING_RULE` with a self-check clause):
- **MOM** → Meeting Overview (table) · Agenda · Discussion Summary (per-topic) ·
  Decisions (table w/ rationale) · Action Items (table w/ owner/due/REQ) ·
  Commitments · Open Points / Parking Lot · Next Steps.
- **Meeting Analysis** → Executive Snapshot · Key Discussion Points · Decisions &
  Direction · What Needs To Happen Next · Risks & Open Questions · **Analyst's Note**.
- **BRD** → **15 sections** (adds Business Context, Current State & Pain Points,
  Objectives w/ rationale, Scope incl. Future considerations, Constraints, Risks as
  a table w/ mitigation, Success Criteria & KPIs, Glossary).
- **FRD** → **11 sections** (adds Actors & Roles, FR-N ids traced to REQ-ids,
  Data Requirements, Integration Requirements, Non-Functional Requirements, a
  Requirements Traceability table).
- **User Stories** → grouped by **epic**, role inference fixed (no more "As a
  Mustafa (Imdadi BuildMart)" — infers a functional role when the stakeholder is a
  person's name).
- **Acceptance Criteria** → **Given/When/Then**, rendered as per-story sections
  (not cramped table cells).

**New deliverable: Requirements Traceability Matrix** (`Traceability_Matrix.md/.pdf`)
— deterministic, **0 Gemini calls**, produced by the same shared stories call. Every
REQ-id in one row: category, priority, status, whether a user story + acceptance
criteria exist. Registry: added to the `stories_and_ac` group's `produces`.

**Automated quality validator** (`app/docgen/validate.py`, `app/docgen/output.py`,
**0 Gemini calls**): after each markdown doc is written, deterministic checks run —
leaked chat/JSON artifacts, missing template section, empty section, a cited REQ-id
that isn't in `facts.json`, requirements the BRD/FRD never reference, too many
"Not discussed" sections. Findings are written next to the doc as
`<Base>.review.md` (deleted when clean) and printed by `regenerate_docs.py`. Wired
into **both** write paths via the new shared `write_generated_document()`.

Tests: **195 passing**. New: `tests/test_validate.py`; `tests/test_render_tables.py`
rewritten for the new renderers.

**Deliverables are now 8:** MOM, Meeting Analysis, BRD, FRD, User Stories,
Acceptance Criteria, Requirements Traceability Matrix, Business Process Flow.

---

## Round 1 — the five flagged defects (2026-08-28)

Test meeting folder:
`/home/enjay/Downloads/Meeting Saathi/Sangam + Sarathi Demo - Imdadi BuildMart - 2026-08-27 1153/`
(original transcript backed up as `transcript.pre-reconcile.json`.)

---

## What was fixed this session

### 1. Speaker identification — DONE

New **speaker-reconciliation** pass: one Gemini call over the full transcript that
collapses a failed diarization's "Unidentified speaker N" fragments into the true,
small participant set, assigning a real name only where the transcript unambiguously
reveals it, else a stable `Participant N` label.

- `app/docgen/reconcile_prompt.py` — prompt + compact schema (participants, each with
  `speaker_numbers`).
- `app/docgen/engine.py:reconcile_speaker_identities()` — the Gemini call.
- `app/pipeline/speaker_reconcile.py` — pure functions (dominance metric, label-map
  build, apply, attendee merge). No I/O, no Gemini call.
- `app/reconcile.py:maybe_reconcile_speakers()` — trigger glue used by **both**
  orchestrators (streaming + legacy), right after `fill_unresolved_with_excerpts()`.
- `scripts/reconcile_speakers.py` — standalone recovery for an existing meeting
  (`--roster "..."`, `--regenerate`).

**Permanent behaviour (the "free" choice):** the pass fires **only** when placeholder
labels dominate — `speaker_reconcile_min_dominance` (default 0.4 of transcript chars)
**and** `speaker_reconcile_min_labels` (default 6) distinct labels. A normal,
well-diarized meeting triggers **zero** extra Gemini calls. Toggle:
`speaker_reconcile_enabled`. `KEEP_RAW_RECORDING` was left `false` (reconciliation
handles it; disk/privacy).

**Result on the test meeting:** 138 labels → 5 participants — Mustafa (Imdadi
BuildMart, client); Uzeba / Aditya / Dhaval (NJIT Solutions, vendor — Sangam &
Sarathi are the demoed products); Participant 1 (NJIT Solutions, unnamed).

### 2. All-placeholder attendee roster — DONE

`app/docgen/engine.py:_attendees_for_prompt()` collapses a roster that is *entirely*
`Unidentified speaker N` / `Participant N` to one sentence ("N distinct speakers took
part; individual names could not be identified…") before it reaches Gemini. A roster
with even one real name passes through untouched. `_GROUNDING_RULE` updated to match.
(Mostly moot for the test meeting now that #1 works, but the guard is in.)

### 3 + 4a. Extraction recall — DONE

`app/docgen/extract_prompt.py`:
- New **COMPLETENESS** section pushing every topic / decision / action item /
  commitment (target 12-25 topics for a 60-90 min meeting), explicitly subordinate to
  the no-invention rule.
- `business_process` (single nullable object) → **`business_processes`** (list); prompt
  asks for one entry per distinct AS-IS process and fuller step recall.
- `extract_meeting_facts()` token budget 8192 → 12288.
- MOM prompt: dropped the hard "keep it concise" squeeze, added explicit "cover every
  topic, draw from transcript where facts are thin".

Backward compat: `engine.business_processes_from_facts()` reads either key, so old
`facts.json` files still render.

**Result:** test meeting facts went 6→10 topics, 1→2 action items, 1 business process
of 6 thin steps → 1 of 11 detailed steps. MOM is now genuinely complete (all 10
topics, every requirement cross-referenced, risks/assumptions/dependencies/open
questions all populated).

### 4b. Business Process Flow renderer — DONE

`app/docgen/render_diagram.py:render_business_process_mermaid()`:
- Multiple processes → one titled `subgraph` each (node ids prefixed `p1_`, `p2_`…).
- inputs / outputs / system_interaction now rendered as an italic sub-line on the node
  (were silently dropped before).
- A non-terminal step with no successor gets an explicit `(process ends / not carried
  forward)` stop marker + red dashed `deadEnd` style instead of a dangling box.
- Legacy 2-arg call `render_business_process_mermaid(name, steps)` still works.

### 5. BRD + FRD — DONE

- **BRD** (`generate_prompt.py:BRD_SYSTEM_PROMPT`): rewritten to a fixed **12-section
  numbered template** (Document Control → Success Criteria). Explicit "emit these
  headings and NO OTHERS; never turn an instruction into a heading" — kills the old
  leak of `## 2. Product-Agnostic Scope` / `## 4. Product Discussion Headings`.
- **FRD**: promoted from a zero-call bare requirements table to a **1-Gemini-call**
  real FRD (`FRD_SYSTEM_PROMPT`) — functional modules, per-requirement
  inputs/processing/outputs, business rules, data & integration. Registry entry
  `frd` is now `local=False`. `render_frd_markdown()` + its tests deleted.
- New guard `engine._strip_trailing_model_noise()` — trims leaked chat-template
  tokens (`<|im_end|>`, `_dst_id_=`), orphan trailing code fences, and "use this
  verbatim" meta tails from any `markdown_body`. Confirmed once in production on this
  meeting's first FRD regen.

Tests: 186 passing (`pytest`). New files: `tests/test_speaker_reconcile.py`,
`tests/test_render_diagram.py`; `tests/test_render_tables.py` trimmed.

---

## Still open

### A. Extension scraper root-cause (#1c) — needs a live Meet call

`extension/content_script.js`'s active-speaker selectors (`[class*="speaking" i]`,
`[data-is-speaking="true"]`, `[aria-label*="is speaking" i]`) caught **zero** events
for the whole 82-min meeting → `chunk_dom_coverage` 0.0 everywhere → every chunk fell
to per-chunk diarization. The reconciliation pass above is the safety net, but the
real fix is updating/hardening those selectors against current Meet DOM. Requires
joining a real multi-party Meet call to inspect. Consider also: when DOM coverage is
globally ~0, do ONE whole-meeting diarization pass instead of per-chunk (needs
`KEEP_RAW_RECORDING=true`).

### B. Transcript not translated to English on the fallback path

This meeting's `transcript.txt`/`.json` are raw Devanagari/Urdu — `whisper_task
="translate"` only applies to the DOM-primary Whisper path, not the pyannote /
AssemblyAI fallback branch (already noted in `extract_prompt.py:LANGUAGE_RULE`). The
generated documents are correct English (Gemini translates), but the stored
transcript isn't. Decide whether that matters.

### C. Minor: User Stories `as_a` phrasing

When a requirement's `stakeholder` is a person ("Mustafa (Imdadi BuildMart)") rather
than a role, stories read "As a Mustafa (Imdadi BuildMart), I want…". The
STORIES_AND_ACCEPTANCE_CRITERIA prompt could prefer an inferred role when the
stakeholder looks like a personal name.

### D. Watch on future meetings

Extraction still returned only 1 decision and 1 business process for an 82-min
meeting. The multi-process schema + recall push are in place; confirm they actually
produce more on a meeting that genuinely has more.

---

## Useful commands

```bash
# reconcile speakers for a meeting whose diarization failed, then regenerate
python scripts/reconcile_speakers.py "<folder>/transcript.json" --regenerate
python scripts/reconcile_speakers.py "<folder>/transcript.json" --roster "Name - Company (role); ..."

# regenerate all (or some) docs from an existing transcript
python scripts/regenerate_docs.py "<folder>/transcript.json" [mom brd frd ...]

pytest                     # 186 tests
systemctl --user status meeting-saathi.service
journalctl --user -u meeting-saathi -f
```

Gemini free tier is ~20 requests/day (see RESUME.md). A full 7-doc regenerate is
~6 calls; the reconciliation pass is 1 more.
