/** Test helpers: a scripted fake `fetch` and a Gemini response builder. */

export function jsonResponse(body, { status = 200, headers = {} } = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => headers[k.toLowerCase()] ?? headers[k] ?? null },
    async text() {
      return text;
    },
    async json() {
      return JSON.parse(text);
    },
  };
}

/** A generateContent payload with the given model text + finishReason. */
export function geminiText(text, finishReason = "STOP", usageMetadata = null) {
  return { candidates: [{ content: { parts: [{ text }] }, finishReason }], usageMetadata };
}

/**
 * Returns { fetch, calls } where `fetch` walks `responses` in order (each a
 * value from jsonResponse()), and `calls` records every { url, init, body }.
 * A function entry is invoked as (url, init) and must return a response.
 */
export function scriptedFetch(responses) {
  const calls = [];
  let i = 0;
  const fetch = async (url, init) => {
    let body;
    try {
      body = init && typeof init.body === "string" ? JSON.parse(init.body) : init?.body;
    } catch {
      body = init?.body;
    }
    calls.push({ url, init, body });
    const entry = responses[i++];
    if (entry === undefined) throw new Error(`scriptedFetch: no response scripted for call #${i} (${url})`);
    return typeof entry === "function" ? entry(url, init) : entry;
  };
  return { fetch, calls };
}

/** No-op backoff sleep for tests. */
export const noSleep = async () => {};
