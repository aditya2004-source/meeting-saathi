from app.pipeline.diarize import SpeakerSegment
from app.pipeline.speaker_names import (
    SpeakerEvent,
    fill_unresolved_with_excerpts,
    is_placeholder_speaker,
    parse_speaker_events,
    resolve_speaker_names,
    speaker_from_dom_events,
)
from app.pipeline.transcribe import TranscribedSegment


def test_parse_speaker_events_happy_path():
    raw = '[{"name": "Priya Shah", "t_seconds": 12.5}, {"name": "Rahul", "t_seconds": 40}]'

    events = parse_speaker_events(raw)

    assert events == [SpeakerEvent("Priya Shah", 12.5), SpeakerEvent("Rahul", 40.0)]


def test_parse_speaker_events_filters_you():
    raw = '[{"name": "You", "t_seconds": 1.0}, {"name": "you", "t_seconds": 2.0}]'

    assert parse_speaker_events(raw) == []


def test_parse_speaker_events_malformed_json_returns_empty():
    assert parse_speaker_events("not json") == []
    assert parse_speaker_events('{"not": "a list"}') == []
    assert parse_speaker_events('[{"name": null, "t_seconds": 1}]') == []
    assert parse_speaker_events('[{"name": "Priya", "t_seconds": "oops"}]') == []


def test_resolve_speaker_names_no_events_is_a_noop():
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1", text="hi")]

    assert resolve_speaker_names(segments, []) == segments


def test_resolve_speaker_names_majority_vote_picks_correct_name():
    segments = [
        SpeakerSegment(start=0.0, end=2.0, speaker="Speaker 1", text="a"),
        SpeakerSegment(start=5.0, end=7.0, speaker="Speaker 1", text="b"),
    ]
    events = [
        SpeakerEvent("Priya Shah", 1.0),
        SpeakerEvent("Priya Shah", 6.0),
        SpeakerEvent("Rahul", 6.2),
    ]

    resolved = resolve_speaker_names(segments, events)

    assert [s.speaker for s in resolved] == ["Priya Shah", "Priya Shah"]
    assert [s.text for s in resolved] == ["a", "b"]


def test_resolve_speaker_names_below_confidence_falls_back_to_placeholder():
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1", text="a")]
    # Tied 1-1 vote -> winner's share is 0.5, which passes the default
    # min_confidence of 0.5; use a stricter threshold to force a fallback.
    events = [SpeakerEvent("Priya Shah", 0.5), SpeakerEvent("Rahul", 0.6)]

    resolved = resolve_speaker_names(segments, events, min_confidence=0.75)

    assert resolved[0].speaker == "Speaker 1"


def test_resolve_speaker_names_respects_tolerance_window():
    segments = [SpeakerSegment(start=10.0, end=12.0, speaker="Speaker 1", text="a")]
    events = [SpeakerEvent("Priya Shah", 12.5)]  # 0.5s past the segment end

    within_tolerance = resolve_speaker_names(segments, events, tolerance_seconds=1.0)
    outside_tolerance = resolve_speaker_names(segments, events, tolerance_seconds=0.1)

    assert within_tolerance[0].speaker == "Priya Shah"
    assert outside_tolerance[0].speaker == "Speaker 1"


def test_resolve_speaker_names_never_touches_unknown():
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Unknown", text="a")]
    events = [SpeakerEvent("Priya Shah", 0.5)]

    resolved = resolve_speaker_names(segments, events)

    assert resolved[0].speaker == "Unknown"


def test_resolve_speaker_names_collision_keeps_stronger_group_only():
    segments = [
        SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1", text="a"),
        SpeakerSegment(start=10.0, end=11.0, speaker="Speaker 2", text="b"),
    ]
    events = [
        # Speaker 1's group: 3 events, all "Priya Shah" -> strong signal.
        SpeakerEvent("Priya Shah", 0.2),
        SpeakerEvent("Priya Shah", 0.4),
        SpeakerEvent("Priya Shah", 0.6),
        # Speaker 2's group: 1 event, also "Priya Shah" -> weaker signal,
        # but a plausible name collision (e.g. mis-clustering).
        SpeakerEvent("Priya Shah", 10.2),
    ]

    resolved = resolve_speaker_names(segments, events)

    assert resolved[0].speaker == "Priya Shah"
    assert resolved[1].speaker == "Speaker 2"


