import test from "node:test";
import assert from "node:assert/strict";

import {
  GeminiClient,
  cleanMarkdownBody,
  fixHeaderlessTables,
  normalizeLiteralNewlines,
  repairInvalidBackslashEscapes,
  stripTrailingModelNoise,
  unescapeHtmlEntities,
} from "../gemini.js";
import { geminiText, jsonResponse, noSleep, scriptedFetch } from "./helpers.mjs";

// --- repair helpers (port of tests/test_docgen_json_repair.py) ---

test("valid JSON is unchanged by the escape repair", () => {
  const text = JSON.stringify({ a: "line one\nline two", b: 'quote: " and backslash: \\\\' });
  assert.equal(repairInvalidBackslashEscapes(text), text);
  assert.deepEqual(JSON.parse(repairInvalidBackslashEscapes(text)), JSON.parse(text));
});

test("repairs an invalid \\u escape not followed by four hex digits", () => {
  const broken = '{"markdown_body": "some text \\unit more text"}';
  assert.equal(JSON.parse(repairInvalidBackslashEscapes(broken)).markdown_body, "some text \\unit more text");
});

test("repairs markdown escape sequences", () => {
  const broken = '{"markdown_body": "bold\\*text\\* and \\_italic\\_"}';
  assert.equal(JSON.parse(repairInvalidBackslashEscapes(broken)).markdown_body, "bold\\*text\\* and \\_italic\\_");
});

test("a valid unicode escape still decodes to the right character", () => {
  const text = '{"markdown_body": "caf\\u00e9"}';
  assert.equal(JSON.parse(repairInvalidBackslashEscapes(text)).markdown_body, "café");
});

test("valid escapes like newline and tab are preserved", () => {
  const text = '{"markdown_body": "line one\\nline two\\ttabbed"}';
  assert.equal(JSON.parse(repairInvalidBackslashEscapes(text)).markdown_body, "line one\nline two\ttabbed");
});

test("normalizeLiteralNewlines collapses over-escaped runs", () => {
  assert.equal(normalizeLiteralNewlines("a\\nb"), "a\nb");
  assert.equal(normalizeLiteralNewlines("a\\\\nb"), "a\nb");
  assert.equal(normalizeLiteralNewlines("a\\\\\\nb"), "a\nb");
  assert.equal(normalizeLiteralNewlines("col1\\tcol2"), "col1\tcol2");
});

test("unescapeHtmlEntities decodes the common entities", () => {
  assert.equal(unescapeHtmlEntities("Purpose &amp; Scope"), "Purpose & Scope");
  assert.equal(unescapeHtmlEntities("&lt;tag&gt; &quot;q&quot; &#39;a&#39;"), '<tag> "q" \'a\'');
});

test("stripTrailingModelNoise removes a leaked control-token tail", () => {
  const body = '## Section\nReal content here.\n```\n"}\n<|im_end|>_dst_id_=';
  const cleaned = stripTrailingModelNoise(body);
  assert.ok(!cleaned.includes("<|im_end|>"));
  assert.ok(!cleaned.includes("```"));
  assert.ok(cleaned.trimEnd().endsWith("Real content here."));
});

test("cleanMarkdownBody composes all passes", () => {
  const out = cleanMarkdownBody("Heading&amp;More\\nnext line");
  assert.equal(out, "Heading&More\nnext line\n");
});

test("fixHeaderlessTables injects a header for a header-less 2-col table", () => {
  const src = "## Meeting Overview\n| Title | Demo |\n| Date | Sept 2 |\n| Attendees | A, B |\n\n## Next";
  const out = fixHeaderlessTables(src);
  assert.ok(out.includes("| Field | Detail |\n| --- | --- |\n| Title | Demo |"));
});

test("fixHeaderlessTables leaves a valid table untouched", () => {
  const src = "| Area | Today | Later |\n| --- | --- | --- |\n| Calls | manual | auto |";
  assert.equal(fixHeaderlessTables(src), src);
});

