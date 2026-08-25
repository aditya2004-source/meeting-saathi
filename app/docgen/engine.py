import json
from typing import Optional

from google import genai
from google.genai import types

from app.config import settings
from app.docgen.extract_prompt import EXTRACT_RESPONSE_SCHEMA, EXTRACT_SYSTEM_PROMPT
from app.docgen.generate_prompt import (
    BRD_RESPONSE_SCHEMA,
    BRD_SYSTEM_PROMPT,
    MEETING_ANALYSIS_RESPONSE_SCHEMA,
    MEETING_ANALYSIS_SYSTEM_PROMPT,
    MOM_RESPONSE_SCHEMA,
    MOM_SYSTEM_PROMPT,
    STORIES_AND_ACCEPTANCE_CRITERIA_RESPONSE_SCHEMA,
    STORIES_AND_ACCEPTANCE_CRITERIA_SYSTEM_PROMPT,
)
from app.docgen.render_diagram import render_business_process_mermaid
from app.docgen.render_tables import (
    render_acceptance_criteria_markdown,
    render_frd_markdown,
    render_user_stories_markdown,
)
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
        # a snippet so a real failure is diagnosable.
        snippet = text[:200]
        raise ValueError(
            f"Gemini returned invalid JSON ({exc}, finish_reason={finish_reason}): {snippet!r}"
        ) from exc


def extract_meeting_facts(transcript_text: str) -> dict:
    # 8192, not the old 4096 -- the extraction schema now includes requirements,
    # risks, assumptions, dependencies, open_questions, commitments, and an
    # optional business_process object, which won't reliably fit in the old
    # budget for a real meeting. The MAX_TOKENS retry-and-double logic in
    # _generate_json() is the safety net either way.
    return _generate_json(EXTRACT_SYSTEM_PROMPT, EXTRACT_RESPONSE_SCHEMA, transcript_text, max_output_tokens=8192)


def empty_meeting_facts() -> dict:
    """Same shape extract_meeting_facts() would return, for a meeting with no
    transcribed speech at all -- used instead of calling Gemini with an essentially
    empty transcript, which is a confirmed-in-production crash (the model's response
    wasn't valid JSON, most likely because there was nothing to extract).
    """
    return {
        "attendees": [],
        "topics_discussed": [],
        "decisions": [],
        "action_items": [],
        "key_quotes": [],
        "requirements": [],
        "risks": [],
        "assumptions": [],
        "dependencies": [],
        "open_questions": [],
        "commitments": [],
        "business_process": None,
    }


def _normalize_literal_newlines(text: str) -> str:
    """Gemini occasionally double-escapes a newline inside markdown_body --
    the raw JSON contains `\\\\n` (two backslashes then "n") instead of a
    single `\\n` escape. _repair_invalid_backslash_escapes() above correctly
    treats a backslash-backslash pair as a valid JSON escape (a literal
    backslash character), so json.loads() faithfully decodes it to a literal
    two-character "\\n" in the string instead of an actual line break.
    Confirmed in production: a Meeting Analysis document rendered as one
    unbroken line, headings and all, because of exactly this. A business
    document never legitimately needs the literal two-character sequence, so
    collapsing it to a real newline is safe.
    """
    return text.replace("\\n", "\n")


def _generate_document(
    system_prompt: str,
    response_schema: dict,
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    max_output_tokens: int = 8192,
) -> dict:
    grounding = {
        "meeting_title": meeting_title,
        "meeting_date": meeting_date,
        "attendees": attendees,
        "extracted_facts": facts,
    }
    user_content = (
        "Here is the grounding data (JSON) and the full transcript. "
        "Only use facts present here.\n\n"
        f"EXTRACTED FACTS:\n{json.dumps(grounding, indent=2)}\n\n"
        f"FULL TRANSCRIPT:\n{transcript_text}"
    )
    result = _generate_json(system_prompt, response_schema, user_content, max_output_tokens=max_output_tokens)
    if isinstance(result.get("markdown_body"), str):
        result["markdown_body"] = _normalize_literal_newlines(result["markdown_body"])
    return result


def _timed_generate_document(recorder: Optional[TimingRecorder], stage: str, *args, **kwargs) -> dict:
    if recorder is None:
        return _generate_document(*args, **kwargs)
    with timed(recorder, stage):
        return _generate_document(*args, **kwargs)


def _placeholder_doc(title: str, meeting_title: str, meeting_date: str, note: str) -> dict:
    heading_suffix = f" ({meeting_date})" if meeting_date else ""
    return {"title": title, "markdown_body": f"# {title} — {meeting_title}{heading_suffix}\n\n{note}\n"}


_NO_SPEECH_NOTE = "No speech was captured during this meeting, so there is nothing to summarize."
_NO_REQUIREMENTS_NOTE = "No requirements were extracted from this meeting, so there is nothing to generate."

