import test from "node:test";
import assert from "node:assert/strict";

import {
  applyReconciliation,
  buildLabelMap,
  distinctUnidentifiedLabels,
  isUnidentifiedLabel,
  maybeReconcileSpeakers,
  mergeRealNames,
  placeholderDominance,
} from "../reconcile.js";

const seg = (speaker, text, start = 0) => ({ start, end: start + 1, speaker, text });

test("isUnidentifiedLabel matches only the clean placeholder", () => {
  assert.ok(isUnidentifiedLabel("Unidentified speaker 1"));
  assert.ok(isUnidentifiedLabel("Unidentified speaker 138"));
  assert.ok(!isUnidentifiedLabel("Speaker 1"));
  assert.ok(!isUnidentifiedLabel("Priya"));
});

test("distinctUnidentifiedLabels keeps first-appearance order", () => {
  const segs = [seg("Unidentified speaker 2", "a"), seg("Priya", "b"), seg("Unidentified speaker 2", "c"), seg("Unidentified speaker 1", "d")];
  assert.deepEqual(distinctUnidentifiedLabels(segs), ["Unidentified speaker 2", "Unidentified speaker 1"]);
});

test("placeholderDominance is a character-weighted fraction", () => {
  assert.equal(placeholderDominance([seg("Priya", "hello")]), 0);
  assert.equal(placeholderDominance([seg("Priya", "x".repeat(10)), seg("Unidentified speaker 1", "y".repeat(90))]), 0.9);
});

test("buildLabelMap inverts participants and skips unknown / self / non-int", () => {
  const rec = {
    participants: [
      { canonical_label: "Priya", speaker_numbers: [1, 2] },
      { canonical_label: "Unidentified speaker 3", speaker_numbers: [3] }, // maps to itself -> skipped
      { canonical_label: "Sam", speaker_numbers: [99] }, // 99 not in known -> skipped
    ],
  };
  assert.deepEqual(buildLabelMap(rec, ["Unidentified speaker 1", "Unidentified speaker 2", "Unidentified speaker 3"]), {
    "Unidentified speaker 1": "Priya",
    "Unidentified speaker 2": "Priya",
  });
});

test("applyReconciliation rewrites segments, drops resolved excerpts, returns real names", () => {
  const segs = [seg("Unidentified speaker 1", "hi"), seg("Unidentified speaker 2", "bye")];
  const rec = {
    participants: [
      { canonical_label: "Priya", is_real_name: true, speaker_numbers: [1] },
      { canonical_label: "Participant 2", is_real_name: false, speaker_numbers: [2] },
    ],
  };
  const out = applyReconciliation(segs, rec, { "Unidentified speaker 1": "hi...", "Unidentified speaker 2": "bye..." });
  assert.deepEqual(out.segments.map((s) => s.speaker), ["Priya", "Participant 2"]);
  assert.deepEqual(out.excerpts, { "Participant 2": "bye..." }); // Priya's excerpt dropped
  assert.deepEqual(out.realNames, ["Priya"]);
});

test("mergeRealNames adds discovered names, skips ones already present", () => {
  assert.deepEqual(mergeRealNames(["Priya"], ["Priya", "Sam"]), ["Priya", "Sam"]);
  assert.deepEqual(mergeRealNames([], ["Aditya"]), ["Aditya"]);
});

// --- the trigger ---

function fakeClient(result) {
  return {
    calls: 0,
    async generateJson() {
      this.calls++;
      return result;
    },
  };
}

test("maybeReconcileSpeakers is a no-op below the label threshold", async () => {
  const client = fakeClient({ participants: [] });
  const segs = [seg("Priya", "x".repeat(100)), seg("Unidentified speaker 1", "y".repeat(10))];
  const out = await maybeReconcileSpeakers({ client, meetingTitle: "M", segments: segs, attendees: ["Priya"] });
  assert.equal(client.calls, 0);
  assert.equal(out.segments, segs);
});

test("maybeReconcileSpeakers fires when placeholders dominate and applies the result", async () => {
  const client = fakeClient({
    participants: [{ canonical_label: "Priya", is_real_name: true, speaker_numbers: [1, 2, 3] }],
  });
  const segs = [1, 2, 3].map((i) => seg(`Unidentified speaker ${i}`, "word ".repeat(20), i));
  const out = await maybeReconcileSpeakers({ client, meetingTitle: "M", segments: segs, attendees: [] });
  assert.equal(client.calls, 1);
  assert.deepEqual(new Set(out.segments.map((s) => s.speaker)), new Set(["Priya"]));
  assert.deepEqual(out.attendees, ["Priya"]);
});

test("maybeReconcileSpeakers swallows a Gemini failure and keeps the labels", async () => {
  const client = {
    async generateJson() {
      throw new Error("boom");
    },
  };
  const segs = [1, 2, 3].map((i) => seg(`Unidentified speaker ${i}`, "word ".repeat(20), i));
  const out = await maybeReconcileSpeakers({ client, meetingTitle: "M", segments: segs, attendees: ["x"] });
  assert.equal(out.segments, segs);
  assert.deepEqual(out.attendees, ["x"]);
});
