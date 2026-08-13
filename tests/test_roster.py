"""Covers app.pipeline.roster -- parsing the extension's People-panel
attendee_roster.json sidecar and combining it with detected speaker names
into one deterministic attendee list. Added to fix a real client meeting
where 7 people joined but the Attendees list in the generated documents was
missing people who never spoke (the old attendee list was derived purely
from who spoke in the transcript).
"""
from app.pipeline.diarize import SpeakerSegment
from app.pipeline.roster import compute_attendees, parse_attendee_roster


def test_parse_attendee_roster_happy_path_flat_strings():
    raw = '["Priya Shah", "Rahul Verma"]'

    assert parse_attendee_roster(raw) == ["Priya Shah", "Rahul Verma"]


def test_parse_attendee_roster_happy_path_dict_objects():
    raw = '[{"name": "Priya Shah"}, {"name": "Rahul Verma"}]'

    assert parse_attendee_roster(raw) == ["Priya Shah", "Rahul Verma"]


def test_parse_attendee_roster_filters_you_and_dedupes():
    raw = '["You", "you", "Priya Shah", "Priya Shah"]'

    assert parse_attendee_roster(raw) == ["Priya Shah"]


def test_parse_attendee_roster_malformed_json_returns_empty():
    assert parse_attendee_roster("not json") == []
    assert parse_attendee_roster('{"not": "a list"}') == []
    assert parse_attendee_roster('[{"name": null}]') == []
    assert parse_attendee_roster("[42, true, null]") == []


def test_compute_attendees_roster_order_preserved():
    roster = ["Priya Shah", "Rahul Verma", "Silent Person"]
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Priya Shah", text="hi")]

    assert compute_attendees(roster, segments) == ["Priya Shah", "Rahul Verma", "Silent Person"]


def test_compute_attendees_adds_spoken_names_not_on_roster():
    roster = ["Priya Shah"]
    segments = [
        SpeakerSegment(start=0.0, end=1.0, speaker="Priya Shah", text="hi"),
        SpeakerSegment(start=2.0, end=3.0, speaker="External Dial-In", text="hello"),
    ]

    assert compute_attendees(roster, segments) == ["Priya Shah", "External Dial-In"]


def test_compute_attendees_never_includes_unresolved_placeholders():
    roster = ["Priya Shah"]
    segments = [
        SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1", text="hi"),
        SpeakerSegment(start=2.0, end=3.0, speaker="Unknown", text="hello"),
    ]

    assert compute_attendees(roster, segments) == ["Priya Shah"]


def test_compute_attendees_no_roster_falls_back_to_spoken_names_only():
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Priya Shah", text="hi")]

    assert compute_attendees([], segments) == ["Priya Shah"]
