import json
import re
import traceback
from typing import Optional

from google import genai
from google.genai import types

from app.config import settings
from app.docgen.extract_prompt import (
    CONTEXT_EXTRACT_RESPONSE_SCHEMA,
    CONTEXT_EXTRACT_SYSTEM_PROMPT,
    CORE_EXTRACT_RESPONSE_SCHEMA,
    CORE_EXTRACT_SYSTEM_PROMPT,
    VERIFY_EXTRACT_RESPONSE_SCHEMA,
    VERIFY_EXTRACT_SYSTEM_PROMPT,
)
from app.docgen.generate_prompt import (
    BUSINESS_PROCESS_FLOW_RESPONSE_SCHEMA,
    BUSINESS_PROCESS_FLOW_SYSTEM_PROMPT,
    MEETING_ANALYSIS_RESPONSE_SCHEMA,
    MEETING_ANALYSIS_SYSTEM_PROMPT,
    MOM_RESPONSE_SCHEMA,
    MOM_SYSTEM_PROMPT,
    SOW_RESPONSE_SCHEMA,
    SOW_SYSTEM_PROMPT,
)
from app.docgen.reconcile_prompt import RECONCILE_RESPONSE_SCHEMA, RECONCILE_SYSTEM_PROMPT
from app.docgen.validate import review_markdown_document
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


_MAX_OUTPUT_TOKENS_CAP = 49152


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


_CORE_LIST_FIELDS = (
    "topics_discussed",
    "decisions",
    "action_items",
    "key_quotes",
    "requirements",
    "risks",
    "assumptions",
    "dependencies",
    "open_questions",
    "commitments",
    "business_processes",
)


def _item_key(item) -> str:
    """A loose identity key for deduping facts list entries across the draft and
    the verification pass -- the first meaningful text field, normalised.
    """
    if isinstance(item, str):
        text = item
    elif isinstance(item, dict):
        text = next(
            (
                str(item[k])
                for k in ("id", "decision", "description", "statement", "question", "commitment", "quote", "process_name")
                if item.get(k)
            ),
            json.dumps(item, sort_keys=True),
        )
    else:
        text = str(item)
    return " ".join(text.lower().split())[:120]


def _union_verified_facts(draft: dict, verified: dict) -> dict:
    """Union the draft extraction with the verification pass so nothing either
    call found is lost, then drop anything the verification pass explicitly
    flagged as unsupported. Requirements keep the draft's ids.
    """
    unsupported = {" ".join(str(s).lower().split()) for s in (verified.get("unsupported_items") or [])}

    def _is_unsupported(item) -> bool:
        key = _item_key(item)
        return any(u and (u in key or key in u) for u in unsupported)

    merged = dict(draft)
    for field in _CORE_LIST_FIELDS:
        seen: dict[str, object] = {}
        for item in (draft.get(field) or []) + (verified.get(field) or []):
            if _is_unsupported(item):
                continue
            seen.setdefault(_item_key(item), item)
        merged[field] = list(seen.values())
    # Scalars/other keys: prefer the verified value when it's non-empty.
    for k, v in verified.items():
        if k in ("unsupported_items", *_CORE_LIST_FIELDS):
            continue
        if v not in (None, "", []):
            merged[k] = v
    return merged


def extract_meeting_facts(transcript_text: str) -> dict:
    """Builds one facts.json from focused Gemini calls -- see the note at the top
    of app/docgen/extract_prompt.py for why extraction is split. Call 1 is the
    concrete record; in quality mode call 2 re-reads the transcript against that
    draft and the union is kept (nothing either pass found is lost); the final
    call is the BA-analysis context. Each optional pass fails soft -- its absence
    never loses the earlier passes' facts.
    """
    core = _generate_json(
        CORE_EXTRACT_SYSTEM_PROMPT, CORE_EXTRACT_RESPONSE_SCHEMA, transcript_text, max_output_tokens=16384
    )

    if settings.docgen_quality_mode:
        try:
            verified = _generate_json(
                VERIFY_EXTRACT_SYSTEM_PROMPT,
                VERIFY_EXTRACT_RESPONSE_SCHEMA,
                f"DRAFT EXTRACTION:\n{json.dumps(core, indent=2)}\n\nFULL TRANSCRIPT:\n{transcript_text}",
                max_output_tokens=16384,
            )
            core = _union_verified_facts(core, verified)
        except Exception:  # noqa: BLE001 - verification is additive, never fail extraction for it
            traceback.print_exc()

    try:
        context = _generate_json(
            CONTEXT_EXTRACT_SYSTEM_PROMPT,
            CONTEXT_EXTRACT_RESPONSE_SCHEMA,
            transcript_text,
            max_output_tokens=8192,
        )
    except Exception:  # noqa: BLE001 - context enrichment is additive, never fail the whole extraction for it
        traceback.print_exc()
        context = {}
    empty_context = {
        "meeting_purpose": None,
        "goals": [],
        "current_state": [],
        "constraints": [],
        "systems_and_integrations": [],
        "glossary": [],
        "non_functional_notes": [],
    }
    return {**empty_context, **core, **{k: v for k, v in context.items() if v not in (None, [])}}


