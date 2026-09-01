import test from "node:test";
import assert from "node:assert/strict";
import { IDBFactory } from "fake-indexeddb";
import "fake-indexeddb/auto";

import { emptyMeetingFacts } from "../facts.js";
import { MeetingStore } from "../idb.js";
import { STAGE_ORDER, resumeAll, runPipeline } from "../pipeline.js";

async function freshStore() {
  globalThis.indexedDB = new IDBFactory();
  return MeetingStore.open(globalThis.indexedDB);
}

async function seedMeeting(store, { runId = "r", bytes = 8000, ...rest } = {}) {
  await store.createMeeting({ runId, title: "Quarterly sync", startedAt: "2026-08-01T10:00:00Z", ...rest });
  await store.updateMeeting(runId, { endedAt: "2026-08-01T10:45:00Z" });
  if (bytes > 0) await store.appendAudioPart(runId, 0, new Blob([new Uint8Array(bytes)], { type: "audio/webm" }));
  return runId;
}

/** Stub Gemini client with per-operation counters and a "the offscreen doc
 *  died right as this step began" hook (fires BEFORE the step does its work,
 *  so the previous stage is fully committed). */
class FakeClient {
  constructor() {
    this.counts = { uploadFile: 0, getFile: 0, deleteFile: 0, transcribe: 0, extractCore: 0, extractVerify: 0, extractContext: 0, mom: 0, analysis: 0, refine: 0 };
    this.failAt = null;
  }
  _die(tag) {
    if (this.failAt === tag) {
      this.failAt = null;
      throw new Error(`simulated offscreen-doc death at ${tag}`);
    }
  }
  async uploadFile() {
    this._die("uploading");
    this.counts.uploadFile++;
    return { uri: "https://g/files/f1", name: "files/f1", mimeType: "audio/webm" };
  }
  async getFile(name) {
    this.counts.getFile++;
    return { name, uri: "https://g/files/f1", mimeType: "audio/webm", state: "ACTIVE" };
  }
  async deleteFile() {
    this.counts.deleteFile++;
  }
  async generateJson(sys, _schema, content, _maxTok) {
    const isParts = Array.isArray(content);
    if (isParts && content.some((p) => p.fileData)) {
      this._die("transcribing");
      this.counts.transcribe++;
      // >200 words, so the short-transcript fast path does NOT kick in — tests
      // that want the fast path set `this.shortTranscript`.
      const sentence =
        "Welcome everyone, in this quarterly planning session we will review pricing, the rollout schedule, staffing needs, and the open questions raised last week about integration with the existing tools and the reporting requirements that the finance team asked for. ";
      const long = sentence.repeat(8); // ~340 words
      return {
        segments: this.shortTranscript
          ? [{ start_seconds: 0, end_seconds: 3, speaker: "Speaker 1", text: "Hi, can you hear me? Ok bye." }]
          : [
              { start_seconds: 0, end_seconds: 12, speaker: "Speaker 1", text: long },
              { start_seconds: 13, end_seconds: 18, speaker: "Speaker 2", text: "Glad to be here, thanks for setting this up." },
            ],
        attendees: ["Speaker 1", "Speaker 2"],
      };
    }
    const s = String(sys || "");
    const draft = typeof content === "string" && content.startsWith("You produced the DRAFT");
    if (s.includes("extraction layer")) {
      this._die("extracting");
      this.counts.extractCore++;
      return { ...emptyMeetingFacts(), topics_discussed: ["Intro", "Pricing"], decisions: [{ decision: "Proceed" }] };
    }
    if (s.includes("QA reviewer")) {
      this.counts.extractVerify++;
      return { ...emptyMeetingFacts(), topics_discussed: ["Intro", "Pricing"], unsupported_items: [] };
    }
    if (s.includes("context extractor")) {
      this.counts.extractContext++;
      return { meeting_purpose: "Kickoff", goals: [{ statement: "Grow" }], current_state: [] };
    }
    // combined docs: one call → { mom, meeting_analysis }
    if (s.includes("Produce BOTH client-facing documents")) {
      if (draft || (typeof content === "string" && content.startsWith("You produced the DRAFT"))) {
        this.counts.refine++;
        return {
          mom: { title: "MOM", markdown_body: "## Meeting Overview\n" + "refined ".repeat(80) },
          meeting_analysis: { title: "Analysis", markdown_body: "## Executive Snapshot\n" + "refined ".repeat(80) },
        };
      }
      this._die("mom");
      this.counts.mom++;
      this.counts.analysis++;
      return {
        mom: { title: "MOM", markdown_body: "## Meeting Overview\n" + "draft ".repeat(80) },
        meeting_analysis: { title: "Analysis", markdown_body: "## Executive Snapshot\n" + "draft ".repeat(80) },
      };
    }
    throw new Error("unrouted generateJson, sys=" + s.slice(0, 60));
  }
}