# Every generator below shares this signature, even the ones that ignore most of it
# (FRD/Business Process Flow are local, zero-Gemini-call renders of `facts` only) --
# a uniform signature is what lets app/docgen/registry.py dispatch to any of them the
# same way, from one on-demand "Generate" click (see app/main.py's
# /meetings/{run_id}/documents/{doc_key}/generate route).


def generate_mom(
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    recorder: Optional[TimingRecorder] = None,
) -> dict:
    if not transcript_text.strip():
        return {"mom": _placeholder_doc("Minutes of Meeting", meeting_title, meeting_date, _NO_SPEECH_NOTE)}
    doc = _timed_generate_document(
        recorder,
        "generate_mom",
        MOM_SYSTEM_PROMPT,
        MOM_RESPONSE_SCHEMA,
        meeting_title,
        meeting_date,
        attendees,
        facts,
        transcript_text,
    )
    return {"mom": doc}


def generate_meeting_analysis(
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    recorder: Optional[TimingRecorder] = None,
) -> dict:
    if not transcript_text.strip():
        return {"meeting_analysis": _placeholder_doc("Meeting Analysis", meeting_title, meeting_date, _NO_SPEECH_NOTE)}
    doc = _timed_generate_document(
        recorder,
        "generate_meeting_analysis",
        MEETING_ANALYSIS_SYSTEM_PROMPT,
        MEETING_ANALYSIS_RESPONSE_SCHEMA,
        meeting_title,
        meeting_date,
        attendees,
        facts,
        transcript_text,
    )
    return {"meeting_analysis": doc}


def generate_brd(
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    recorder: Optional[TimingRecorder] = None,
) -> dict:
    if not transcript_text.strip():
        return {"brd": _placeholder_doc("Business Requirements Document (BRD)", meeting_title, meeting_date, _NO_SPEECH_NOTE)}
    doc = _timed_generate_document(
        recorder,
        "generate_brd",
        BRD_SYSTEM_PROMPT,
        BRD_RESPONSE_SCHEMA,
        meeting_title,
        meeting_date,
        attendees,
        facts,
        transcript_text,
    )
    return {"brd": doc}


def generate_frd(
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    recorder: Optional[TimingRecorder] = None,
) -> dict:
    """Zero-Gemini-call, deterministic render of facts["requirements"] -- see
    app/docgen/render_tables.py. Same tier as generate_business_process_flow().
    """
    requirements = facts.get("requirements") or []
    title = "Functional Requirements Document (FRD)"
    if not requirements:
        return {"frd": _placeholder_doc(title, meeting_title, meeting_date, _NO_REQUIREMENTS_NOTE)}
    markdown_body = render_frd_markdown(title, requirements)
    return {"frd": {"title": title, "markdown_body": markdown_body}}


def generate_user_stories_and_acceptance_criteria(
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    recorder: Optional[TimingRecorder] = None,
) -> dict:
    """One shared Gemini call producing both User Stories and Acceptance Criteria --
    see app/docgen/generate_prompt.py's STORIES_AND_ACCEPTANCE_CRITERIA_* -- rendered
    into two separate documents by two small renderers reading the same `stories`
    list, so requesting either one only ever costs one Gemini call.
    """
    requirements = facts.get("requirements") or []
    if not requirements:
        return {
            "user_stories": _placeholder_doc("User Stories", meeting_title, meeting_date, _NO_REQUIREMENTS_NOTE),
            "acceptance_criteria": _placeholder_doc(
                "Acceptance Criteria", meeting_title, meeting_date, _NO_REQUIREMENTS_NOTE
            ),
        }
    result = _timed_generate_document(
        recorder,
        "generate_stories_and_ac",
        STORIES_AND_ACCEPTANCE_CRITERIA_SYSTEM_PROMPT,
        STORIES_AND_ACCEPTANCE_CRITERIA_RESPONSE_SCHEMA,
        meeting_title,
        meeting_date,
        attendees,
        facts,
        transcript_text,
    )
    stories = result.get("stories") or []
    return {
        "user_stories": {"title": "User Stories", "markdown_body": render_user_stories_markdown("User Stories", stories)},
        "acceptance_criteria": {
            "title": "Acceptance Criteria",
            "markdown_body": render_acceptance_criteria_markdown("Acceptance Criteria", stories),
        },
    }


def generate_business_process_flow(
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    recorder: Optional[TimingRecorder] = None,
) -> Optional[dict]:
    """Zero-Gemini-call, deterministic render of facts["business_process"] into a
    Mermaid flowchart -- see app/docgen/render_diagram.py. Returns None (not
    generatable) when no business-process walkthrough was extracted from this
    meeting, rather than rendering an empty/meaningless diagram.
    """
    business_process = facts.get("business_process")
    steps = (business_process or {}).get("steps") or []
    if not business_process or not steps:
        return None
    process_name = business_process.get("process_name") or meeting_title
    mermaid_source = render_business_process_mermaid(process_name, steps)
    return {"business_process_flow": {"mermaid_source": mermaid_source}}