def reconcile_speaker_identities(
    transcript_text: str, placeholder_labels: list[str], roster_hint: str = ""
) -> dict:
    """One Gemini call that collapses a failed diarization's many
    "Unidentified speaker N" labels back into the true small participant
    set -- see app/docgen/reconcile_prompt.py and
    app/pipeline/speaker_reconcile.py. Only invoked when placeholder labels
    dominate a transcript (a normal, well-diarized meeting never triggers
    it), so it doesn't erode the on-demand-generation / quota posture.

    Returns the raw {"participants": [...], "label_map": [...]} dict;
    speaker_reconcile.apply_reconciliation() validates and applies it.
    """
    labels_block = "\n".join(f"- {label}" for label in placeholder_labels)
    user_content = (
        "These are the distinct unidentified-speaker labels in the transcript; map "
        "every one of them:\n"
        f"{labels_block}\n\n"
    )
    if roster_hint.strip():
        user_content += (
            "The meeting organiser says the real attendees were (use these names only "
            "where the transcript supports them):\n"
            f"{roster_hint.strip()}\n\n"
        )
    user_content += f"FULL TRANSCRIPT:\n{transcript_text}"
    # The inverted output (a handful of participants, each with a list of
    # integer speaker numbers) is compact even for a 100+ fragment meeting;
    # 16384 leaves comfortable headroom, and the MAX_TOKENS retry-and-double
    # in _generate_json() is the safety net.
    return _generate_json(
        RECONCILE_SYSTEM_PROMPT, RECONCILE_RESPONSE_SCHEMA, user_content, max_output_tokens=16384
    )


_TRANSLATE_SYSTEM_PROMPT = (
    "You translate business-meeting transcript segments into natural English. You are "
    "given a JSON array of items, each {\"index\": int, \"text\": str} -- one verbatim "
    "spoken utterance, usually Hindi (often Roman-script or mixed Hindi-English). For "
    "EVERY item, return {\"index\": <same index>, \"text\": <English translation>}:\n"
    "- Translate the meaning into clear, natural English.\n"
    "- Keep people's names, company/product names, numbers, and money/quantity figures "
    "exactly as spoken.\n"
    "- If an item is already entirely English, return its text unchanged.\n"
    "- Never merge, split, reorder, summarise, expand, or drop an item. Return exactly "
    "as many items as you were given, with the same index values."
)

_TRANSLATE_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "translations": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "text": {"type": "STRING"},
                },
                "required": ["index", "text"],
            },
        }
    },
    "required": ["translations"],
}


def translate_transcript_segments(items: list[dict]) -> dict:
    """One Gemini call that translates transcript segments to English -- used by
    app/pipeline/translate.py to fix the AssemblyAI fallback path, which
    transcribes in the spoken language (no task="translate" equivalent) so
    Hindi text would otherwise reach transcript.json/transcript.txt verbatim.
    The DOM-primary and local-Whisper paths already decode straight to English.

    `items` is [{"index": int, "text": str}] for only the segments that need
    translating. Returns {"translations": [{"index": int, "text": str}]}; the
    caller maps results back by index and keeps the original for any index the
    model dropped.
    """
    user_content = "Translate every item to English:\n" + json.dumps(items, ensure_ascii=False)
    return _generate_json(
        _TRANSLATE_SYSTEM_PROMPT, _TRANSLATE_RESPONSE_SCHEMA, user_content, max_output_tokens=32768
    )


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
        "meeting_purpose": None,
        "goals": [],
        "current_state": [],
        "constraints": [],
        "systems_and_integrations": [],
        "glossary": [],
        "non_functional_notes": [],
        "risks": [],
        "assumptions": [],
        "dependencies": [],
        "open_questions": [],
        "commitments": [],
        "business_processes": [],
    }


