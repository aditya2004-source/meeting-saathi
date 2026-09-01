/**
 * The processing state machine — runs in the offscreen document after capture.
 *
 *   assembling → uploading → transcribing → extracting → generating → done
 *
 * Resumable: the meeting row's `stage` is the resume point, and every stage
 * re-derives its inputs from IndexedDB (or the Gemini Files API), so if the
 * offscreen document is reclaimed mid-processing, the next run picks up from
 * the last committed stage and never repeats a completed step. A stage that is
 * in flight when the document dies is re-run (idempotent; at worst it costs the
 * user's own quota twice for that one call).
 *
 * All effects are injected via `ctx` so this is unit-testable without a browser:
 *   ctx.store         — a MeetingStore (lib/idb.js)
 *   ctx.makeClient    — (model) => GeminiClient-like (lib/gemini.js)
 *   ctx.send          — (type, payload) => void   (PROCESSING_* messages to the SW)
 *   ctx.qualityMode   — boolean (default true)
 *   ctx.languageMode  — "translate" | "english" | "original" (default "translate")
 *   ctx.logger        — (msg, err?) => void
 */

import { extractMeetingFacts, emptyMeetingFacts } from "./facts.js";
import { generateBothDocuments } from "./generate.js";
import { MEETING_STATUS } from "./idb.js";
import { TRANSCRIBE_RESPONSE_SCHEMA, buildTranscribePrompt } from "./prompts.js";
import { buildTranscript, formatMeetingDate } from "./transcript.js";

export const STAGE_ORDER = ["assembling", "uploading", "transcribing", "extracting", "generating", "done"];
export const DOC_KEYS = ["mom", "meeting_analysis"];

// Below this, an "assembled" recording is treated as no speech (an empty WebM
// container is a few hundred bytes; a few seconds of Opus is a few KB).
const MIN_AUDIO_BYTES = 2048;
// A recording shorter than this is almost always an accidental start/stop (a
// real "ok, bye" standup still runs — the short-transcript fast path keeps it
// cheap). Only these skip Gemini entirely and get a placeholder.
export const MIN_MEETING_SECONDS = 5;
const TRANSCRIBE_MAX_TOKENS = 32768;

const meetingSeconds = (m) =>
  m.endedAt && m.startedAt ? (Date.parse(m.endedAt) - Date.parse(m.startedAt)) / 1000 : 0;

// Under this many words (~1.5 min of speech), the quality-mode verify + refine
// passes have little to improve and just burn requests — skip them so a short
// meeting is ~4 Gemini calls instead of ~8 (matters a lot on the free tier).
// Real multi-minute meetings are well past this and still get full quality mode.
export const SHORT_TRANSCRIPT_WORDS = 200;
const wordCount = (s) => (s ? s.trim().split(/\s+/).filter(Boolean).length : 0);

// Backstop: no meeting should process for longer than this. Every network call
// is already bounded by GeminiClient's per-request timeout; this catches a
// pathological chain of retries or any non-fetch hang.
export const PIPELINE_MAX_MS = 20 * 60 * 1000;

// ---------------------------------------------------------------------------
// Stages — each: async (runId, meeting, client, store, mem, ctx) => void
// ---------------------------------------------------------------------------

/**
 * Return a currently-ACTIVE Gemini file URI for this meeting's audio, uploading
 * (or re-uploading an expired one) only if needed. Shared by the `uploading`
 * stage and by `transcribing` on a resume that skipped `uploading`.
 */
async function ensureUploadedFile(runId, meeting, client, store, mem) {
  if (mem.fileUri) return { uri: mem.fileUri, mimeType: mem.fileMimeType };

  if (meeting.geminiFileName) {
    try {
      const file = await client.getFile(meeting.geminiFileName);
      if (file && file.state === "ACTIVE" && file.uri) {
        mem.fileUri = file.uri;
        mem.fileMimeType = file.mimeType || meeting.audioMimeType || "audio/webm";
        return { uri: mem.fileUri, mimeType: mem.fileMimeType };
      }
    } catch {
      /* expired / gone — re-upload below */
    }
  }

  const blob = mem.audioBlob || (await store.assembleAudioBlob(runId, { type: meeting.audioMimeType || "audio/webm" }));
  if (!blob) throw new Error("ensureUploadedFile: no audio to upload");
  const uploaded = await client.uploadFile(blob, { displayName: `meeting-${runId}` });
  mem.fileUri = uploaded.uri;
  mem.fileMimeType = uploaded.mimeType;
  await store.updateMeeting(runId, {
    geminiFileUri: uploaded.uri,
    geminiFileName: uploaded.name,
    audioMimeType: uploaded.mimeType,
  });
  return { uri: mem.fileUri, mimeType: mem.fileMimeType };
}