def test_resolve_speaker_names_never_touches_already_real_names():
    # The chunked/streaming pipeline's real input: most segments already
    # carry a real DOM-resolved name from the fast path, only one chunk's
    # segments are still a placeholder (from the rare pyannote-fallback
    # branch). A real name must never be folded into a "group" and re-voted
    # on just because it's technically not "Unknown".
    segments = [
        SpeakerSegment(start=0.0, end=1.0, speaker="Priya Shah", text="already named"),
        SpeakerSegment(start=10.0, end=11.0, speaker="Speaker 1 (chunk@10.0)", text="fallback chunk"),
    ]
    events = [
        SpeakerEvent("Rahul", 0.5),  # would outvote "Priya Shah" if she were grouped -- must not apply
        SpeakerEvent("Rahul", 10.2),
    ]

    resolved = resolve_speaker_names(segments, events)

    assert resolved[0].speaker == "Priya Shah"
    assert resolved[1].speaker == "Rahul"


def test_is_placeholder_speaker():
    assert is_placeholder_speaker("Unknown")
    assert is_placeholder_speaker("Speaker 1")
    assert is_placeholder_speaker("Speaker 12 (chunk@123.4)")
    assert not is_placeholder_speaker("Priya Shah")
    assert not is_placeholder_speaker('Unidentified speaker ("hello")')


def test_speaker_from_dom_events_stale_event_becomes_unknown():
    transcribed = [TranscribedSegment(start=0.0, end=2.0, text="hello")]
    events = [SpeakerEvent("Priya Shah", 100.0)]  # 400s before this segment

    segments = speaker_from_dom_events(transcribed, events, offset=500.0, max_staleness_seconds=300.0)

    assert segments[0].speaker == "Unknown"


def test_speaker_from_dom_events_fresh_event_within_staleness_cap_is_used():
    transcribed = [TranscribedSegment(start=0.0, end=2.0, text="hello")]
    events = [SpeakerEvent("Priya Shah", 400.0)]  # 100s before this segment

    segments = speaker_from_dom_events(transcribed, events, offset=500.0, max_staleness_seconds=300.0)

    assert segments[0].speaker == "Priya Shah"


def test_fill_unresolved_with_excerpts_groups_same_placeholder_one_label():
    segments = [
        SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1", text="hello everyone"),
        SpeakerSegment(start=2.0, end=3.0, speaker="Speaker 1", text="let's get started"),
        SpeakerSegment(start=5.0, end=6.0, speaker="Priya Shah", text="already resolved"),
    ]

    filled = fill_unresolved_with_excerpts(segments)

    assert filled[0].speaker == filled[1].speaker
    assert filled[0].speaker.startswith('Unidentified speaker (')
    assert "hello everyone" in filled[0].speaker
    assert "let's get started" in filled[0].speaker
    assert filled[2].speaker == "Priya Shah"


def test_fill_unresolved_with_excerpts_each_unknown_gets_own_label():
    segments = [
        SpeakerSegment(start=0.0, end=1.0, speaker="Unknown", text="first unknown line"),
        SpeakerSegment(start=5.0, end=6.0, speaker="Unknown", text="second unknown line"),
    ]

    filled = fill_unresolved_with_excerpts(segments)

    assert filled[0].speaker != filled[1].speaker
    assert "first unknown line" in filled[0].speaker
    assert "second unknown line" in filled[1].speaker


def test_fill_unresolved_with_excerpts_no_placeholders_is_a_noop():
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Priya Shah", text="hi")]

    assert fill_unresolved_with_excerpts(segments) == segments


def test_fill_unresolved_with_excerpts_truncates_long_excerpt():
    long_text = "x" * 500
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1", text=long_text)]

    filled = fill_unresolved_with_excerpts(segments)

    assert len(filled[0].speaker) < 200
    assert filled[0].speaker.endswith('...")')
