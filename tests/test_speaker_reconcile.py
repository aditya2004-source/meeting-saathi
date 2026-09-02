from unittest.mock import patch

from app.pipeline.diarize import SpeakerSegment
from app.pipeline.speaker_reconcile import (
    apply_reconciliation,
    build_label_map,
    distinct_unidentified_labels,
    is_unidentified_label,
    merge_real_names,
    placeholder_dominance,
)


def _seg(speaker, text, start=0.0):
    return SpeakerSegment(start=start, end=start + 1.0, speaker=speaker, text=text)


def test_is_unidentified_label():
    assert is_unidentified_label("Unidentified speaker 1")
    assert is_unidentified_label("Unidentified speaker 138")
    assert not is_unidentified_label("Speaker 1")
    assert not is_unidentified_label("Mustafa")
    assert not is_unidentified_label("Participant 2 (client)")


def test_distinct_unidentified_labels_in_first_appearance_order():
    segments = [
        _seg("Unidentified speaker 3", "c"),
        _seg("Mustafa", "hi"),
        _seg("Unidentified speaker 1", "a"),
        _seg("Unidentified speaker 3", "c again"),
    ]
    assert distinct_unidentified_labels(segments) == ["Unidentified speaker 3", "Unidentified speaker 1"]


def test_placeholder_dominance_by_character_count():
    segments = [_seg("Mustafa", "x" * 10), _seg("Unidentified speaker 1", "y" * 90)]
    assert placeholder_dominance(segments) == 0.9
    assert placeholder_dominance([]) == 0.0
    assert placeholder_dominance([_seg("Mustafa", "hello")]) == 0.0


def test_build_label_map_inverts_participants_and_filters_junk():
    reconciliation = {
        "participants": [
            {"canonical_label": "Mustafa", "speaker_numbers": [1, 99]},  # 99 not in transcript
            {"canonical_label": "", "speaker_numbers": [2]},  # empty target
            {"canonical_label": "Participant 3", "speaker_numbers": [3, "x"]},  # junk number ignored
            "not a dict",
        ]
    }
    known = ["Unidentified speaker 1", "Unidentified speaker 2", "Unidentified speaker 3"]
    assert build_label_map(reconciliation, known) == {
        "Unidentified speaker 1": "Mustafa",
        "Unidentified speaker 3": "Participant 3",
    }


def test_apply_reconciliation_remaps_segments_and_rekeys_excerpts():
    segments = [
        _seg("Unidentified speaker 1", "hello", 0.0),
        _seg("Unidentified speaker 2", "reply", 1.0),
        _seg("Unidentified speaker 3", "another", 2.0),
        _seg("Mustafa", "already named", 3.0),
    ]
    reconciliation = {
        "participants": [
            {"canonical_label": "Dhaval", "is_real_name": True, "speaker_numbers": [1]},
            {"canonical_label": "Participant 2", "is_real_name": False, "speaker_numbers": [2, 3]},
        ],
    }
    excerpts = {
        "Unidentified speaker 1": "hello there",
        "Unidentified speaker 2": "the reply",
        "Unidentified speaker 3": "another line",
    }

    new_segments, new_excerpts, real_names = apply_reconciliation(segments, reconciliation, excerpts)

    assert [s.speaker for s in new_segments] == ["Dhaval", "Participant 2", "Participant 2", "Mustafa"]
    # real name drops its excerpt; the two anonymous fragments merge, first quote wins
    assert new_excerpts == {"Participant 2": "the reply"}
    assert real_names == ["Dhaval"]


def test_apply_reconciliation_unmapped_label_keeps_original():
    segments = [_seg("Unidentified speaker 1", "a"), _seg("Unidentified speaker 2", "b")]
    reconciliation = {
        "participants": [{"canonical_label": "Anil", "is_real_name": True, "speaker_numbers": [1]}],
    }
    new_segments, _, real_names = apply_reconciliation(segments, reconciliation, {})
    assert [s.speaker for s in new_segments] == ["Anil", "Unidentified speaker 2"]
    assert real_names == ["Anil"]


def test_merge_real_names_dedupes_on_bare_name():
    assert merge_real_names(["Mustafa"], ["Mustafa (Imdadi BuildMart)", "Dhaval"]) == [
        "Mustafa",
        "Dhaval",
    ]
    assert merge_real_names([], ["Aditya"]) == ["Aditya"]


def test_maybe_reconcile_below_threshold_is_a_noop():
    from app import reconcile

    segments = [_seg("Mustafa", "x" * 100), _seg("Unidentified speaker 1", "y" * 10)]
    with patch("app.reconcile.docgen_engine.reconcile_speaker_identities") as mock_call:
        out_segments, out_excerpts, out_attendees = reconcile.maybe_reconcile_speakers(
            "Meeting", segments, {}, ["Mustafa"], ""
        )
    mock_call.assert_not_called()
    assert out_segments == segments
    assert out_attendees == ["Mustafa"]


def test_maybe_reconcile_fires_when_placeholders_dominate():
    from app import reconcile

    segments = [_seg(f"Unidentified speaker {i}", "word " * 20, float(i)) for i in range(1, 9)]
    fake = {
        "participants": [
            {"canonical_label": "Priya", "is_real_name": True, "speaker_numbers": list(range(1, 9))}
        ],
    }
    with patch("app.reconcile.docgen_engine.reconcile_speaker_identities", return_value=fake) as mock_call:
        out_segments, _, out_attendees = reconcile.maybe_reconcile_speakers("Meeting", segments, {}, [], "")
    mock_call.assert_called_once()
    assert {s.speaker for s in out_segments} == {"Priya"}
    assert out_attendees == ["Priya"]


def test_maybe_reconcile_swallows_gemini_failure():
    from app import reconcile

    segments = [_seg(f"Unidentified speaker {i}", "word " * 20, float(i)) for i in range(1, 9)]
    with patch(
        "app.reconcile.docgen_engine.reconcile_speaker_identities", side_effect=RuntimeError("boom")
    ):
        out_segments, out_excerpts, out_attendees = reconcile.maybe_reconcile_speakers(
            "Meeting", segments, {"Unidentified speaker 1": "q"}, ["x"], ""
        )
    assert out_segments == segments
    assert out_attendees == ["x"]