const STAGES = {
  async assembling(runId, meeting, client, store, mem) {
    mem.audioBlob = await store.assembleAudioBlob(runId, { type: meeting.audioMimeType || "audio/webm" });
    const secs = meetingSeconds(meeting);
    const tooShort = secs > 0 && secs < MIN_MEETING_SECONDS;
    const noSpeech = !mem.audioBlob || mem.audioBlob.size < MIN_AUDIO_BYTES || tooShort;
    mem.ctx.logger?.(
      `assembling: ${runId} bytes=${mem.audioBlob ? mem.audioBlob.size : 0} secs=${Math.round(secs)} noSpeech=${noSpeech} tooShort=${tooShort}`,
    );
    // Persist — downstream stages must know this after a resume, when `mem` is empty.
    await store.updateMeeting(runId, { noSpeech, tooShort });
    mem.noSpeech = noSpeech;
  },

  async uploading(runId, meeting, client, store, mem) {
    if (meeting.noSpeech) return;
    await ensureUploadedFile(runId, meeting, client, store, mem);
  },

  async transcribing(runId, meeting, client, store, mem) {
    if (meeting.noSpeech) {
      await store.putTranscript(runId, { segments: [], attendees: [], plainText: "", excerpts: {} });
      mem.transcript = { segments: [], attendees: [], plainText: "" };
      return;
    }

    const existing = await store.getTranscript(runId);
    if (existing && typeof existing.plainText === "string" && existing.plainText.length > 0) {
      mem.transcript = existing;
      return;
    }

    // Resume-safe: if `uploading` didn't run this session, this verifies the
    // file is still ACTIVE (re-uploading only if it expired).
    const { uri: fileUri, mimeType } = await ensureUploadedFile(runId, meeting, client, store, mem);

    const prompt = buildTranscribePrompt({
      speakerEvents: meeting.speakerEvents || [],
      roster: meeting.attendeeRoster || [],
      languageMode: mem.ctx.languageMode || "translate",
    });
    const raw = await client.generateJson(
      null,
      TRANSCRIBE_RESPONSE_SCHEMA,
      [{ fileData: { fileUri, mimeType } }, { text: prompt }],
      TRANSCRIBE_MAX_TOKENS,
    );

    const built = buildTranscript(raw, {
      meetingTitle: meeting.title || "Google Meet",
      speakerEvents: meeting.speakerEvents || [],
      roster: meeting.attendeeRoster || [],
    });
    await store.putTranscript(runId, built);
    mem.transcript = built;

    // Soft truncation check (long-audio risk / gate G1): a transcript whose
    // last timestamp is far short of the meeting length, or that is suspiciously
    // tiny, is likely a paraphrase/drop. Logged, not fatal.
    const lastEnd = built.segments.length ? built.segments[built.segments.length - 1].end : 0;
    const meetingSeconds = meeting.endedAt && meeting.startedAt ? (Date.parse(meeting.endedAt) - Date.parse(meeting.startedAt)) / 1000 : 0;
    if (meetingSeconds > 600 && lastEnd > 0 && lastEnd < meetingSeconds * 0.6) {
      mem.ctx.logger?.(`transcribe: last segment ${Math.round(lastEnd)}s vs meeting ~${Math.round(meetingSeconds)}s — possible truncation`);
    }
  },

  async extracting(runId, meeting, client, store, mem) {
    if (meeting.facts) {
      mem.facts = meeting.facts;
      return;
    }
    if (meeting.noSpeech) {
      mem.facts = emptyMeetingFacts();
      await store.updateMeeting(runId, { facts: mem.facts });
      return;
    }
    const transcript = mem.transcript || (await store.getTranscript(runId));
    const short = wordCount(transcript.plainText) < SHORT_TRANSCRIPT_WORDS;
    const facts = await extractMeetingFacts(client, transcript.plainText, {
      qualityMode: mem.ctx.qualityMode !== false && !short,
      logger: mem.ctx.logger,
    });
    await store.updateMeeting(runId, { facts });
    mem.facts = facts;
  },

  async generating(runId, meeting, client, store, mem) {
    // Too short / no speech → placeholder docs, zero Gemini calls.
    if (meeting.noSpeech) {
      const note = meeting.tooShort
        ? `This meeting was under ${MIN_MEETING_SECONDS} seconds — too short to generate minutes or an analysis.`
        : "No speech was captured during this meeting, so there is nothing to summarize.";
      for (const docKey of DOC_KEYS) {
        if (await store.getDocument(runId, docKey)) continue;
        const title = docKey === "mom" ? "Minutes of Meeting" : "Meeting Analysis";
        await store.putDocument(runId, docKey, { title, markdown: `# ${title} — ${meeting.title || "Google Meet"}\n\n${note}\n` });
      }
      return;
    }

    // Resume skips generation entirely if BOTH docs are already stored.
    if ((await store.getDocument(runId, "mom")) && (await store.getDocument(runId, "meeting_analysis"))) return;

    const transcript = mem.transcript || (await store.getTranscript(runId)) || { plainText: "" };
    const facts = mem.facts || meeting.facts || emptyMeetingFacts();
    const meetingDate = formatMeetingDate(meeting.startedAt) || meeting.startedAt || "";
    const short = wordCount(transcript.plainText) < SHORT_TRANSCRIPT_WORDS;

    // ONE call produces both documents (a third of the old cost).
    const docs = await generateBothDocuments(client, {
      meetingTitle: meeting.title || "Google Meet",
      meetingDate,
      attendees: transcript.attendees || [],
      facts,
      transcriptText: transcript.plainText || "",
      qualityMode: mem.ctx.qualityMode !== false && !short,
      logger: mem.ctx.logger,
    });

    for (const docKey of DOC_KEYS) {
      const doc = docs[docKey] || {};
      await store.putDocument(runId, docKey, {
        title: doc.title || (docKey === "mom" ? "Minutes of Meeting" : "Meeting Analysis"),
        markdown: doc.markdown_body || "",
      });
      mem.ctx.send?.("PROCESSING_PROGRESS", { runId, stage: "generating", docKey, pct: 92 });
    }
  },

  async done(runId, meeting, client, store, mem) {
    await store.updateMeeting(runId, { status: MEETING_STATUS.READY, stage: "done", error: null });
    // Courtesy cleanup — the Files API entry auto-expires in 48h anyway, and
    // the audio parts are now fully derived into transcript + documents.
    if (meeting.geminiFileName || meeting.geminiFileUri) {
      await client.deleteFile(meeting.geminiFileName || meeting.geminiFileUri);
    }
    await store.deleteAudioParts(runId);
    mem.ctx.send?.("PROCESSING_DONE", { runId });
  },
};

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

