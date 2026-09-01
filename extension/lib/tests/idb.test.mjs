import test from "node:test";
import assert from "node:assert/strict";
import { IDBFactory } from "fake-indexeddb";
import "fake-indexeddb/auto"; // installs IDBKeyRange etc. as globals

import { MEETING_STATUS, MeetingStore } from "../idb.js";

async function freshStore() {
  globalThis.indexedDB = new IDBFactory();
  return MeetingStore.open(globalThis.indexedDB);
}

const blob = (n, type = "audio/webm") => new Blob([new Uint8Array(n)], { type });

test("createMeeting / getMeeting / updateMeeting round-trip", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "r1", title: "Kickoff", model: "gemini-3.6-flash" });
  let m = await store.getMeeting("r1");
  assert.equal(m.title, "Kickoff");
  assert.equal(m.status, MEETING_STATUS.RECORDING);

  const updated = await store.updateMeeting("r1", { status: MEETING_STATUS.PROCESSING, stage: "uploading" });
  assert.equal(updated.status, "processing");
  assert.equal(updated.stage, "uploading");
  m = await store.getMeeting("r1");
  assert.equal(m.stage, "uploading");
  assert.notEqual(m.updatedAt, m.createdAt);
});

test("updateMeeting rejects for an unknown runId", async () => {
  const store = await freshStore();
  await assert.rejects(() => store.updateMeeting("nope", { stage: "x" }), /not found/);
});

test("listMeetings returns newest first", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "a", title: "A", startedAt: "2026-08-01T10:00:00Z" });
  await store.createMeeting({ runId: "b", title: "B", startedAt: "2026-08-03T10:00:00Z" });
  await store.createMeeting({ runId: "c", title: "C", startedAt: "2026-08-02T10:00:00Z" });
  assert.deepEqual((await store.listMeetings()).map((m) => m.runId), ["b", "c", "a"]);
});

test("listResumable returns only recording/processing meetings", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "rec", title: "R" });
  await store.createMeeting({ runId: "proc", title: "P" });
  await store.updateMeeting("proc", { status: MEETING_STATUS.PROCESSING });
  await store.createMeeting({ runId: "done", title: "D" });
  await store.updateMeeting("done", { status: MEETING_STATUS.READY });
  assert.deepEqual((await store.listResumable()).map((m) => m.runId).sort(), ["proc", "rec"]);
});

test("audio parts append, read back in seq order, and assemble", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "r", title: "R" });
  // append out of order — getAudioParts must sort by seq
  await store.appendAudioPart("r", 2, blob(30));
  await store.appendAudioPart("r", 0, blob(10));
  await store.appendAudioPart("r", 1, blob(20));

  const parts = await store.getAudioParts("r");
  assert.deepEqual(parts.map((p) => p.seq), [0, 1, 2]);
  assert.equal(await store.countAudioParts("r"), 3);

  const assembled = await store.assembleAudioBlob("r");
  assert.equal(assembled.size, 60);
  assert.equal(assembled.type, "audio/webm");
});

test("assembleAudioBlob returns null when there are no parts", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "r", title: "R" });
  assert.equal(await store.assembleAudioBlob("r"), null);
});

test("deleteAudioParts clears only that meeting's parts", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "r1", title: "R1" });
  await store.createMeeting({ runId: "r2", title: "R2" });
  await store.appendAudioPart("r1", 0, blob(10));
  await store.appendAudioPart("r2", 0, blob(10));
  await store.deleteAudioParts("r1");
  assert.equal(await store.countAudioParts("r1"), 0);
  assert.equal(await store.countAudioParts("r2"), 1);
});

test("transcripts and documents round-trip", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "r", title: "R" });
  await store.putTranscript("r", { segments: [{ start: 0, end: 1, speaker: "A", text: "hi" }], attendees: ["A"], plainText: "R\n\n[00:00:00] A: hi" });
  const t = await store.getTranscript("r");
  assert.equal(t.attendees[0], "A");

  await store.putDocument("r", "mom", { title: "MOM", markdown: "# MOM" });
  await store.putDocument("r", "meeting_analysis", { title: "Analysis", markdown: "# Analysis" });
  assert.equal((await store.getDocument("r", "mom")).markdown, "# MOM");
  assert.equal((await store.listDocuments("r")).length, 2);
});

test("deleteMeeting cascades to parts, transcript and documents", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "r", title: "R" });
  await store.createMeeting({ runId: "keep", title: "Keep" });
  await store.appendAudioPart("r", 0, blob(10));
  await store.appendAudioPart("keep", 0, blob(10));
  await store.putTranscript("r", { plainText: "x" });
  await store.putDocument("r", "mom", { title: "MOM", markdown: "# MOM" });

  await store.deleteMeeting("r");

  assert.equal(await store.getMeeting("r"), undefined);
  assert.equal(await store.countAudioParts("r"), 0);
  assert.equal(await store.getTranscript("r"), undefined);
  assert.deepEqual(await store.listDocuments("r"), []);
  // untouched
  assert.equal((await store.getMeeting("keep")).title, "Keep");
  assert.equal(await store.countAudioParts("keep"), 1);
});

test("a 90-minute-scale meeting's parts store and assemble intact (G3 shape)", async () => {
  const store = await freshStore();
  await store.createMeeting({ runId: "big", title: "Long meeting" });
  // ~90 min at 20s flushes ≈ 270 parts; use 120 parts × 256 KB ≈ 30 MB total.
  const PARTS = 120;
  const PART_BYTES = 256 * 1024;
  let expected = 0;
  for (let i = 0; i < PARTS; i++) {
    const b = blob(PART_BYTES);
    expected += b.size;
    await store.appendAudioPart("big", i, b);
  }
  assert.equal(await store.countAudioParts("big"), PARTS);
  const assembled = await store.assembleAudioBlob("big");
  assert.equal(assembled.size, expected);

  // coexists with other meetings
  await store.createMeeting({ runId: "small", title: "Other" });
  await store.appendAudioPart("small", 0, blob(1024));
  assert.equal(await store.countAudioParts("big"), PARTS);
  assert.equal(await store.countAudioParts("small"), 1);

  await store.deleteMeeting("big");
  assert.equal(await store.countAudioParts("big"), 0);
  assert.equal(await store.countAudioParts("small"), 1);
});