test("fixHeaderlessTables ignores a lone pipe line that is not a table", () => {
  const src = "Some prose with a | pipe | inside it.";
  assert.equal(fixHeaderlessTables(src), src);
});

// --- generateJson: MAX_TOKENS retry-and-double (port of tests/test_docgen_max_tokens_retry.py) ---

function client(responses) {
  const { fetch, calls } = scriptedFetch(responses);
  return { c: new GeminiClient({ apiKey: "k", model: "m", fetchImpl: fetch, sleepImpl: noSleep }), calls };
}

test("retries with a doubled budget on MAX_TOKENS truncation", async () => {
  const { c, calls } = client([
    jsonResponse(geminiText('{"a": "this got cut off mid-str', "MAX_TOKENS")),
    jsonResponse(geminiText('{"a": "full value"}', "STOP")),
  ]);
  const result = await c.generateJson("system", { type: "OBJECT" }, "content", 4096);
  assert.deepEqual(result, { a: "full value" });
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.generationConfig.maxOutputTokens, 8192);
});

test("gives up after one retry if still truncated", async () => {
  const { c, calls } = client([
    jsonResponse(geminiText('{"a": "still cut off', "MAX_TOKENS")),
    jsonResponse(geminiText('{"a": "still cut off', "MAX_TOKENS")),
  ]);
  await assert.rejects(() => c.generateJson("system", { type: "OBJECT" }, "content", 4096), /invalid JSON/);
  assert.equal(calls.length, 2);
});

test("does not retry when truncation is not the cause", async () => {
  const { c, calls } = client([jsonResponse(geminiText('{"a": "unterminated', "STOP"))]);
  await assert.rejects(() => c.generateJson("system", { type: "OBJECT" }, "content", 4096), /invalid JSON/);
  assert.equal(calls.length, 1);
});

test("generateJson parses a clean structured response", async () => {
  const { c } = client([jsonResponse(geminiText('{"title":"T","markdown_body":"B"}'))]);
  assert.deepEqual(await c.generateJson("s", {}, "u", 8192), { title: "T", markdown_body: "B" });
});

// --- transient-error backoff ---

test("retries a 503 then succeeds", async () => {
  const { c, calls } = client([
    jsonResponse({ error: { message: "high demand" } }, { status: 503 }),
    jsonResponse(geminiText('{"ok":true}')),
  ]);
  assert.deepEqual(await c.generateJson("s", {}, "u", 8192), { ok: true });
  assert.equal(calls.length, 2);
});

test("a 400 is not retried and surfaces the API message", async () => {
  const { c, calls } = client([
    jsonResponse({ error: { message: "MIME type does not match" } }, { status: 400 }),
  ]);
  await assert.rejects(() => c.generateJson("s", {}, "u", 8192), /MIME type does not match/);
  assert.equal(calls.length, 1);
});

test("a quota 429 fails FAST — no retries (each retry would burn more of the exhausted quota)", async () => {
  const waits = [];
  const { fetch, calls } = scriptedFetch([
    jsonResponse(
      {
        error: {
          code: 429,
          status: "RESOURCE_EXHAUSTED",
          message: "You exceeded your current quota, please check your plan and billing details",
          details: [{ "@type": "type.googleapis.com/google.rpc.RetryInfo", retryDelay: "40s" }],
        },
      },
      { status: 429 },
    ),
  ]);
  const c = new GeminiClient({ apiKey: "k", model: "m", fetchImpl: fetch, sleepImpl: async (ms) => waits.push(ms) });
  await c.generateJson("s", {}, "u", 8192).then(
    () => assert.fail("should have thrown"),
    (err) => {
      assert.equal(err.quotaExceeded, true);
      assert.equal(err.status, 429);
      assert.equal(err.retryAfterMs, 40000);
    },
  );
  assert.equal(calls.length, 1); // exactly one request, no retry storm
  assert.deepEqual(waits, []);
});

