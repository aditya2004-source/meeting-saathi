from app.pipeline.speaker_names import SpeakerEvent, dom_coverage_ratio, speaker_from_dom_events
from app.pipeline.transcribe import TranscribedSegment


def test_dom_coverage_ratio_no_events_is_zero():
    assert dom_coverage_ratio([], window_start=0.0, window_end=50.0) == 0.0


def test_dom_coverage_ratio_events_outside_window_is_zero():
    events = [SpeakerEvent("Priya", 1000.0)]
    assert dom_coverage_ratio(events, window_start=0.0, window_end=50.0) == 0.0


def test_dom_coverage_ratio_one_event_at_window_start_is_full_coverage():
    events = [SpeakerEvent("Priya", 0.0)]
    assert dom_coverage_ratio(events, window_start=0.0, window_end=50.0) == 1.0


def test_dom_coverage_ratio_event_before_window_still_covers_from_window_start():
    # The event predates this chunk (it belongs to a prior chunk's speaker
    # turn), but its attribution should still carry forward into this
    # window until a newer event (or the window ends) -- this is the
    # cross-chunk-boundary case that matters most in practice.
    events = [SpeakerEvent("Priya", -10.0)]
    assert dom_coverage_ratio(events, window_start=0.0, window_end=50.0) == 1.0


def test_dom_coverage_ratio_event_partway_through_window_gives_partial_coverage():
    events = [SpeakerEvent("Priya", 25.0)]
    ratio = dom_coverage_ratio(events, window_start=0.0, window_end=50.0)
    assert ratio == 0.5


def test_dom_coverage_ratio_clamped_to_one():
    events = [SpeakerEvent("Priya", -100.0), SpeakerEvent("Rahul", -50.0)]
    assert dom_coverage_ratio(events, window_start=0.0, window_end=50.0) == 1.0


def test_dom_coverage_ratio_zero_duration_window_is_zero():
    events = [SpeakerEvent("Priya", 0.0)]
    assert dom_coverage_ratio(events, window_start=10.0, window_end=10.0) == 0.0


def test_speaker_from_dom_events_assigns_nearest_preceding_event():
    transcribed = [
        TranscribedSegment(start=0.0, end=2.0, text="hello"),
        TranscribedSegment(start=6.0, end=8.0, text="world"),
    ]
    events = [SpeakerEvent("Priya Shah", -1.0), SpeakerEvent("Rahul", 5.0)]

    segments = speaker_from_dom_events(transcribed, events, offset=0.0)

    assert [s.speaker for s in segments] == ["Priya Shah", "Rahul"]
    assert [s.text for s in segments] == ["hello", "world"]


def test_speaker_from_dom_events_no_preceding_event_is_unknown():
    transcribed = [TranscribedSegment(start=0.0, end=2.0, text="hello")]
    events = [SpeakerEvent("Priya Shah", 5.0)]  # only after the segment

    segments = speaker_from_dom_events(transcribed, events, offset=0.0)

    assert segments[0].speaker == "Unknown"


def test_speaker_from_dom_events_skips_empty_text_segments():
    transcribed = [TranscribedSegment(start=0.0, end=1.0, text="")]
    events = [SpeakerEvent("Priya Shah", 0.0)]

    assert speaker_from_dom_events(transcribed, events, offset=0.0) == []


def test_speaker_from_dom_events_applies_global_offset():
    transcribed = [TranscribedSegment(start=0.0, end=2.0, text="hello")]
    events = [SpeakerEvent("Priya Shah", 100.0)]  # global time, this chunk starts at t=100

    segments = speaker_from_dom_events(transcribed, events, offset=100.0)

    assert segments[0].start == 100.0
    assert segments[0].end == 102.0
    assert segments[0].speaker == "Priya Shah"
