import test from "node:test";
import assert from "node:assert/strict";

import { attendeesForPrompt, generateDocument, generateMeetingAnalysis, generateMom, placeholderDoc } from "../generate.js";
import { silentLogger, StubClient } from "./stubclient.mjs";

const FACTS = { requirements: [{ id: "REQ-1" }], topics_discussed: ["Pricing"] };
const BASE = { meetingTitle: "Kickoff", meetingDate: "28 July 2026", attendees: ["Aditya"], facts: FACTS, transcriptText: "[00:00:00] Aditya: hi" };

test("attendeesForPrompt collapses an all-anonymous list", () => {
  assert.deepEqual(attendeesForPrompt(["Unidentified speaker 1", "Unidentified speaker 2"]), [
    "2 distinct speakers took part; individual names could not be identified from the meeting audio",
  ]);
  assert.deepEqual(attendeesForPrompt(["Participant 1"]), [
    "1 speaker took part; individual names could not be identified from the meeting audio",
  ]);
});

test("attendeesForPrompt passes through a list with any real name", () => {
  assert.deepEqual(attendeesForPrompt(["Aditya", "Unidentified speaker 1"]), ["Aditya", "Unidentified speaker 1"]);
  assert.deepEqual(attendeesForPrompt([]), []);
});

test("placeholderDoc format", () => {
  assert.deepEqual(placeholderDoc("Minutes of Meeting", "Sync", "28 July 2026", "No speech."), {
    title: "Minutes of Meeting",
    markdown_body: "# Minutes of Meeting — Sync (28 July 2026)\n\nNo speech.\n",
  });
});

test("generateMom returns a placeholder for an empty transcript without calling Gemini", async () => {
  const client = new StubClient([]);
  const out = await generateMom(client, { ...BASE, transcriptText: "   " });
  assert.equal(client.calls.length, 0);
  assert.match(out.mom.markdown_body, /No speech was captured/);
});

test("generateDocument cleans the body and runs the refine pass in quality mode", async () => {
  const draft = { title: "MOM", markdown_body: "## Meeting Overview\nSolid draft body with enough content to keep." };
  const refined = { title: "MOM", markdown_body: "## Meeting Overview\nSolid draft body with enough content to keep, now improved and expanded." };
  const client = new StubClient([draft, refined]);

  const doc = await generateDocument(client, {
    systemPrompt: "sys",
    responseSchema: {},
    ...BASE,
    docKey: "mom",
    qualityMode: true,
    logger: silentLogger,
  });

  assert.equal(client.calls.length, 2);
  assert.match(doc.markdown_body, /now improved and expanded/);
  // refine call carries the draft + the automated findings block
  assert.match(client.calls[1].userContent, /AUTOMATED CHECK FINDINGS:/);
  assert.match(client.calls[1].userContent, /DRAFT DOCUMENT:/);
});

test("generateDocument keeps the draft when the refine result is suspiciously short", async () => {
  const draft = { title: "MOM", markdown_body: "A".repeat(400) };
  const shortRefine = { title: "MOM", markdown_body: "too short" };
  const client = new StubClient([draft, shortRefine]);
  const doc = await generateDocument(client, { systemPrompt: "s", responseSchema: {}, ...BASE, docKey: "mom", qualityMode: true });
  assert.equal(doc.markdown_body, "A".repeat(400) + "\n");
});

test("generateDocument keeps the draft when the refine call throws", async () => {
  const draft = { title: "MOM", markdown_body: "## Meeting Overview\n" + "content ".repeat(50) };
  const client = new StubClient([draft, new Error("refine 503")]);
  const doc = await generateDocument(client, {
    systemPrompt: "s",
    responseSchema: {},
    ...BASE,
    docKey: "mom",
    qualityMode: true,
    logger: silentLogger,
  });
  assert.match(doc.markdown_body, /Meeting Overview/);
});

test("generateDocument skips refine when quality mode is off", async () => {
  const draft = { title: "MOM", markdown_body: "## Meeting Overview\nbody" };
  const client = new StubClient([draft]);
  await generateDocument(client, { systemPrompt: "s", responseSchema: {}, ...BASE, docKey: "mom", qualityMode: false });
  assert.equal(client.calls.length, 1);
});

test("generateMeetingAnalysis wraps the doc under the meeting_analysis key", async () => {
  const draft = { title: "Analysis", markdown_body: "## Executive Snapshot\n" + "x ".repeat(80) };
  const client = new StubClient([draft]);
  const out = await generateMeetingAnalysis(client, { ...BASE, qualityMode: false });
  assert.ok(out.meeting_analysis.markdown_body.includes("Executive Snapshot"));
});
