/**
 * IndexedDB wrapper for the standalone extension — the only durable store.
 * Holds meetings, their audio parts (durable during capture), transcripts, and
 * generated documents. `chrome.storage.local` keeps only the API key + small
 * settings + a debug ring buffer; nothing here ever leaves the machine.
 *
 * Data model (DB "meeting-saathi", v1):
 *   meetings    keyPath runId          — { runId, title, startedAt, endedAt,
 *                                          status, stage, error, geminiFileUri,
 *                                          facts, speakerEvents[], attendeeRoster[],
 *                                          model, audioMimeType, updatedAt }
 *   audioParts  keyPath [runId, seq]   — { runId, seq, blob }
 *   transcripts keyPath runId          — { runId, segments[], attendees[], plainText, excerpts }
 *   documents   keyPath [runId, docKey]— { runId, docKey, title, markdown, generatedAt }
 */

export const DB_NAME = "meeting-saathi";
export const DB_VERSION = 1;

export const MEETING_STATUS = Object.freeze({
  RECORDING: "recording",
  PROCESSING: "processing",
  READY: "ready",
  FAILED: "failed",
  STOPPED: "stopped", // user hit Stop mid-processing — never auto-resumed, only via the Resume button
});

/** Wrap an IDBRequest as a promise. */
function req(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export function openDb(indexedDBImpl = globalThis.indexedDB) {
  if (!indexedDBImpl) throw new Error("IndexedDB is not available in this context");
  return new Promise((resolve, reject) => {
    const open = indexedDBImpl.open(DB_NAME, DB_VERSION);
    open.onupgradeneeded = () => {
      const db = open.result;
      if (!db.objectStoreNames.contains("meetings")) {
        const s = db.createObjectStore("meetings", { keyPath: "runId" });
        s.createIndex("startedAt", "startedAt");
        s.createIndex("status", "status");
      }
      if (!db.objectStoreNames.contains("audioParts")) {
        db.createObjectStore("audioParts", { keyPath: ["runId", "seq"] });
      }
      if (!db.objectStoreNames.contains("transcripts")) {
        db.createObjectStore("transcripts", { keyPath: "runId" });
      }
      if (!db.objectStoreNames.contains("documents")) {
        db.createObjectStore("documents", { keyPath: ["runId", "docKey"] });
      }
    };
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(open.error);
    open.onblocked = () => reject(new Error("IndexedDB open blocked by another connection"));
  });
}

export class MeetingStore {
  constructor(db) {
    this.db = db;
  }

  static async open(indexedDBImpl) {
    return new MeetingStore(await openDb(indexedDBImpl));
  }

  close() {
    this.db.close();
  }

  /**
   * Run `fn(store)` inside one transaction and resolve with its return value
   * only after the transaction commits (so writes are durable before we act).
   */
  _tx(storeNames, mode, fn) {
    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(storeNames, mode);
      let result;
      let failed = false;
      tx.oncomplete = () => (failed ? undefined : resolve(result));
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
      const stores = Array.isArray(storeNames)
        ? Object.fromEntries(storeNames.map((n) => [n, tx.objectStore(n)]))
        : tx.objectStore(storeNames);
      Promise.resolve()
        .then(() => fn(stores))
        .then((r) => {
          result = r;
        })
        .catch((err) => {
          failed = true;
          try {
            tx.abort();
          } catch {
            /* already done */
          }
          reject(err);
        });
    });
  }

  // ---------------------------------------------------------------- meetings

  async createMeeting({ runId, title, startedAt, model = null, speakerEvents = [], attendeeRoster = [], audioMimeType = "audio/webm" }) {
    const now = new Date().toISOString();
    const row = {
      runId,
      title: title || "Google Meet",
      startedAt: startedAt || now,
      endedAt: null,
      status: MEETING_STATUS.RECORDING,
      stage: null,
      error: null,
      geminiFileUri: null,
      facts: null,
      speakerEvents,
      attendeeRoster,
      model,
      audioMimeType,
      createdAt: now,
      updatedAt: now,
    };
    await this._tx("meetings", "readwrite", (s) => req(s.add(row)));
    return row;
  }

  getMeeting(runId) {
    return this._tx("meetings", "readonly", (s) => req(s.get(runId)));
  }

  /** Read-modify-write a meeting row within one transaction. */
  updateMeeting(runId, patch) {
    return this._tx("meetings", "readwrite", async (s) => {
      const existing = await req(s.get(runId));
      if (!existing) throw new Error(`meeting ${runId} not found`);
      const next = { ...existing, ...patch, runId, updatedAt: new Date().toISOString() };
      await req(s.put(next));
      return next;
    });
  }

  /** All meetings, newest first (by startedAt). */
  listMeetings() {
    return this._tx("meetings", "readonly", async (s) => {
      const all = await req(s.getAll());
      return all.sort((a, b) => String(b.startedAt).localeCompare(String(a.startedAt)));
    });
  }

  /** Meetings still mid-flight — used by the offscreen doc to resume on (re)creation. */
  async listResumable() {
    const all = await this.listMeetings();
    return all.filter((m) => m.status === MEETING_STATUS.RECORDING || m.status === MEETING_STATUS.PROCESSING);
  }

  /** Deletes a meeting and everything under it. */
  deleteMeeting(runId) {
    return this._tx(["meetings", "audioParts", "transcripts", "documents"], "readwrite", async (stores) => {
      await req(stores.meetings.delete(runId));
      await req(stores.transcripts.delete(runId));
      await this._deleteRange(stores.audioParts, runId);
      await this._deleteRange(stores.documents, runId);
    });
  }

  /**
   * Delete every [runId, *] compound-key record in `store`, regardless of the
   * second component's type. Lower bound `[runId]` sorts before any 2-element
   * key with that prefix; upper bound `[runId, []]` (an array sorts after every
   * number/date/string/binary in IndexedDB key ordering), exclusive.
   */
  async _deleteRange(store, runId) {
    const range = IDBKeyRange.bound([runId], [runId, []], false, true);
    const keys = await req(store.getAllKeys(range));
    for (const key of keys) await req(store.delete(key));
  }

  // ------------------------------------------------------------- audio parts

  /**
   * Append one MediaRecorder blob-part. `seq` is a monotonic counter from the
   * recorder — parts from a single continuous session concatenate into a valid
   * WebM (only the first carries the container header).
   */
  appendAudioPart(runId, seq, blob) {
    return this._tx("audioParts", "readwrite", (s) => req(s.put({ runId, seq, blob })));
  }

  getAudioParts(runId) {
    return this._tx("audioParts", "readonly", async (s) => {
      const range = IDBKeyRange.bound([runId], [runId, []], false, true);
      const rows = await req(s.getAll(range));
      return rows.sort((a, b) => a.seq - b.seq);
    });
  }

  async countAudioParts(runId) {
    return (await this.getAudioParts(runId)).length;
  }

  /** Concatenate all parts (in seq order) into one Blob. */
  async assembleAudioBlob(runId, { type } = {}) {
    const parts = await this.getAudioParts(runId);
    if (parts.length === 0) return null;
    const mime = type || parts[0].blob?.type || "audio/webm";
    return new Blob(parts.map((p) => p.blob), { type: mime });
  }

  deleteAudioParts(runId) {
    return this._tx("audioParts", "readwrite", (s) => this._deleteRange(s, runId));
  }

  // ------------------------------------------------------------- transcripts

  putTranscript(runId, { segments = [], attendees = [], plainText = "", excerpts = {} }) {
    return this._tx("transcripts", "readwrite", (s) => req(s.put({ runId, segments, attendees, plainText, excerpts })));
  }

  getTranscript(runId) {
    return this._tx("transcripts", "readonly", (s) => req(s.get(runId)));
  }

  // -------------------------------------------------------------- documents

  putDocument(runId, docKey, { title, markdown, generatedAt = new Date().toISOString() }) {
    return this._tx("documents", "readwrite", (s) => req(s.put({ runId, docKey, title, markdown, generatedAt })));
  }

  getDocument(runId, docKey) {
    return this._tx("documents", "readonly", (s) => req(s.get([runId, docKey])));
  }

  listDocuments(runId) {
    return this._tx("documents", "readonly", async (s) => {
      const range = IDBKeyRange.bound([runId], [runId, []], false, true);
      return req(s.getAll(range));
    });
  }

  /** Drop all generated documents for a run — used by "Regenerate" / rename-speakers. */
  deleteDocuments(runId) {
    return this._tx("documents", "readwrite", (s) => this._deleteRange(s, runId));
  }

  // ------------------------------------------------------------ housekeeping

  /** navigator.storage.estimate() passthrough — { quota, usage } or null. */
  async storageEstimate() {
    try {
      if (globalThis.navigator?.storage?.estimate) return await navigator.storage.estimate();
    } catch {
      /* not available in this context */
    }
    return null;
  }
}
