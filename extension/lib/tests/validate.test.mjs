import test from "node:test";
import assert from "node:assert/strict";

import { reviewMarkdownDocument } from "../validate.js";

const FACTS = { requirements: [{ id: "REQ-1" }, { id: "REQ-2" }, { id: "REQ-3" }] };

test("a clean FRD has no findings", () => {
  const heads = [
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
  ];
  let body = heads.map((h) => `## ${h}\nContent for ${h}.`).join("\n\n");
  body += "\nCovers REQ-1, REQ-2 and REQ-3.";
  assert.deepEqual(reviewMarkdownDocument("frd", body, FACTS), []);
});

test("missing section and uncovered requirements are flagged", () => {
  const body = "## 1. Purpose & Scope\nText mentioning REQ-1 only.";
  const f = reviewMarkdownDocument("frd", body, FACTS);
  assert.ok(f.some((x) => x.includes("Missing required section") && x.includes("Actors & Roles")));
  assert.ok(f.some((x) => x.includes("Does not reference 2 requirement(s): REQ-2, REQ-3")));
});

test("leaked artifacts are flagged", () => {
  const body = '## 1. Document Control\nText.\n```json\n{"x": 1}\n```\n<|im_end|>';
  const f = reviewMarkdownDocument("brd", body, FACTS);
  assert.ok(f.some((x) => x.includes("code fence")));
  assert.ok(f.some((x) => x.includes("chat control token")));
});

test("an unknown REQ-id citation is flagged", () => {
  const body = "## 1. Purpose & Scope\nSee REQ-1 and REQ-9.";
  const f = reviewMarkdownDocument("frd", body, FACTS);
  assert.ok(f.some((x) => x.includes("REQ-9, which is not in facts.json")));
});

test("an empty section is flagged", () => {
  const body = "## Executive Snapshot\n\n## Key Discussion Points\nsome text";
  const f = reviewMarkdownDocument("meeting_analysis", body, { requirements: [] });
  assert.ok(f.some((x) => x.includes('Empty section: "Executive Snapshot"')));
});

test("an untemplated doc_key only gets leak + citation checks", () => {
  const body = "# User Stories\n\n## Lead Management\n| REQ-1 | rep | x | y | high |";
  assert.deepEqual(reviewMarkdownDocument("user_stories", body, FACTS), []);
});

test("a clean MOM has no findings", () => {
  const heads = [
    "Meeting Overview",
    "Agenda / Topics Covered",
    "Discussion Summary",
    "Decisions",
    "Action Items",
    "Commitments",
    "Open Points / Parking Lot",
    "Next Steps",
  ];
  const body = heads.map((h) => `## ${h}\nReal content for ${h}.`).join("\n\n");
  assert.deepEqual(reviewMarkdownDocument("mom", body, { requirements: [] }), []);
});