/**
 * Run (or resume) the pipeline for one meeting. Returns when it reaches `done`
 * or throws (after marking the row failed + sending PROCESSING_FAILED).
 */
export async function runPipeline(runId, ctx) {
  const { store, makeClient, send = () => {}, logger = () => {} } = ctx;
  ctx.logger = logger;
  ctx.send = send;

  let meeting = await store.getMeeting(runId);
  if (!meeting) throw new Error(`runPipeline: meeting ${runId} not found`);
  if (meeting.status === MEETING_STATUS.READY) return { runId, stage: "done", alreadyDone: true };
  // The user stopped this one — only the dashboard Resume button restarts it
  // (it flips the row back to `processing` before re-invoking).
  if (meeting.status === MEETING_STATUS.STOPPED) return { runId, stage: meeting.stage, stopped: true };

  const client = makeClient(meeting.model);
  const mem = { ctx };

  const startStage = STAGE_ORDER.includes(meeting.stage) ? meeting.stage : "assembling";
  let stage = startStage;
  const budgetMs = ctx.pipelineMaxMs || PIPELINE_MAX_MS;
  const deadlineAt = Date.now() + budgetMs;

  try {
    logger(`pipeline: ${runId} starting at ${startStage}`);
    for (let i = STAGE_ORDER.indexOf(startStage); i < STAGE_ORDER.length; i++) {
      stage = STAGE_ORDER[i];
      await store.updateMeeting(runId, { status: MEETING_STATUS.PROCESSING, stage, error: null });
      send("PROCESSING_PROGRESS", { runId, stage, pct: Math.round((i / (STAGE_ORDER.length - 1)) * 100) });
      meeting = await store.getMeeting(runId); // pick up what earlier stages committed
      logger(`pipeline: ${runId} → ${stage}`);

      const remaining = deadlineAt - Date.now();
      if (remaining <= 0) throw new Error(`processing timed out after ${Math.round(budgetMs / 60000)} min — Resume from the dashboard`);
      let timer;
      const guard = new Promise((_, rej) => {
        timer = setTimeout(() => rej(new Error(`processing timed out after ${Math.round(budgetMs / 60000)} min — Resume from the dashboard`)), remaining);
      });
      try {
        await Promise.race([STAGES[stage](runId, meeting, client, store, mem), guard]);
      } finally {
        clearTimeout(timer);
      }
    }
    return { runId, stage: "done" };
  } catch (err) {
    const message = String((err && err.message) || err);
    logger(`pipeline failed at ${stage}: ${message}`, err);

    // A Gemini free-tier / billing quota hit isn't a real failure — everything
    // done so far (transcript, facts) is checkpointed. Park it as `stopped`
    // with a clear message; the user hits Resume once the limit resets and it
    // continues from `${stage}` (only the remaining calls run).
    const isQuota = (err && err.quotaExceeded) || /quota|RESOURCE_EXHAUSTED|exceeded your current quota/i.test(message);
    const badKey = (err && err.badKey) || /invalid authentication|API key not valid/i.test(message);
    const status = isQuota ? MEETING_STATUS.STOPPED : MEETING_STATUS.FAILED;
    const savedSoFar = stage === "extracting" || stage === "generating" ? " The transcript is saved, so Resume only re-does the last steps." : "";
    const waitS = err && err.retryAfterMs ? Math.ceil(err.retryAfterMs / 1000) : 60;
    const rowError = isQuota
      ? `Gemini free-tier limit hit at "${stage}".${savedSoFar} Wait ~${waitS}s (or add billing to your key / switch to a -lite model in Settings), then click Resume.`
      : badKey
        ? `Your Gemini API key was rejected at "${stage}". Open Settings, re-check the key (use "Test this key"), then Resume.`
        : message;
    try {
      await store.updateMeeting(runId, { status, stage, error: rowError });
    } catch {
      /* best effort */
    }
    send(isQuota ? "PROCESSING_STOPPED" : "PROCESSING_FAILED", { runId, stage, error: rowError });
    throw err;
  }
}

/**
 * Resume every meeting left mid-flight — called when the offscreen document is
 * (re)created. Recording-status rows whose capture was lost are moved straight
 * into processing from `assembling` (whatever audio parts made it to IndexedDB).
 */
export async function resumeAll(ctx) {
  const { store, logger = () => {} } = ctx;
  const pending = await store.listResumable();
  const outcomes = [];
  for (const m of pending) {
    // A row still marked "recording" means capture died — process what we have.
    const stage = m.status === MEETING_STATUS.RECORDING ? "assembling" : m.stage || "assembling";
    if (m.stage !== stage) await store.updateMeeting(m.runId, { status: MEETING_STATUS.PROCESSING, stage });
    try {
      outcomes.push(await runPipeline(m.runId, ctx));
    } catch (err) {
      logger(`resume of ${m.runId} failed`, err);
      outcomes.push({ runId: m.runId, error: String((err && err.message) || err) });
    }
  }
  return outcomes;
}