test("a 401 gives a key-rejected hint and doesn't retry", async () => {
  const { c, calls } = client([
    jsonResponse({ error: { message: "Request had invalid authentication credentials" } }, { status: 401 }),
  ]);
  await c.generateJson("s", {}, "u", 8192).then(
    () => assert.fail("should have thrown"),
    (err) => {
      assert.match(err.message, /key was rejected/i);
      assert.equal(err.badKey, true);
    },
  );
  assert.equal(calls.length, 1);
});

test("constructor rejects a missing API key", () => {
  assert.throws(() => new GeminiClient({ apiKey: "" }), /API key/);
});

// --- per-request timeout: a stalled connection must fail, not hang ---

test("a hung request aborts after requestTimeoutMs, retries, then throws 'timed out'", async () => {
  const retries = [];
  const hangingFetch = (_url, init) =>
    new Promise((_res, reject) => {
      init.signal.addEventListener("abort", () => {
        const e = new Error("The operation was aborted");
        e.name = "AbortError";
        reject(e);
      });
    });
  const c = new GeminiClient({
    apiKey: "k",
    model: "m",
    fetchImpl: hangingFetch,
    sleepImpl: noSleep,
    requestTimeoutMs: 5,
    maxRetries: 2,
    onRetry: (n, ms, status) => retries.push(status),
  });
  await assert.rejects(() => c.generateJson("s", {}, "u", 8192), /timed out after 0s/);
  assert.deepEqual(retries, ["timeout", "timeout"]); // 2 retries before giving up
});

test("a request that answers within the timeout is unaffected", async () => {
  const { fetch } = scriptedFetch([jsonResponse(geminiText('{"ok":1}'))]);
  const c = new GeminiClient({ apiKey: "k", model: "m", fetchImpl: fetch, sleepImpl: noSleep, requestTimeoutMs: 5000 });
  assert.deepEqual(await c.generateJson("s", {}, "u", 8192), { ok: 1 });
});

// --- Files API resumable upload ---

test("uploadFile does start -> upload -> and returns the file uri", async () => {
  const blob = new Blob([new Uint8Array(1024)], { type: "audio/webm" });
  const { fetch, calls } = scriptedFetch([
    jsonResponse({}, { headers: { "x-goog-upload-url": "https://upload.example/resumable/abc" } }),
    jsonResponse({ file: { name: "files/xyz", uri: "https://g/files/xyz", mimeType: "audio/webm", state: "ACTIVE" } }),
  ]);
  const c = new GeminiClient({ apiKey: "k", model: "m", fetchImpl: fetch, sleepImpl: noSleep });
  const out = await c.uploadFile(blob);
  assert.equal(out.uri, "https://g/files/xyz");
  assert.equal(out.name, "files/xyz");
  assert.equal(calls[0].init.headers["X-Goog-Upload-Command"], "start");
  assert.equal(calls[1].url, "https://upload.example/resumable/abc");
  assert.equal(calls[1].init.headers["X-Goog-Upload-Command"], "upload, finalize");
});

test("uploadFile polls while PROCESSING then resolves on ACTIVE", async () => {
  const blob = new Blob([new Uint8Array(8)], { type: "audio/webm" });
  const { fetch } = scriptedFetch([
    jsonResponse({}, { headers: { "x-goog-upload-url": "https://upload.example/r" } }),
    jsonResponse({ file: { name: "files/p", uri: "u", state: "PROCESSING" } }),
    jsonResponse({ name: "files/p", uri: "u", mimeType: "audio/webm", state: "PROCESSING" }),
    jsonResponse({ name: "files/p", uri: "u", mimeType: "audio/webm", state: "ACTIVE" }),
  ]);
  const c = new GeminiClient({ apiKey: "k", model: "m", fetchImpl: fetch, sleepImpl: noSleep });
  const out = await c.uploadFile(blob, { pollIntervalMs: 1 });
  assert.equal(out.state, "ACTIVE");
});
