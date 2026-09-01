/**
 * MeetingRecorder — ONE continuous MediaRecorder for the whole meeting, with a
 * requestData() flush timer that persists each emitted blob-part to IndexedDB
 * in order. Replaces the old stop/start-every-50s chunking (which existed only
 * to make each chunk independently uploadable to the streaming server).
 *
 * Parts from a single MediaRecorder session concatenate into one valid WebM —
 * only the first part carries the container header (MDN; see the old
 * offscreen.js C14 note). So losing the doc mid-meeting only costs the audio
 * since the last flush, not the whole recording.
 *
 * All external effects are injected so this is unit-testable without a browser:
 *   - mediaRecorderFactory(stream, options) -> a MediaRecorder-like object
 *   - store.appendAudioPart(runId, seq, blob)
 *   - setIntervalImpl / clearIntervalImpl
 */

export const DEFAULT_FLUSH_MS = 20000;
export const DEFAULT_MIME = "audio/webm;codecs=opus";

export class MeetingRecorder {
  constructor({
    stream,
    runId,
    store,
    mimeType = DEFAULT_MIME,
    flushMs = DEFAULT_FLUSH_MS,
    mediaRecorderFactory = (s, o) => new MediaRecorder(s, o),
    setIntervalImpl = (fn, ms) => setInterval(fn, ms),
    clearIntervalImpl = (id) => clearInterval(id),
    onError = () => {},
  }) {
    if (!runId) throw new Error("MeetingRecorder requires a runId");
    if (!store) throw new Error("MeetingRecorder requires a store");
    this.stream = stream;
    this.runId = runId;
    this.store = store;
    this.mimeType = mimeType;
    this.flushMs = flushMs;
    this._factory = mediaRecorderFactory;
    this._setInterval = setIntervalImpl;
    this._clearInterval = clearIntervalImpl;
    this._onError = onError;

    this._recorder = null;
    this._timer = null;
    this._seq = 0;
    this._bytes = 0;
    // Serialise IndexedDB writes so parts always land in seq order even if
    // dataavailable events arrive back-to-back (a flush + the final stop).
    this._writeChain = Promise.resolve();
    this._stopped = false;
  }

  get seq() {
    return this._seq;
  }

  get bytes() {
    return this._bytes;
  }

  get state() {
    return this._recorder ? this._recorder.state : "inactive";
  }

  _persist(blob) {
    const seq = this._seq++;
    this._bytes += blob.size;
    this._writeChain = this._writeChain
      .then(() => this.store.appendAudioPart(this.runId, seq, blob))
      .catch((err) => this._onError(err, { seq }));
    return this._writeChain;
  }

  /** Create the single MediaRecorder, start it, and arm the flush timer. */
  start() {
    if (this._recorder) throw new Error("MeetingRecorder already started");
    let options = { mimeType: this.mimeType };
    try {
      if (
        typeof MediaRecorder !== "undefined" &&
        typeof MediaRecorder.isTypeSupported === "function" &&
        !MediaRecorder.isTypeSupported(this.mimeType)
      ) {
        options = {}; // let the browser pick a default container
      }
    } catch {
      /* keep the requested mimeType */
    }
    this._recorder = this._factory(this.stream, options);

    this._recorder.ondataavailable = (event) => {
      const data = event && event.data;
      if (data && data.size > 0) this._persist(data);
    };
    this._recorder.onerror = (event) => this._onError((event && event.error) || new Error("MediaRecorder error"), {});

    // No timeslice arg — we drive flushing explicitly with requestData().
    this._recorder.start();
    this._timer = this._setInterval(() => {
      if (this._recorder && this._recorder.state === "recording") {
        try {
          this._recorder.requestData();
        } catch (err) {
          this._onError(err, { during: "requestData" });
        }
      }
    }, this.flushMs);
  }

  /**
   * Stop the recorder, wait for its final dataavailable, and wait for every
   * pending IndexedDB write. Resolves { seq, bytes } once all parts are durable.
   * Idempotent.
   */
  async stop() {
    if (this._stopped) return { seq: this._seq, bytes: this._bytes };
    this._stopped = true;

    if (this._timer !== null) {
      this._clearInterval(this._timer);
      this._timer = null;
    }

    const rec = this._recorder;
    if (rec && rec.state !== "inactive") {
      await new Promise((resolve) => {
        const prev = rec.onstop;
        rec.onstop = (e) => {
          if (typeof prev === "function") {
            try {
              prev(e);
            } catch {
              /* ignore */
            }
          }
          resolve();
        };
        try {
          rec.stop();
        } catch {
          resolve();
        }
      });
    }

    await this._writeChain;
    return { seq: this._seq, bytes: this._bytes };
  }
}
