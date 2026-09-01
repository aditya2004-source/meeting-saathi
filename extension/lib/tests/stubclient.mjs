/**
 * A stand-in for GeminiClient for the facts/generate unit tests — the JS
 * equivalent of the Python tests patching `_client.models.generate_content`.
 * Scripts generateJson() results (values or Error to throw) in call order.
 */
export class StubClient {
  constructor(scripted) {
    this._scripted = [...scripted];
    this.calls = [];
  }

  async generateJson(systemPrompt, schema, userContent, maxOutputTokens) {
    this.calls.push({ systemPrompt, schema, userContent, maxOutputTokens });
    if (this._scripted.length === 0) throw new Error(`StubClient: no result scripted for call #${this.calls.length}`);
    const next = this._scripted.shift();
    if (next instanceof Error) throw next;
    return typeof next === "function" ? next(userContent) : next;
  }
}

export const silentLogger = () => {};