function ctxFor(store, client, over = {}) {
  const sent = [];
  return {
    ctx: { store, makeClient: () => client, qualityMode: false, send: (type, p) => sent.push({ type, ...p }), logger: () => {}, ...over },
    sent,
  };
}

// --------------------------------------------------------------------- happy path

test("runPipeline: full run produces both documents and marks the meeting ready", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  const client = new FakeClient();
  const { ctx, sent } = ctxFor(store, client);

  const out = await runPipeline("r", ctx);
  assert.equal(out.stage, "done");

  const m = await store.getMeeting("r");
  assert.equal(m.status, "ready");
  assert.equal(m.stage, "done");
  assert.ok(m.facts);
  assert.equal(m.geminiFileName, "files/f1");

  assert.equal((await store.getDocument("r", "mom")).markdown.length > 0, true);
  assert.equal((await store.getDocument("r", "meeting_analysis")).markdown.length > 0, true);
  assert.ok((await store.getTranscript("r")).plainText.includes("Welcome everyone"));

  // audio parts + Gemini file cleaned up at `done`
  assert.equal(await store.countAudioParts("r"), 0);
  assert.equal(client.counts.deleteFile, 1);

  assert.equal(client.counts.uploadFile, 1);
  assert.equal(client.counts.transcribe, 1);
  assert.equal(client.counts.mom, 1);
  assert.equal(client.counts.analysis, 1);

  assert.deepEqual(sent.filter((e) => e.type === "PROCESSING_DONE"), [{ type: "PROCESSING_DONE", runId: "r" }]);
  assert.ok(sent.some((e) => e.type === "PROCESSING_PROGRESS" && e.stage === "transcribing"));
});

test("runPipeline: quality mode adds the verify + refine passes", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  const client = new FakeClient();
  const { ctx } = ctxFor(store, client, { qualityMode: true });
  await runPipeline("r", ctx);
  assert.equal(client.counts.extractVerify, 1);
  assert.equal(client.counts.refine, 1); // one combined refine (both docs at once)
});

// --------------------------------------------------------------------- G4

test("G4: resumes from every stage without repeating a completed step", async () => {
  for (const failAt of ["uploading", "transcribing", "extracting", "mom"]) {
    const store = await freshStore();
    await seedMeeting(store);
    const client = new FakeClient();
    const { ctx } = ctxFor(store, client);

    client.failAt = failAt;
    await assert.rejects(() => runPipeline("r", ctx), /simulated offscreen-doc death/, `first run should crash at ${failAt}`);

    const midRow = await store.getMeeting("r");
    assert.equal(midRow.status, "failed", `${failAt}: row marked failed`);

    // The offscreen doc is recreated — resume with a fresh mem (same store + client counters).
    const resumed = await runPipeline("r", ctx);
    assert.equal(resumed.stage, "done", `${failAt}: resume completes`);

    assert.equal(client.counts.uploadFile, 1, `${failAt}: uploadFile ran exactly once`);
    assert.equal(client.counts.transcribe, 1, `${failAt}: transcribe ran exactly once`);
    assert.equal(client.counts.extractCore, 1, `${failAt}: core extract ran exactly once`);
    assert.equal(client.counts.mom, 1, `${failAt}: MOM generated exactly once`);
    assert.equal(client.counts.analysis, 1, `${failAt}: Analysis generated exactly once`);

    const m = await store.getMeeting("r");
    assert.equal(m.status, "ready", `${failAt}: ready after resume`);
    assert.ok(await store.getDocument("r", "mom"), `${failAt}: MOM stored`);
    assert.ok(await store.getDocument("r", "meeting_analysis"), `${failAt}: Analysis stored`);
    assert.equal(await store.countAudioParts("r"), 0, `${failAt}: parts cleaned`);
  }
});

test("G4: an already-uploaded file is reused (getFile ACTIVE) instead of re-uploaded", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  const client = new FakeClient();
  const { ctx } = ctxFor(store, client);

  client.failAt = "transcribing"; // crash right after uploading committed
  await assert.rejects(() => runPipeline("r", ctx));
  await runPipeline("r", ctx);

  assert.equal(client.counts.uploadFile, 1);
  assert.equal(client.counts.getFile >= 1, true); // checked the existing file on resume
});

