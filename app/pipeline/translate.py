"""Guarantees the stored transcript is in English even on the fallback
transcription path.

The DOM-primary and local-Whisper paths decode with faster-whisper
``task="translate"`` (``settings.whisper_task``), so their output is already
English. The AssemblyAI path (``diarize_chunk()``, used when
``settings.assemblyai_api_key`` is set and Meet DOM speaker coverage is
sparse) transcribes in whatever language is spoken -- AssemblyAI has no
``task="translate"`` equivalent -- so Hindi/other-language text would
otherwise land in ``transcript.json`` / ``transcript.txt`` verbatim.

This module runs one Gemini pass over just the non-English segments,
translating them in place while keeping every segment's start/end/speaker.
Fail-soft: any error (or a disabled ``whisper_task``) leaves the segments
exactly as they were -- the generated documents are still produced in
English regardless, because the docgen prompts translate too; this is only
about the transcript the user reads/keeps.

Known gap: detection is script-based, so a segment AssemblyAI happens to
return as Roman-script Hindi ("chal se", "haan theek hai") is not caught.
The common case -- AssemblyAI writing Hindi in Devanagari -- is.
"""
import re
import traceback
from dataclasses import replace
from typing import Optional

from app.config import settings
from app.docgen import engine as docgen_engine
from app.pipeline.diarize import SpeakerSegment
from app.pipeline.timing import TimingRecorder, timed

# One character from a non-Latin script we expect from Indian-language speech
# marks a segment as needing translation: Arabic/Urdu (U+0600-06FF, U+0750-077F)
# and the contiguous Indic block U+0900-U+0DFF (Devanagari, Gurmukhi, Gujarati,
# Bengali, Tamil, Telugu, Kannada, Malayalam, Oriya, Sinhala).
_NON_LATIN_RE = re.compile("[؀-ۿݐ-ݿऀ-෿]")


def segment_needs_translation(text: str) -> bool:
    return bool(_NON_LATIN_RE.search(text or ""))


def apply_translations(
    segments: list[SpeakerSegment], translations: dict[int, str]
) -> list[SpeakerSegment]:
    """Pure: return a new segment list with segment i's text replaced by
    translations[i] where present and non-empty; every other segment is
    passed through unchanged."""
    out = []
    for i, seg in enumerate(segments):
        new_text = (translations.get(i) or "").strip()
        out.append(replace(seg, text=new_text) if new_text else seg)
    return out


def maybe_translate_segments_to_english(
    segments: list[SpeakerSegment], recorder: Optional[TimingRecorder] = None
) -> list[SpeakerSegment]:
    """Translate any non-English segment to English via one Gemini call.
    No-op (and zero Gemini calls) when translation is disabled, there are no
    segments, or every segment is already Latin-script. Never raises."""
    if settings.whisper_task != "translate" or not segments:
        return segments

    to_translate = [i for i, seg in enumerate(segments) if segment_needs_translation(seg.text)]
    if not to_translate:
        return segments

    try:
        items = [{"index": i, "text": segments[i].text} for i in to_translate]
        if recorder is None:
            result = docgen_engine.translate_transcript_segments(items)
        else:
            with timed(recorder, "translate_transcript"):
                result = docgen_engine.translate_transcript_segments(items)
        translations = {
            int(item["index"]): item.get("text") or ""
            for item in (result.get("translations") or [])
            if isinstance(item, dict) and "index" in item
        }
    except Exception:  # noqa: BLE001 - fail-soft, must never fail the pipeline
        traceback.print_exc()
        return segments

    return apply_translations(segments, translations)
