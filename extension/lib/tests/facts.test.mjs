import test from "node:test";
import assert from "node:assert/strict";

import { extractMeetingFacts, itemKey, unionVerifiedFacts, emptyMeetingFacts } from "../facts.js";
import { silentLogger, StubClient } from "./stubclient.mjs";

test("itemKey uses the first meaningful field, normalised and truncated", () => {
  assert.equal(itemKey("  Hello   World  "), "hello world");
  assert.equal(itemKey({ id: "REQ-2", statement: "x" }), "req-2");
  assert.equal(itemKey({ description: "The Thing" }), "the thing");
  assert.equal(itemKey({ decision: "A".repeat(200) }).length, 120);
});

test("unionVerifiedFacts keeps items from both passes and dedupes", () => {
  const draft = { decisions: [{ decision: "Ship v1" }], requirements: [{ id: "REQ-1", statement: "a" }] };
  const verified = {
    decisions: [{ decision: "ship v1" }, { decision: "Hire a QA" }],
    requirements: [{ id: "REQ-1", statement: "a (clarified)" }, { id: "REQ-2", statement: "b" }],
    unsupported_items: [],
  };
  const merged = unionVerifiedFacts(draft, verified);
  assert.equal(merged.decisions.length, 2);
  assert.deepEqual(merged.decisions.map((d) => d.decision), ["Ship v1", "Hire a QA"]);
  assert.deepEqual(merged.requirements.map((r) => r.id), ["REQ-1", "REQ-2"]);
});

test("unionVerifiedFacts drops items the verify pass flagged unsupported", () => {
  const draft = { risks: [{ description: "Budget might be cut" }, { description: "Vendor lock-in" }] };
  const verified = { risks: [], unsupported_items: ["budget might be cut"] };
  const merged = unionVerifiedFacts(draft, verified);
  assert.deepEqual(merged.risks.map((r) => r.description), ["Vendor lock-in"]);
});

test("unionVerifiedFacts prefers a non-empty verified scalar", () => {
  const merged = unionVerifiedFacts({ meeting_purpose: null, attendees: ["A"] }, { meeting_purpose: "Kickoff", attendees: [] });
  assert.equal(merged.meeting_purpose, "Kickoff");
  assert.deepEqual(merged.attendees, ["A"]); // empty verified value does not overwrite
});

test("extractMeetingFacts: one combined call by default (concrete record + context)", async () => {
  const combined = {
    ...emptyMeetingFacts(),
    topics_discussed: ["Pricing"],
    decisions: [{ decision: "Ship" }],
    meeting_purpose: "Decide the launch",
    goals: [{ statement: "Grow revenue" }],
  };
  const client = new StubClient([combined]);

  const facts = await extractMeetingFacts(client, "TRANSCRIPT");

  assert.equal(client.calls.length, 1); // was 3 — halved for the free tier
  assert.equal(facts.meeting_purpose, "Decide the launch");
  assert.deepEqual(facts.decisions.map((d) => d.decision), ["Ship"]);
  assert.deepEqual(facts.goals, [{ statement: "Grow revenue" }]);
  // missing fields are backfilled to the empty shape
  assert.deepEqual(facts.risks, []);
  assert.equal("business_processes" in facts, true);
});

test("extractMeetingFacts: quality mode adds one verify pass and unions", async () => {
  const combined = { ...emptyMeetingFacts(), topics_discussed: ["Pricing"], decisions: [{ decision: "Ship" }], meeting_purpose: "P" };
  const verified = { ...combined, decisions: [{ decision: "Ship" }, { decision: "Delay launch" }], unsupported_items: [] };
  const client = new StubClient([combined, verified]);

  const facts = await extractMeetingFacts(client, "T", { qualityMode: true });

  assert.equal(client.calls.length, 2);
  assert.deepEqual(facts.decisions.map((d) => d.decision), ["Ship", "Delay launch"]);
});

test("extractMeetingFacts survives a failing verify pass", async () => {
  const combined = { ...emptyMeetingFacts(), topics_discussed: ["X"], meeting_purpose: "Z" };
  const client = new StubClient([combined, new Error("verify 503")]);
  const facts = await extractMeetingFacts(client, "T", { qualityMode: true, logger: silentLogger });
  assert.deepEqual(facts.topics_discussed, ["X"]);
  assert.equal(facts.meeting_purpose, "Z");
});