test("G4: an in-flight step (crash mid-upload) is re-run, not lost", async () => {
  // Model a crash DURING uploadFile by having it throw after incrementing.
  const store = await freshStore();
  await seedMeeting(store);
  const client = new FakeClient();
  const realUpload = client.uploadFile.bind(client);
  let first = true;
  client.uploadFile = async (...a) => {
    if (first) {
      first = false;
      client.counts.uploadFile++;
      throw new Error("network died mid-upload");
    }
    return realUpload(...a);
  };
  const { ctx } = ctxFor(store, client);
  await assert.rejects(() => runPipeline("r", ctx));
  await runPipeline("r", ctx);
  assert.equal(client.counts.uploadFile, 2); // re-run — acceptable (plan risk #3)
  assert.equal((await store.getMeeting("r")).status, "ready");
});

// --------------------------------------------------------------------- no speech

test("runPipeline: a no-speech meeting produces placeholder docs and no Gemini audio calls", async () => {
  const store = await freshStore();
  await seedMeeting(store, { bytes: 200 }); // below MIN_AUDIO_BYTES
  const client = new FakeClient();
  const { ctx } = ctxFor(store, client);

  await runPipeline("r", ctx);

  assert.equal(client.counts.uploadFile, 0);
  assert.equal(client.counts.transcribe, 0);
  assert.equal(client.counts.mom, 0);
  const mom = await store.getDocument("r", "mom");
  assert.match(mom.markdown, /No speech was captured/);
  assert.equal((await store.getMeeting("r")).status, "ready");
});

test("runPipeline: an accidental sub-5-second recording is treated as too short — no Gemini calls", async () => {
  const store = await freshStore();
  await seedMeeting(store, { bytes: 40000 }); // plenty of bytes...
  await store.updateMeeting("r", { startedAt: "2026-08-01T10:00:00Z", endedAt: "2026-08-01T10:00:03Z" }); // ...but 3s long
  const client = new FakeClient();
  const { ctx } = ctxFor(store, client);

  await runPipeline("r", ctx);

  assert.equal(client.counts.uploadFile, 0);
  assert.equal(client.counts.transcribe, 0);
  assert.equal(client.counts.mom + client.counts.analysis, 0);
  assert.match((await store.getDocument("r", "mom")).markdown, /too short/);
  assert.equal((await store.getMeeting("r")).status, "ready");
  assert.equal(await store.countAudioParts("r"), 0); // still cleaned up at done
});

test("runPipeline: a real 15-second meeting runs the full pipeline (not skipped)", async () => {
  const store = await freshStore();
  await seedMeeting(store, { bytes: 40000 });
  await store.updateMeeting("r", { startedAt: "2026-08-01T10:00:00Z", endedAt: "2026-08-01T10:00:15Z" });
  const client = new FakeClient();
  client.shortTranscript = true;
  const { ctx } = ctxFor(store, client);

  await runPipeline("r", ctx);

  assert.equal(client.counts.transcribe, 1);
  assert.equal(client.counts.mom, 1);
  assert.equal((await store.getMeeting("r")).status, "ready");
});

// --------------------------------------------------------------------- failure surfacing

test("runPipeline: a Gemini quota error parks the row as 'stopped' (not failed) with a resume hint", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  const client = new FakeClient();
  const realGen = client.generateJson.bind(client);
  client.generateJson = async (sys, schema, content, tok) => {
    if (String(sys || "").includes("Produce BOTH client-facing documents")) {
      const e = new Error("Gemini generateContent failed (HTTP 429): You exceeded your current quota");
      e.quotaExceeded = true;
      e.status = 429;
      throw e;
    }
    return realGen(sys, schema, content, tok);
  };
  const { ctx, sent } = ctxFor(store, client);

  await assert.rejects(() => runPipeline("r", ctx), /quota/);
  const m = await store.getMeeting("r");
  assert.equal(m.status, "stopped");
  assert.equal(m.stage, "generating");
  assert.match(m.error, /free-tier limit/i);
  assert.match(m.error, /Resume/);
  assert.equal(sent.filter((e) => e.type === "PROCESSING_STOPPED").length, 1);
  assert.equal(sent.filter((e) => e.type === "PROCESSING_FAILED").length, 0);
  // transcript + facts were checkpointed before the quota hit
  assert.ok((await store.getTranscript("r")).plainText.length > 0);
  assert.ok(m.facts);
});