def business_processes_from_facts(facts: dict) -> list[dict]:
    """The `business_processes` list, tolerating facts.json written before it
    became a list (a single nullable `business_process` object).
    """
    processes = facts.get("business_processes")
    if isinstance(processes, list):
        return [p for p in processes if isinstance(p, dict) and (p.get("steps") or [])]
    legacy = facts.get("business_process")
    if isinstance(legacy, dict) and (legacy.get("steps") or []):
        return [legacy]
    return []


_ANON_ATTENDEE_RE = re.compile(r"^(Unidentified speaker|Participant)\b", re.IGNORECASE)


def _attendees_for_prompt(attendees: list[str]) -> list[str]:
    """When every "attendee" is an anonymous placeholder ("Unidentified
    speaker 7", "Participant 3") -- i.e. diarization ran but named nobody,
    even after the reconciliation pass -- feeding Gemini the verbatim list
    (which the grounding rule says to reproduce and never drop) just puts a
    wall of meaningless labels in the document. Collapse it to a single
    honest statement of the count instead. A list with even one real name is
    passed through untouched.
    """
    if attendees and all(_ANON_ATTENDEE_RE.match(a or "") for a in attendees):
        n = len(attendees)
        speaker_word = "speaker" if n == 1 else "distinct speakers"
        return [f"{n} {speaker_word} took part; individual names could not be identified from the meeting audio"]
    return attendees


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
    # Collapse any run of backslashes immediately before n/t (Gemini sometimes
    # over-escapes -- `\\\\n` in the raw JSON decodes to a literal backslash +
    # backslash + "n", not a line break) down to a real whitespace character.
    text = re.sub(r"\\{2,}n", "\n", text)
    text = re.sub(r"\\{2,}t", "\t", text)
    return text.replace("\\n", "\n").replace("\\t", "\t")


_HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&nbsp;": " ",
}


def _unescape_html_entities(text: str) -> str:
    """Gemini's JSON mode occasionally HTML-escapes prose ("Purpose &amp; Scope")
    -- a business document should never render a literal entity, so decode the
    common ones.
    """
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    return text


# Chat-template control tokens and their common leaked companions. A
# well-behaved response never contains any of these; confirmed in
# production once (gemini-3.5-flash appended `...)"}` + a stray ``` fence +
# `<|im_end|>` + `_dst_id_=` to the end of an FRD's markdown_body, all
# inside the JSON string so it parsed clean). Everything from the first
# such marker to the end of the document is model noise, not content.
_CONTROL_TOKEN_RE = re.compile(r"<\|(?:im_end|im_start|endoftext|eot_id)\|>|_dst_id_=")


_META_TAIL_RE = re.compile(
    r"(?i)\b(use it verbatim|do not insert any other symbols|"
    r"this is the complete and valid json)\b"
)


def _strip_trailing_model_noise(text: str) -> str:
    marker = _CONTROL_TOKEN_RE.search(text)
    if marker:
        text = text[: marker.start()]
        # A response that leaked a control token usually also leaked, just
        # before it, an orphan closing code fence and/or a "use this
        # verbatim" meta-instruction. Peel those trailing lines off until
        # the tail is real content again.
        lines = text.rstrip().split("\n")
        while lines and (
            lines[-1].strip() in ("```", "```json", '"}', "}")
            or _META_TAIL_RE.search(lines[-1])
        ):
            lines.pop()
        # The leak often starts by the model "closing the JSON" mid-line:
        # a real trailing content line ending in an unescaped `"}`.
        if lines:
            lines[-1] = re.sub(r'"\}\s*$', "", lines[-1])
        text = "\n".join(lines)
    return text.rstrip() + "\n" if text.strip() else text


_PIPE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_PIPE_SEP_RE = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")


