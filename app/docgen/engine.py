import json
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from google import genai
from google.genai import types

from app.config import settings
from app.docgen.extract_prompt import EXTRACT_RESPONSE_SCHEMA, EXTRACT_SYSTEM_PROMPT
from app.docgen.generate_prompt import (
    ACTION_POINTS_RESPONSE_SCHEMA,
    ACTION_POINTS_SYSTEM_PROMPT,
    MOM_RESPONSE_SCHEMA,
    MOM_SYSTEM_PROMPT,
    REQUIREMENT_GATHERING_RESPONSE_SCHEMA,
    REQUIREMENT_GATHERING_SYSTEM_PROMPT,
)
from app.docgen.render_tables import render_requirement_gathering_markdown
from app.pipeline.timing import TimingRecorder, timed

_client = genai.Client(api_key=settings.gemini_api_key)

_VALID_JSON_ESCAPES = set('"\\/bfnrt')
_HEX_DIGITS = set("0123456789abcdefABCDEF")


def _repair_invalid_backslash_escapes(text: str) -> str:
    """Gemini's JSON-mode responses sometimes embed markdown in a string
    field (e.g. this document's own `markdown_body`) that itself uses
    backslashes -- markdown escapes like `\\*`, or content that just happens
    to contain `\\u` not meant as a unicode escape -- without doubling them,
    which isn't valid JSON. Confirmed in production: "Invalid \\uXXXX
    escape" ~3000 characters into a real markdown_body value. Doubles any
    backslash that isn't already part of a valid JSON escape sequence, so
    content that's semantically fine but not strictly valid JSON can still
    be parsed, instead of failing outright.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _VALID_JSON_ESCAPES:
                out.append(text[i : i + 2])
                i += 2
                continue
            if nxt == "u" and i + 6 <= n and all(c in _HEX_DIGITS for c in text[i + 2 : i + 6]):
                out.append(text[i : i + 6])
                i += 6
                continue
            # Not a valid JSON escape -- treat the backslash as a literal
            # character by doubling it, rather than leave invalid syntax.
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_MAX_OUTPUT_TOKENS_CAP = 32768


def _generate_json(
    system_prompt: str, response_schema: dict, user_content: str, max_output_tokens: int, _retried: bool = False
) -> dict:
    response = _client.models.generate_content(
        model=settings.gemini_model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_json_schema=response_schema,
            max_output_tokens=max_output_tokens,
        ),
    )
    text = response.text or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass  # try the repair pass below before giving up
    try:
        return json.loads(_repair_invalid_backslash_escapes(text))
    except json.JSONDecodeError as exc:
        finish_reason = None
        if response.candidates:
            finish_reason = response.candidates[0].finish_reason
        # Confirmed in production: a transcript with a long Whisper
        # hallucination loop (e.g. a repeated word in a low-signal audio
        # chunk) makes Gemini's response balloon -- it genuinely runs out
        # of its max_output_tokens budget mid-string, which json.loads
        # reports as "Unterminated string...". Retry once with double the
        # budget (capped) instead of failing the whole run outright.
        if (
            not _retried
            and finish_reason == types.FinishReason.MAX_TOKENS
            and max_output_tokens < _MAX_OUTPUT_TOKENS_CAP
        ):
            return _generate_json(
                system_prompt,
                response_schema,
                user_content,
                min(max_output_tokens * 2, _MAX_OUTPUT_TOKENS_CAP),
                _retried=True,
            )
        # A bare JSONDecodeError ("Unterminated string starting at: line 1
        # column 82") gives no clue what Gemini actually returned -- include
        # a snippet so a real failure (as opposed to the empty-transcript
        # case handled by empty_meeting_documents() below, or the repairable
        # backslash-escape case handled above) is diagnosable.
        snippet = text[:200]
        raise ValueError(
            f"Gemini returned invalid JSON ({exc}, finish_reason={finish_reason}): {snippet!r}"
        ) from exc


def extract_meeting_facts(transcript_text: str) -> dict:
    return _generate_json(EXTRACT_SYSTEM_PROMPT, EXTRACT_RESPONSE_SCHEMA, transcript_text, max_output_tokens=4096)


def empty_meeting_documents(meeting_title: str) -> dict:
    """Same return shape as generate_documents(), for a meeting with no
    transcribed speech at all (e.g. a short test call where nobody spoke).

    Calling Gemini with an essentially empty transcript is what caused a
    real crash -- confirmed in production, the model's response wasn't
    valid JSON (json.loads raised "Unterminated string starting at: line 1
    column 82"), most likely because there was nothing for it to summarize.
    Skipping the call entirely for this case is both correctness (a
    "no speech captured" note is a truthful, useful document) and avoids
    burning Gemini quota on a call that can't produce anything meaningful
    anyway.
    """
    note = "No speech was captured during this meeting, so there is nothing to summarize."
    return {
        "facts": {},
        "mom": {"title": "Minutes of Meeting", "markdown_body": f"# Minutes of Meeting — {meeting_title}\n\n{note}\n"},
        "requirement_gathering": {
            "title": "Requirement Gathering Sheet",
            "markdown_body": f"# Requirement Gathering Sheet — {meeting_title}\n\n{note}\n",
        },
        "action_points": {
            "title": "Action Points",
            "markdown_body": f"# Action Points — {meeting_title}\n\n{note}\n",
        },
    }


def _generate_document(
    system_prompt: str,
    response_schema: dict,
    meeting_title: str,
    facts: dict,
    transcript_text: str,
) -> dict:
    grounding = {"meeting_title": meeting_title, "extracted_facts": facts}
    user_content = (
        "Here is the grounding data (JSON) and the full transcript. "
        "Only use facts present here.\n\n"
        f"EXTRACTED FACTS:\n{json.dumps(grounding, indent=2)}\n\n"
        f"FULL TRANSCRIPT:\n{transcript_text}"
    )
    return _generate_json(system_prompt, response_schema, user_content, max_output_tokens=8192)


def generate_documents(
    meeting_title: str, transcript_text: str, recorder: Optional[TimingRecorder] = None
) -> dict:
    """Two-call pattern: Call 1 extracts structured facts (forced JSON
    schema), Call 2 runs three times (forced JSON schema each) to write
    MOM, the Requirement Gathering Sheet, and Action Points -- each grounded
    only in Call 1's facts + the transcript, so the three are independent of
    each other and run concurrently (network-bound Gemini calls release the
    GIL, so real threads pay off here without needing an asyncio rewrite).
    """
    if recorder is None:
        facts = extract_meeting_facts(transcript_text)
    else:
        with timed(recorder, "extract_facts"):
            facts = extract_meeting_facts(transcript_text)

    def _generate(stage: str, system_prompt, schema):
        if recorder is None:
            return _generate_document(system_prompt, schema, meeting_title, facts, transcript_text)
        with timed(recorder, stage):
            return _generate_document(system_prompt, schema, meeting_title, facts, transcript_text)

    with ThreadPoolExecutor(max_workers=3) as pool:
        mom_future = pool.submit(_generate, "generate_mom", MOM_SYSTEM_PROMPT, MOM_RESPONSE_SCHEMA)
        rg_future = pool.submit(
            _generate,
            "generate_requirement_gathering",
            REQUIREMENT_GATHERING_SYSTEM_PROMPT,
            REQUIREMENT_GATHERING_RESPONSE_SCHEMA,
        )
        ap_future = pool.submit(
            _generate, "generate_action_points", ACTION_POINTS_SYSTEM_PROMPT, ACTION_POINTS_RESPONSE_SCHEMA
        )
        mom = mom_future.result()
        requirement_gathering = rg_future.result()
        action_points = ap_future.result()

    requirement_gathering["markdown_body"] = render_requirement_gathering_markdown(
        requirement_gathering["title"], requirement_gathering["rows"]
    )
    return {
        "facts": facts,
        "mom": mom,
        "requirement_gathering": requirement_gathering,
        "action_points": action_points,
    }
