# RESUME HERE — updated 2026-09-02

When the founder types **"resume"**, start from this file.

There are **two** Meeting Saathi builds. Keep them straight:

| Build | Folder | Chrome name | What it is |
|---|---|---|---|
| **Personal** | `Admin personal/extension(personal)/` | "Meeting Saathi (Personal)" | Founder's daily driver. Talks to the Python server (`meeting-saathi.service` on :8420, Whisper/AssemblyAI + Gemini). **Do NOT change its code** — founder's rule, it's perfect. Only its manifest `name` was touched. `/Admin personal/` is gitignored. |
| **Shareable** | `extension/` | "Meeting Saathi" | Server-less. Each user pastes their own Gemini key. Gemini T1 transcription. This is what gets handed to other people. Tracked; branch `standalone-extension`. |

Both were made to produce the **same 3 documents at the same quality** this session.

---

## State — what's DONE and verified

### Server / Personal path (`app/`) — commit `fe3f632`, branch `standalone-extension`, pushed
- Doc set = **MOM + Meeting Analysis + Business Process Flow** only (SOW/BRD/FRD/US removed from `registry.py`; `engine.generate_sow` kept).
- `speaker_reconcile_min_labels` **6 → 2** — the name-recovery Gemini pass now fires on essentially every meeting (Meet DOM scrape captures nothing on this box).
- **Transcript translated to English** on every path — `app/pipeline/translate.py` + `engine.translate_transcript_segments()`, wired into both orchestrators, gated on `whisper_task=="translate"`, fail-soft. Fixes the AssemblyAI path storing raw Devanagari.
- **REQ-ids removed** from all client docs (`generate_prompt._grounding_rule` + `engine._REFINE_INSTRUCTION`).
- **MOM simplified** — 6 sections, bullet lists not tables (`MOM_SYSTEM_PROMPT`, `validate._REQUIRED_HEADINGS["mom"]`).
- **Mermaid renders in the PDF** — `render_pdf.py`: was double-rendering (auto-run + manual) → CSS-leak "sticker". Fixed with inline `mermaid.initialize({startOnLoad:false})` + `mermaid.run({suppressErrors:true})` + wait for `data-processed`.
- `engine._fix_headerless_tables()` — injects `| Field | Detail |` header when Gemini omits it.
- `.env GEMINI_MODEL = gemini-3.6-flash` (was 3.5-flash; its 20/day free bucket was exhausted; 3.6 is recommended + separate bucket).
- **All 3 docs regenerated + visually verified** on the founder's meeting "Sangam CRM : Demo" (`~/Downloads/Meeting Saathi/Sangam CRM Demo - 2026-09-02 0722/`). Real names (Subina/Vinay/Kailash/Ashwati/Himanshu/Aditya), English, 0 REQ-ids, flowcharts render.
- Pre-existing: ~10 stale `test_docgen_grounding.py` failures (removed BRD/FRD/US generators, old BPF renderer) — needs a test-cleanup pass, not a regression. 187 pass.
- **One stray folder** in Downloads: `Sangam CRM Demo - 2026-09-02 0726` — a redundant finalize script raced the server; only a partial transcript, no docs, not on the dashboard. Founder said don't delete; safe to remove.

### Shareable extension (`extension/`) — commit `8880c62`, branch `standalone-extension`, pushed
- **Speaker reconcile pass** ported: `lib/reconcile.js` + `RECONCILE_*` in `prompts.js`, wired into `pipeline.js` transcribing stage. E2E-verified with live Gemini: 6 "Unidentified speaker N" → Aditya/Sainath/Rathore + Participant 1-3.
- **REQ-ids removed**, **MOM simplified to 6 sections**, `fixHeaderlessTables` in `gemini.js::cleanMarkdownBody`, `validate.js` headings + mermaid-fence fix.
- **Business Process Flow is now the 3rd doc**: `BUSINESS_PROCESS_FLOW_*` prompt/schema, `generate.js::generateBusinessProcessFlow` (own call + diagram-aware refine), `pipeline.js DOC_KEYS` += it (serialised after the combined MOM+Analysis).
- `vendor/mermaid.min.js` vendored (CSP-clean, no eval). `dashboard.{html,js}`: BPF tab + mermaid render (`mermaid.initialize({startOnLoad:false})` before DOMContentLoaded, `mermaid.run({suppressErrors:true})` via `renderMermaidIn()`). **Browser-verified**: flowchart with decision diamond renders clean, no CSS leak.
- Pipeline ≈ 4 Gemini calls default (transcribe + combined-extract + combined-docs + BPF), ≈ 8 with quality mode.
- **122 tests pass** (`npm test` in `extension/`). New: `lib/tests/reconcile.test.mjs` + BPF/table/fence cases.
- `.zip` rebuilt: `dist/meeting-saathi-v2.0.0.zip` (1.1M, now carries mermaid.min.js). Share = this zip + `extension/SETUP-GUIDE.md`.

---

## DO NEXT (in order)

1. **Phase C — the one blocker before sharing.** Founder loads `extension/` unpacked
   (`chrome://extensions` → Load unpacked → `extension/`), sets his Gemini key in
   Settings, does a **real 2-person Meet call** (~2-3 min), records → leaves. Check
   the dashboard: 3 documents appear, real speaker names, Business Process Flow tab
   shows a rendered flowchart. Every prior real Load-Unpacked run found bugs — this
   new code (reconcile, BPF, mermaid) has never run in a loaded extension.
2. **Gate G1** — one real 45-60 min + one real 75-90 min Hindi/English meeting through
   the extension, confirm Gemini transcribes end-to-end (no dropped middle).
3. **Gate G5** — second machine, different Gemini key, founder's laptop off.
4. When Phase C + G1 pass: hand out `dist/meeting-saathi-v2.0.0.zip` + `SETUP-GUIDE.md`.
   Merge `standalone-extension` → master.
5. Cleanup (low priority): the ~10 stale `test_docgen_grounding.py` tests; the stray
   `Sangam CRM Demo - 2026-09-02 0726` folder.

## Known caveat to tell every user
Gemini free tier ≈ 20 calls/day ≈ 5 meetings. For regular use each user adds billing
to their own Gemini key (~₹1-3/meeting). Covered in `SETUP-GUIDE.md`. Never say
"everything runs locally" — audio goes to the user's own Gemini account.

## Speaker names — the durable fix that's coded but unverified live
`content_script.js` (BOTH builds) active-speaker scraper was rewritten this session
against live Meet DOM (no class contains "speaking" anymore → detect the tile whose
subtree is churning `class` mutations = the audio-bars animation; name from the
doubled `innerText`). Verified live against Meet's DOM on 2026-09-02, but never tested
in a recording. If Phase C shows names still wrong, the reconcile pass is the safety
net (it worked on every test this session).
