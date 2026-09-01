/**
 * Hand-written Gemini REST client (no SDK, no bundler — MV3 CSP forbids remote
 * code) plus the JSON/markdown repair helpers ported from app/docgen/engine.py.
 *
 * Every call goes to https://generativelanguage.googleapis.com with the user's
 * own API key. Nothing here touches a founder-controlled resource.
 */

const API_ROOT = "https://generativelanguage.googleapis.com";
const MAX_OUTPUT_TOKENS_CAP = 49152;

// ---------------------------------------------------------------------------
// JSON / markdown repair (ports of app/docgen/engine.py)
// ---------------------------------------------------------------------------

const VALID_JSON_ESCAPES = new Set(['"', "\\", "/", "b", "f", "n", "r", "t"]);
const HEX_DIGITS = new Set("0123456789abcdefABCDEF");

/**
 * Port of engine.py::_repair_invalid_backslash_escapes. Doubles any backslash
 * that isn't already part of a valid JSON escape sequence, so a markdown_body
 * that embeds `\*` / a stray `\u` / etc. can still be JSON.parsed.
 */
export function repairInvalidBackslashEscapes(text) {
  const out = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    const ch = text[i];
    if (ch === "\\" && i + 1 < n) {
      const nxt = text[i + 1];
      if (VALID_JSON_ESCAPES.has(nxt)) {
        out.push(text.slice(i, i + 2));
        i += 2;
        continue;
      }
      if (nxt === "u" && i + 6 <= n && [...text.slice(i + 2, i + 6)].every((c) => HEX_DIGITS.has(c))) {
        out.push(text.slice(i, i + 6));
        i += 6;
        continue;
      }
      out.push("\\\\");
      i += 1;
      continue;
    }
    out.push(ch);
    i += 1;
  }
  return out.join("");
}

/** Port of engine.py::_normalize_literal_newlines. */
export function normalizeLiteralNewlines(text) {
  let t = text.replace(/\\{2,}n/g, "\n");
  t = t.replace(/\\{2,}t/g, "\t");
  return t.replaceAll("\\n", "\n").replaceAll("\\t", "\t");
}

const HTML_ENTITIES = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#39;": "'",
  "&apos;": "'",
  "&nbsp;": " ",
};

/** Port of engine.py::_unescape_html_entities. */
export function unescapeHtmlEntities(text) {
  let t = text;
  for (const [entity, char] of Object.entries(HTML_ENTITIES)) {
    t = t.replaceAll(entity, char);
  }
  return t;
}

const CONTROL_TOKEN_RE = /<\|(?:im_end|im_start|endoftext|eot_id)\|>|_dst_id_=/;
const META_TAIL_RE = /\b(use it verbatim|do not insert any other symbols|this is the complete and valid json)\b/i;

