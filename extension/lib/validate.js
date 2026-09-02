/**
 * Deterministic, zero-Gemini-call quality checks on a generated document —
 * port of app/docgen/validate.py. Catches leaked prompt/JSON artifacts, a
 * missing template section, an empty section, a cited REQ-id that doesn't
 * exist, and (for coverage docs) an unreferenced requirement.
 *
 * Pure function. Findings are advisory — the document is still written; they
 * feed the quality-mode refine pass.
 */

const REQUIRED_HEADINGS = {
  mom: [
    "Meeting Overview",
    "Discussion Highlights",
    "Decisions",
    "Action Items",
    "Open Questions",
    "Next Steps",
  ],
  business_process_flow: [
    "Business Summary",
    "How It Works Today",
    "Key Pain Points",
    "Proposed Way of Working",
    "What Changes",
  ],
  brd: [
    "1. Document Control",
    "2. Executive Summary",
    "3. Business Context & Background",
    "4. Current State & Pain Points",
    "5. Business Objectives & Goals",
    "6. Project Scope",
    "7. Stakeholders",
    "8. Business Requirements",
    "9. Assumptions",
    "10. Constraints",
    "11. Dependencies",
    "12. Risks",
    "13. Open Questions",
    "14. Success Criteria & KPIs",
    "15. Glossary",
  ],
  frd: [
    "1. Purpose & Scope",
    "2. Actors & Roles",
    "3. Functional Modules Overview",
    "4. Functional Requirements by Module",
    "5. Business Rules",
    "6. Data Requirements",
    "7. Integration Requirements",
    "8. Non-Functional Requirements",
    "9. Assumptions & Constraints",
    "10. Open Questions",
    "11. Requirements Traceability",
  ],
  meeting_analysis: [
    "Executive Snapshot",
    "Key Discussion Points",
    "Decisions & Direction",
    "What Needs To Happen Next",
    "Risks & Open Questions",
    "Analyst's Note",
  ],
};

const MUST_COVER_ALL_REQUIREMENTS = new Set(["brd", "frd", "traceability_matrix"]);

const LEAK_PATTERNS = [
  [/<\|[a-z_]+\|>/, "chat control token (<|...|>)"],
  [/_dst_id_=/, "internal marker (_dst_id_=)"],
  [/"\s*markdown_body"\s*:/, 'raw JSON key ("markdown_body":)'],
  [/^\s*```(json)?\s*$/im, "stray code fence"],
  [
    /^#+.*\b(use it verbatim|do not insert any other symbols|EXACTLY these (numbered )?headings|extracted_facts\.)/im,
    "prompt instruction used as a heading",
  ],
];

const REQ_ID_RE = /\bREQ-\d+\b/g;
const HEADING_RE = /^(#{1,6})[ \t]+(.+?)[ \t]*#*$/gm;

/** (level, text, charOffset) for every ATX heading. */
function headings(markdown) {
  const out = [];
  HEADING_RE.lastIndex = 0;
  let m;
  while ((m = HEADING_RE.exec(markdown)) !== null) {
    out.push([m[1].length, m[2].trim(), m.index]);
  }
  return out;
}

/**
 * @param {string} docKey
 * @param {string} markdown
 * @param {{ requirements?: Array<{id?: string}> }} facts
 * @returns {string[]} findings
 */
export function reviewMarkdownDocument(docKey, markdown, facts) {
  const findings = [];
  const text = markdown || "";

  // The Business Process Flow doc legitimately contains fenced ```mermaid blocks,
  // so a bare closing ``` is expected there -- flag a fence only if the fences are
  // unbalanced or one opens a non-mermaid block.
  const allowsMermaid = docKey === "business_process_flow";
  for (const [pattern, label] of LEAK_PATTERNS) {
    if (allowsMermaid && label === "stray code fence") {
      const fenceLines = [...text.matchAll(/^\s*```([a-z]*)\s*$/gim)].map((m) => m[1]);
      const openers = fenceLines.filter(Boolean);
      if (fenceLines.length % 2 !== 0 || openers.some((f) => f !== "mermaid")) {
        findings.push(`Leaked artifact: ${label}.`);
      }
      continue;
    }
    if (pattern.test(text)) findings.push(`Leaked artifact: ${label}.`);
  }

  const hs = headings(text);
  const headingTexts = hs.map((h) => h[1]);

  for (const required of REQUIRED_HEADINGS[docKey] || []) {
    if (!headingTexts.some((h) => h.toLowerCase().includes(required.toLowerCase()))) {
      findings.push(`Missing required section: "${required}".`);
    }
  }

  for (let i = 0; i < hs.length; i++) {
    const [level, htext, offset] = hs[i];
    const rest = text.slice(offset);
    const start = rest.includes("\n") ? offset + rest.indexOf("\n") + 1 : text.length;
    const end = i + 1 < hs.length ? hs[i + 1][2] : text.length;
    const nextLevel = i + 1 < hs.length ? hs[i + 1][0] : 0;
    if (!text.slice(start, end).trim() && !(nextLevel > level)) {
      findings.push(`Empty section: "${htext}" has no content.`);
    }
  }

  const knownIds = new Set(
    (facts.requirements || []).filter((r) => r && r.id).map((r) => String(r.id).trim()),
  );
  const cited = new Set(text.match(REQ_ID_RE) || []);
  for (const missing of [...cited].filter((c) => !knownIds.has(c)).sort()) {
    findings.push(`Cites ${missing}, which is not in facts.json.`);
  }

  if (MUST_COVER_ALL_REQUIREMENTS.has(docKey) && knownIds.size) {
    const uncovered = [...knownIds].filter((id) => !cited.has(id)).sort();
    if (uncovered.length) {
      findings.push(`Does not reference ${uncovered.length} requirement(s): ${uncovered.join(", ")}.`);
    }
  }

  const ndCount = (text.match(/Not discussed in this meeting/g) || []).length;
  if (ndCount > Math.max(3, Math.floor((REQUIRED_HEADINGS[docKey] || []).length / 2))) {
    findings.push(
      'Many sections are "Not discussed in this meeting" -- extraction may be thin ' +
        "or the transcript may be low quality.",
    );
  }

  return findings;
}