def _fix_headerless_tables(text: str) -> str:
    """Gemini often emits a key/value table with no header + separator row
    (`| Title | ... |` straight away), which python-markdown's `tables`
    extension then renders as literal pipe text instead of a table. For any run
    of 2+ consecutive pipe rows that has no separator row, insert a
    `| Field | Detail |` / `| --- | --- |` header (2-col) or a blank header of
    the right width (wider), so it renders as an actual table.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _PIPE_ROW_RE.match(lines[i]):
            j = i
            while j < len(lines) and _PIPE_ROW_RE.match(lines[j]):
                j += 1
            block = lines[i:j]
            if len(block) >= 2 and not any(_PIPE_SEP_RE.match(b) for b in block):
                cols = block[0].strip().strip("|").count("|") + 1
                if cols == 2:
                    out.append("| Field | Detail |")
                else:
                    out.append("|" + " |" * cols)
                out.append("|" + " --- |" * cols)
            out.extend(block)
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _clean_markdown_body(text: str) -> str:
    return _fix_headerless_tables(
        _strip_trailing_model_noise(_unescape_html_entities(_normalize_literal_newlines(text)))
    )


_REFINE_INSTRUCTION = (
    "You produced the DRAFT DOCUMENT below. Produce an improved final version:\n"
    "- Fix every issue in AUTOMATED CHECK FINDINGS (if any).\n"
    "- Make every section complete: if a section is thin but the topic clearly got "
    "real discussion, expand it from the EXTRACTED FACTS and TRANSCRIPT (never invent).\n"
    "- This is a client-facing document: never write an internal requirement id "
    "(REQ-1, [REQ-3], ...), JSON field name, or bracketed tag -- use plain words.\n"
    "- Remove anything not supported by the facts or transcript.\n"
    "- Keep the exact section structure from your instructions; emit no heading that "
    "isn't in that structure, no code fence, no JSON, no <|...|> marker.\n"
    "Return the full corrected document (same schema).\n\n"
)

_REFINE_INSTRUCTION_WITH_DIAGRAMS = (
    "You produced the DRAFT DOCUMENT below. Produce an improved final version:\n"
    "- Fix every issue in AUTOMATED CHECK FINDINGS (if any).\n"
    "- Make every section complete: if a section is thin but the topic clearly got "
    "real discussion, expand it from the EXTRACTED FACTS and TRANSCRIPT (never invent).\n"
    "- Remove anything not supported by the facts or transcript.\n"
    "- Keep the exact section structure from your instructions.\n"
    "- Keep each ```mermaid diagram as a fenced ```mermaid block; keep every diagram "
    "dead simple and syntactically valid (about 8 nodes at most, mostly a straight "
    "top-to-bottom line, short quoted labels). Emit no other code fence, no JSON, no "
    "<|...|> marker.\n"
    "Return the full corrected document (same schema).\n\n"
)


def _refine_markdown_document(
    system_prompt: str,
    response_schema: dict,
    doc_key: str,
    draft: dict,
    grounding_json: str,
    transcript_text: str,
    facts: dict,
    max_output_tokens: int,
    allow_mermaid: bool = False,
) -> dict:
    """One extra Gemini pass (quality mode only) that hands the model back its own
    draft plus the deterministic validator's findings and asks for a corrected,
    more complete version. Returns the draft unchanged if the refine call fails or
    comes back suspiciously short (a sign the model truncated or lost content).
    """
    draft_body = draft.get("markdown_body")
    if not isinstance(draft_body, str) or not draft_body.strip():
        return draft
    findings = review_markdown_document(doc_key, draft_body, facts)
    findings_block = "\n".join(f"- {f}" for f in findings) if findings else "- (no automated findings)"
    instruction = _REFINE_INSTRUCTION_WITH_DIAGRAMS if allow_mermaid else _REFINE_INSTRUCTION
    user_content = (
        f"{instruction}"
        f"AUTOMATED CHECK FINDINGS:\n{findings_block}\n\n"
        f"DRAFT DOCUMENT:\n{draft_body}\n\n"
        f"EXTRACTED FACTS:\n{grounding_json}\n\n"
        f"FULL TRANSCRIPT:\n{transcript_text}"
    )
    try:
        refined = _generate_json(system_prompt, response_schema, user_content, max_output_tokens=max_output_tokens)
    except Exception:  # noqa: BLE001 - refine is a bonus pass; keep the draft on any failure
        traceback.print_exc()
        return draft
    refined_body = refined.get("markdown_body")
    if not isinstance(refined_body, str) or len(refined_body.strip()) < 0.6 * len(draft_body.strip()):
        return draft
    refined["markdown_body"] = _clean_markdown_body(refined_body)
    return refined


def _generate_document(
    system_prompt: str,
    response_schema: dict,
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    max_output_tokens: int = 8192,
    doc_key: str = "",
    allow_mermaid: bool = False,
) -> dict:
    grounding = {
        "meeting_title": meeting_title,
        "meeting_date": meeting_date,
        "attendees": _attendees_for_prompt(attendees),
        "extracted_facts": facts,
    }
    grounding_json = json.dumps(grounding, indent=2)
    user_content = (
        "Here is the grounding data (JSON) and the full transcript. "
        "Only use facts present here.\n\n"
        f"EXTRACTED FACTS:\n{grounding_json}\n\n"
        f"FULL TRANSCRIPT:\n{transcript_text}"
    )
    result = _generate_json(system_prompt, response_schema, user_content, max_output_tokens=max_output_tokens)
    if isinstance(result.get("markdown_body"), str):
        result["markdown_body"] = _clean_markdown_body(result["markdown_body"])
        if settings.docgen_quality_mode and doc_key:
            result = _refine_markdown_document(
                system_prompt, response_schema, doc_key, result, grounding_json, transcript_text, facts,
                max_output_tokens, allow_mermaid=allow_mermaid,
            )
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

# Every generator below shares this signature (meeting_title, meeting_date, attendees,
# facts, transcript_text, recorder) and returns {doc_key: {title, markdown_body}} --
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
        doc_key="mom",
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
        doc_key="meeting_analysis",
    )
    return {"meeting_analysis": doc}


def generate_sow(
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    recorder: Optional[TimingRecorder] = None,
) -> dict:
    """One Gemini call producing a full Statement of Work / Scope of Work
    (engagement overview, scope in/out, deliverables, approach, roles,
    acceptance, risks, sign-off) from the extracted facts plus transcript
    detail -- see SOW_SYSTEM_PROMPT. Grounded: sections with nothing
    discussed become "Not discussed in this meeting."
    """
    if not transcript_text.strip():
        return {"sow": _placeholder_doc("Scope of Work (SOW)", meeting_title, meeting_date, _NO_SPEECH_NOTE)}
    doc = _timed_generate_document(
        recorder,
        "generate_sow",
        SOW_SYSTEM_PROMPT,
        SOW_RESPONSE_SCHEMA,
        meeting_title,
        meeting_date,
        attendees,
        facts,
        transcript_text,
        # 15 sections, several of them tables; give it room so a scope-heavy
        # meeting doesn't clip. The MAX_TOKENS retry is the backstop.
        max_output_tokens=16384,
        doc_key="sow",
    )
    return {"sow": doc}


def generate_business_process_flow(
    meeting_title: str,
    meeting_date: str,
    attendees: list[str],
    facts: dict,
    transcript_text: str,
    recorder: Optional[TimingRecorder] = None,
) -> dict:
    """One Gemini call producing a plain-language markdown explanation of how
    this part of the business works today (a short numbered walk-through plus a
    deliberately simple embedded ```mermaid flowchart), plus a proposed-way-of-
    working section with its own flowchart ONLY when the meeting actually
    discussed or demoed one -- see BUSINESS_PROCESS_FLOW_SYSTEM_PROMPT. Grounded;
    `facts["business_processes"]` is the primary input but the prompt also draws
    on current_state / topics_discussed / the transcript.
    """
    if not transcript_text.strip():
        return {"business_process_flow": _placeholder_doc(
            "Business Process Flow", meeting_title, meeting_date, _NO_SPEECH_NOTE
        )}
    doc = _timed_generate_document(
        recorder,
        "generate_business_process_flow",
        BUSINESS_PROCESS_FLOW_SYSTEM_PROMPT,
        BUSINESS_PROCESS_FLOW_RESPONSE_SCHEMA,
        meeting_title,
        meeting_date,
        attendees,
        facts,
        transcript_text,
        max_output_tokens=8192,
        doc_key="business_process_flow",
        allow_mermaid=True,
    )
    return {"business_process_flow": doc}