test("runPipeline: an unrecoverable error marks the row failed and sends PROCESSING_FAILED", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  const client = new FakeClient();
  client.generateJson = async () => {
    throw new Error("Gemini 400: bad request");
  };
  const { ctx, sent } = ctxFor(store, client);

  await assert.rejects(() => runPipeline("r", ctx), /Gemini 400/);
  const m = await store.getMeeting("r");
  assert.equal(m.status, "failed");
  assert.equal(m.stage, "transcribing");
  assert.match(m.error, /Gemini 400/);
  assert.equal(sent.filter((e) => e.type === "PROCESSING_FAILED").length, 1);
});

test("runPipeline: a user-stopped meeting is not auto-run", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  await store.updateMeeting("r", { status: "stopped", stage: "transcribing" });
  const client = new FakeClient();
  const { ctx } = ctxFor(store, client);
  const out = await runPipeline("r", ctx);
  assert.equal(out.stopped, true);
  assert.equal(client.counts.transcribe, 0);
  assert.equal((await store.getMeeting("r")).status, "stopped");
});

test("runPipeline: an already-ready meeting returns immediately with no calls", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  await store.updateMeeting("r", { status: "ready", stage: "done" });
  const client = new FakeClient();
  const { ctx } = ctxFor(store, client);
  const out = await runPipeline("r", ctx);
  assert.equal(out.alreadyDone, true);
  assert.equal(client.counts.transcribe, 0);
});

// --------------------------------------------------------------------- resumeAll

test("resumeAll: picks up processing + orphaned-recording rows, skips ready ones", async () => {
  const store = await freshStore();
  await seedMeeting(store, { runId: "proc" });
  await store.updateMeeting("proc", { status: "processing", stage: "extracting" });
  await store.putTranscript("proc", { segments: [{ start: 0, end: 1, speaker: "A", text: "hi" }], attendees: ["A"], plainText: "Quarterly sync\n\n[00:00:00] A: hi" });
  await store.updateMeeting("proc", { geminiFileName: "files/f1", geminiFileUri: "https://g/files/f1", noSpeech: false });

  await seedMeeting(store, { runId: "orphan" }); // still "recording"
  await seedMeeting(store, { runId: "done1" });
  await store.updateMeeting("done1", { status: "ready", stage: "done" });

  const client = new FakeClient();
  const { ctx } = ctxFor(store, client);
  const outcomes = await resumeAll(ctx);

  const ran = outcomes.map((o) => o.runId).sort();
  assert.deepEqual(ran, ["orphan", "proc"]);
  assert.equal((await store.getMeeting("proc")).status, "ready");
  assert.equal((await store.getMeeting("orphan")).status, "ready");
  // 'proc' resumed at extracting → no re-transcribe
  assert.equal(client.counts.transcribe, 1); // only the orphan needed transcription
});

// --------------------------------------------------------------------- deadline / fast path

test("runPipeline: a stage that overruns the budget fails the row (not a forever-hang)", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  const client = new FakeClient();
  const slowTranscribe = client.generateJson.bind(client);
  client.generateJson = async (sys, schema, content, tok) => {
    if (Array.isArray(content) && content.some((p) => p.fileData)) {
      await new Promise((r) => setTimeout(r, 400)); // longer than the tiny budget below
    }
    return slowTranscribe(sys, schema, content, tok);
  };
  const { ctx, sent } = ctxFor(store, client, { pipelineMaxMs: 120 });

  await assert.rejects(() => runPipeline("r", ctx), /timed out/);
  const m = await store.getMeeting("r");
  assert.equal(m.status, "failed");
  assert.match(m.error, /timed out/);
  assert.equal(sent.filter((e) => e.type === "PROCESSING_FAILED").length, 1);
});

test("runPipeline: a trivially short transcript skips the verify + refine passes even in quality mode", async () => {
  const store = await freshStore();
  await seedMeeting(store);
  const client = new FakeClient();
  client.shortTranscript = true; // "Hi, can you hear me? Ok bye."
  const { ctx } = ctxFor(store, client, { qualityMode: true });

  await runPipeline("r", ctx);

  assert.equal(client.counts.transcribe, 1);
  assert.equal(client.counts.extractCore, 1);
  assert.equal(client.counts.extractVerify, 0, "verify pass skipped for a tiny transcript");
  assert.equal(client.counts.refine, 0, "refine pass skipped for a tiny transcript");
  assert.equal(client.counts.mom, 1);
  assert.equal(client.counts.analysis, 1);
  assert.equal((await store.getMeeting("r")).status, "ready");
});

test("STAGE_ORDER is the documented sequence", () => {
  assert.deepEqual(STAGE_ORDER, ["assembling", "uploading", "transcribing", "extracting", "generating", "done"]);
});
