import test from "node:test";
import assert from "node:assert/strict";
import { IDBFactory } from "fake-indexeddb";
import "fake-indexeddb/auto";

import { MeetingStore } from "../idb.js";
import { MeetingRecorder } from "../recorder.js";

async function freshStore() {
  globalThis.indexedDB = new IDBFactory();
  const store = await MeetingStore.open(globalThis.indexedDB);
  await store.createMeeting({ runId: "r", title: "R" });
  return store;
}

/** Minimal MediaRecorder stand-in. Each requestData() emits a fixed-size part;
 *  stop() emits one final part then fires onstop. */
class FakeMediaRecorder {
  constructor(stream, options) {
    this.stream = stream;
    this.options = options;
    this.state = "inactive";
    this.partBytes = 4096;
    this.emitted = 0;
  }
  start() {
    this.state = "recording";
  }
  requestData() {
    if (this.state !== "recording") return;
    this.emitted++;
    this.ondataavailable?.({ data: new Blob([new Uint8Array(this.partBytes)], { type: "audio/webm" }) });
  }
  stop() {
    if (this.state === "inactive") {
      this.onstop?.({});
      return;
    }
    this.state = "inactive";
    // header/tail part emitted on stop
    this.ondataavailable?.({ data: new Blob([new Uint8Array(this.partBytes)], { type: "audio/webm" }) });
    this.onstop?.({});
  }
}

function makeRecorder(store, overrides = {}) {
  let tick = null;
  const recorder = new MeetingRecorder({
    stream: {},
    runId: "r",
    store,
    flushMs: 1000,
    mediaRecorderFactory: (s, o) => {
      const mr = new FakeMediaRecorder(s, o);
      recorder._fake = mr;
      return mr;
    },
    setIntervalImpl: (fn) => {
      tick = fn;
      return 1;
    },
    clearIntervalImpl: () => {
      tick = null;
    },
    ...overrides,
  });
  recorder.flushTick = () => tick && tick();
  recorder.timerCleared = () => tick === null;
  return recorder;
}

test("start() creates one MediaRecorder with the requested mime and arms the timer", async () => {
  const store = await freshStore();
  const rec = makeRecorder(store);
  rec.start();
  assert.equal(rec._fake.options.mimeType, "audio/webm;codecs=opus");
  assert.equal(rec.state, "recording");
  assert.equal(rec.timerCleared(), false);
  assert.throws(() => rec.start(), /already started/);
  await rec.stop();
});

test("each flush tick persists one part with an incrementing seq", async () => {
  const store = await freshStore();
  const rec = makeRecorder(store);
  rec.start();
  rec.flushTick();
  rec.flushTick();
  rec.flushTick();
  await rec.stop(); // + one final part

  const parts = await store.getAudioParts("r");
  assert.deepEqual(parts.map((p) => p.seq), [0, 1, 2, 3]);
  assert.equal(rec.seq, 4);
  assert.equal(rec.bytes, 4 * 4096);
});

test("parts assemble into one blob of the summed size, in order", async () => {
  const store = await freshStore();
  const rec = makeRecorder(store);
  rec.start();
  for (let i = 0; i < 5; i++) rec.flushTick();
  const { bytes } = await rec.stop();

  const assembled = await store.assembleAudioBlob("r");
  assert.equal(assembled.size, bytes);
  assert.equal(assembled.size, 6 * 4096); // 5 flushes + 1 on stop
});

test("stop() clears the timer, waits for the final part, and is idempotent", async () => {
  const store = await freshStore();
  const rec = makeRecorder(store);
  rec.start();
  rec.flushTick();
  const first = await rec.stop();
  assert.equal(rec.timerCleared(), true);
  assert.equal(first.seq, 2);
  const second = await rec.stop();
  assert.deepEqual(second, first);
  assert.equal((await store.getAudioParts("r")).length, 2);
});

test("a flush after stop() does nothing (recorder inactive)", async () => {
  const store = await freshStore();
  const rec = makeRecorder(store);
  rec.start();
  await rec.stop();
  rec.flushTick(); // stale timer callback — recorder is inactive
  assert.equal((await store.getAudioParts("r")).length, 1);
});

test("an IndexedDB append failure is routed to onError without breaking later writes", async () => {
  const store = await freshStore();
  const errors = [];
  let calls = 0;
  const flakyStore = {
    appendAudioPart: (runId, seq, blob) => {
      calls++;
      if (seq === 1) return Promise.reject(new Error("quota"));
      return store.appendAudioPart(runId, seq, blob);
    },
  };
  const rec = makeRecorder(flakyStore, { onError: (e, ctx) => errors.push({ e: e.message, ...ctx }) });
  rec.start();
  rec.flushTick(); // seq 0 ok
  rec.flushTick(); // seq 1 fails
  rec.flushTick(); // seq 2 ok
  await rec.stop(); // seq 3 ok

  assert.equal(calls, 4);
  assert.deepEqual(errors, [{ e: "quota", seq: 1 }]);
  assert.deepEqual((await store.getAudioParts("r")).map((p) => p.seq), [0, 2, 3]);
});

test("writes stay ordered even when appends resolve out of order", async () => {
  const store = await freshStore();
  const order = [];
  const slowStore = {
    appendAudioPart: (runId, seq) =>
      new Promise((resolve) => setTimeout(() => {
        order.push(seq);
        resolve();
      }, seq === 0 ? 20 : 1)), // seq 0 resolves slowest
  };
  const rec = makeRecorder(slowStore);
  rec.start();
  rec.flushTick();
  rec.flushTick();
  rec.flushTick();
  await rec.stop();
  assert.deepEqual(order, [0, 1, 2, 3]); // serialised despite the delay on seq 0
});