/** Port of engine.py::_strip_trailing_model_noise. */
export function stripTrailingModelNoise(text) {
  let t = text;
  const marker = CONTROL_TOKEN_RE.exec(t);
  if (marker) {
    t = t.slice(0, marker.index);
    const lines = t.replace(/\s+$/, "").split("\n");
    while (
      lines.length &&
      (["```", "```json", '"}', "}"].includes(lines[lines.length - 1].trim()) ||
        META_TAIL_RE.test(lines[lines.length - 1]))
    ) {
      lines.pop();
    }
    if (lines.length) {
      lines[lines.length - 1] = lines[lines.length - 1].replace(/"\}\s*$/, "");
    }
    t = lines.join("\n");
  }
  return t.trim() ? t.replace(/\s+$/, "") + "\n" : t;
}

/** Port of engine.py::_clean_markdown_body. */
export function cleanMarkdownBody(text) {
  return stripTrailingModelNoise(unescapeHtmlEntities(normalizeLiteralNewlines(text)));
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class GeminiError extends Error {
  constructor(message, { status = null, stage = null, quotaExceeded = false, retryAfterMs = null, badKey = false } = {}) {
    super(message);
    this.name = "GeminiError";
    this.status = status;
    this.stage = stage;
    this.quotaExceeded = quotaExceeded; // 429 RESOURCE_EXHAUSTED — free-tier / billing limit
    this.retryAfterMs = retryAfterMs; // server-suggested wait, if any
    this.badKey = badKey; // 401/403 — key rejected
  }
}

/** Pull the server's suggested wait out of a 429 (Retry-After header or the
 *  RetryInfo detail's `retryDelay: "40s"`). Returns ms, or null. */
function parseRetryAfterMs(res, body) {
  const header = res.headers.get("retry-after");
  if (header && /^\d+$/.test(header.trim())) return parseInt(header.trim(), 10) * 1000;
  try {
    const parsed = typeof body === "string" ? JSON.parse(body) : body;
    for (const d of parsed?.error?.details || []) {
      const m = /^(\d+(?:\.\d+)?)s$/.exec(d.retryDelay || "");
      if (m) return Math.ceil(parseFloat(m[1]) * 1000);
    }
  } catch {
    /* ignore */
  }
  return null;
}

export class GeminiClient {
  /**
   * @param {object} opts
   * @param {string} opts.apiKey          the user's own Gemini API key
   * @param {string} [opts.model]         e.g. "gemini-3.6-flash"
   * @param {Function} [opts.fetchImpl]   injectable for tests (default: global fetch)
   * @param {number} [opts.maxRetries]    transient-error retries per request
   * @param {Function} [opts.sleepImpl]   injectable backoff sleep (default: setTimeout)
   * @param {Function} [opts.onRetry]     called (attempt, delayMs, status) before a backoff wait
   * @param {number} [opts.requestTimeoutMs]  per-request abort deadline (default 180s)
   * @param {number} [opts.uploadTimeoutMs]   deadline for the resumable-upload PUT (default 600s)
   */
  constructor({ apiKey, model = "gemini-3.6-flash", fetchImpl, maxRetries = 3, sleepImpl, onRetry, requestTimeoutMs = 180000, uploadTimeoutMs = 600000 } = {}) {
    if (!apiKey) throw new GeminiError("Gemini API key is not set. Add it in Settings.");
    this.apiKey = apiKey;
    this.model = model;
    this._fetch = fetchImpl || ((...a) => fetch(...a));
    this.maxRetries = maxRetries;
    this._sleep = sleepImpl || sleep;
    this._onRetry = onRetry || (() => {});
    this.requestTimeoutMs = requestTimeoutMs;
    this.uploadTimeoutMs = uploadTimeoutMs;
  }

  _url(method, { upload = false } = {}) {
    const base = upload ? `${API_ROOT}/upload/v1beta` : `${API_ROOT}/v1beta`;
    return `${base}/${method}?key=${encodeURIComponent(this.apiKey)}`;
  }

  /** True for statuses worth retrying with backoff. */
  static _isTransient(status) {
    return status === 429 || status === 500 || status === 502 || status === 503 || status === 504;
  }

  /** fetch with an AbortController deadline — a stalled connection (or an
   *  endpoint that accepts and never answers) must fail, not hang the pipeline. */
  async _fetchWithTimeout(url, init, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await this._fetch(url, { ...init, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  async _requestWithRetry(url, init, stage, { timeoutMs } = {}) {
    const deadline = timeoutMs ?? this.requestTimeoutMs;
    let lastErr = null;
    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      let res;
      try {
        res = await this._fetchWithTimeout(url, init, deadline);
      } catch (networkErr) {
        const aborted = networkErr && (networkErr.name === "AbortError" || /abort/i.test(networkErr.message || ""));
        lastErr = new GeminiError(
          `${aborted ? `Gemini ${stage} timed out after ${Math.round(deadline / 1000)}s` : `Network error contacting Gemini (${stage}): ${networkErr.message}`}`,
          { stage },
        );
        if (attempt < this.maxRetries) {
          const delay = 2000 * (attempt + 1);
          this._onRetry(attempt + 1, delay, aborted ? "timeout" : null);
          await this._sleep(delay);
          continue;
        }
        throw lastErr;
      }
      if (res.ok) return res;

      const bodyText = await res.text().catch(() => "");
      let detail = bodyText.slice(0, 300);
      try {
        detail = JSON.parse(bodyText)?.error?.message || detail;
      } catch {
        /* keep raw */
      }

      // A quota/RESOURCE_EXHAUSTED 429 will NOT clear in a few seconds — every
      // retry is another request billed against the already-exhausted quota
      // (this is what nuked the free tier). Fail fast; the pipeline parks the
      // meeting so the user resumes later (or adds billing).
      const isQuota = res.status === 429 && /quota|RESOURCE_EXHAUSTED/i.test(bodyText);
      if (isQuota) {
        throw new GeminiError(`Gemini ${stage} failed (HTTP 429): ${detail}`, {
          status: 429,
          stage,
          quotaExceeded: true,
          retryAfterMs: parseRetryAfterMs(res, bodyText),
        });
      }

      // 5xx (and a rare non-quota 429): retry with backoff / the server hint.
      if (GeminiClient._isTransient(res.status) && attempt < this.maxRetries) {
        const serverWaitMs = res.status === 429 ? parseRetryAfterMs(res, bodyText) : null;
        const delay = serverWaitMs != null ? Math.min(serverWaitMs + 500, 65000) : 2000 * (attempt + 1);
        this._onRetry(attempt + 1, delay, res.status);
        await this._sleep(delay);
        continue;
      }

      const hint =
        res.status === 401 || res.status === 403
          ? " — your Gemini API key was rejected. Check it's pasted correctly (no spaces) and that the Generative Language API is enabled for it at aistudio.google.com/apikey."
          : res.status === 404 && /model/i.test(detail)
            ? " — that model id isn't available for your key. Pick another in Settings."
            : "";
      throw new GeminiError(`Gemini ${stage} failed (HTTP ${res.status}): ${detail}${hint}`, {
        status: res.status,
        stage,
        badKey: res.status === 401 || res.status === 403,
      });
    }
    throw lastErr || new GeminiError(`Gemini ${stage} failed`, { stage });
  }

  /**
   * One generateContent call. `parts` is an array of Gemini part objects
   * (e.g. [{ text }] or [{ fileData: { fileUri, mimeType } }, { text }]).
   * Returns { text, finishReason, usage }.
   */
  async generateContent({ systemInstruction, responseSchema, parts, maxOutputTokens, responseMimeType = "application/json" }) {
    const body = {
      contents: [{ role: "user", parts }],
      generationConfig: { maxOutputTokens },
    };
    if (systemInstruction) body.systemInstruction = { parts: [{ text: systemInstruction }] };
    if (responseMimeType) body.generationConfig.responseMimeType = responseMimeType;
    if (responseSchema) body.generationConfig.responseSchema = responseSchema;

    const res = await this._requestWithRetry(
      this._url(`models/${this.model}:generateContent`),
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      "generateContent",
    );
    const data = await res.json();
    const cand = data.candidates?.[0];
    const text = (cand?.content?.parts || []).map((p) => p.text || "").join("");
    return { text, finishReason: cand?.finishReason || null, usage: data.usageMetadata || null };
  }

  /**
   * Port of engine.py::_generate_json — structured JSON with the repair pass
   * and the MAX_TOKENS retry-and-double. `userContent` may be a string or a
   * pre-built parts array.
   */
  async generateJson(systemPrompt, responseSchema, userContent, maxOutputTokens, _retried = false) {
    const parts = typeof userContent === "string" ? [{ text: userContent }] : userContent;
    const { text, finishReason } = await this.generateContent({
      systemInstruction: systemPrompt,
      responseSchema,
      parts,
      maxOutputTokens,
    });
    const raw = text || "";
    try {
      return JSON.parse(raw);
    } catch {
      /* try the repair pass */
    }
    try {
      return JSON.parse(repairInvalidBackslashEscapes(raw));
    } catch (exc) {
      if (!_retried && finishReason === "MAX_TOKENS" && maxOutputTokens < MAX_OUTPUT_TOKENS_CAP) {
        return this.generateJson(
          systemPrompt,
          responseSchema,
          userContent,
          Math.min(maxOutputTokens * 2, MAX_OUTPUT_TOKENS_CAP),
          true,
        );
      }
      const snippet = JSON.stringify(raw.slice(0, 200));
      throw new GeminiError(
        `Gemini returned invalid JSON (${exc.message}, finish_reason=${finishReason}): ${snippet}`,
        { stage: "generateJson" },
      );
    }
  }

  // -------------------------------------------------------------------------
  // Files API (resumable upload)
  // -------------------------------------------------------------------------

  /**
   * Resumable upload of an audio Blob. Returns { uri, mimeType, name, state }.
   * Polls until the file is ACTIVE (or throws on FAILED / timeout).
   * `mimeType` MUST be the blob's real type — a mismatch between this and the
   * fileData mimeType later causes a 400 (verified 2026-08-31).
   */
  async uploadFile(blob, { displayName = "meeting-audio", pollIntervalMs = 2000, maxPollMs = 180000 } = {}) {
    const mimeType = blob.type || "application/octet-stream";
    const numBytes = blob.size;

    // 1. start the resumable session
    const startRes = await this._requestWithRetry(
      this._url("files", { upload: true }),
      {
        method: "POST",
        headers: {
          "X-Goog-Upload-Protocol": "resumable",
          "X-Goog-Upload-Command": "start",
          "X-Goog-Upload-Header-Content-Length": String(numBytes),
          "X-Goog-Upload-Header-Content-Type": mimeType,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ file: { display_name: displayName } }),
      },
      "files.upload.start",
    );
    const uploadUrl = startRes.headers.get("x-goog-upload-url");
    if (!uploadUrl) throw new GeminiError("Files API did not return an upload URL", { stage: "files.upload.start" });

    // 2. upload the bytes + finalize in one shot
    const uploadRes = await this._requestWithRetry(
      uploadUrl,
      {
        method: "POST",
        headers: {
          "Content-Length": String(numBytes),
          "X-Goog-Upload-Offset": "0",
          "X-Goog-Upload-Command": "upload, finalize",
        },
        body: blob,
      },
      "files.upload.bytes",
      { timeoutMs: this.uploadTimeoutMs },
    );
    const uploaded = await uploadRes.json();
    let file = uploaded.file || uploaded;

    // 3. poll until ACTIVE
    const deadline = Date.now() + maxPollMs;
    while (file.state === "PROCESSING") {
      if (Date.now() > deadline) throw new GeminiError("Timed out waiting for Gemini to process the audio", { stage: "files.poll" });
      await this._sleep(pollIntervalMs);
      const getRes = await this._requestWithRetry(this._url(file.name), { method: "GET" }, "files.get");
      file = await getRes.json();
    }
    if (file.state !== "ACTIVE") {
      throw new GeminiError(`Gemini could not process the audio (state=${file.state})`, { stage: "files.poll" });
    }
    return { uri: file.uri, mimeType: file.mimeType || mimeType, name: file.name, state: file.state };
  }

  /** Fetch a Files API entry's current metadata, e.g. { name, uri, mimeType, state }. */
  async getFile(name) {
    const res = await this._requestWithRetry(this._url(name), { method: "GET" }, "files.get");
    return res.json();
  }

  /** Courtesy cleanup — the file auto-expires after 48h regardless. Never throws. */
  async deleteFile(name) {
    if (!name) return;
    try {
      await this._fetch(this._url(name), { method: "DELETE" });
    } catch {
      /* best effort */
    }
  }
}

export { MAX_OUTPUT_TOKENS_CAP };
