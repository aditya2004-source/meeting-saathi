"""Prompt + schema for the speaker-reconciliation Gemini call (see
app/docgen/engine.py:reconcile_speaker_identities() and
app/pipeline/speaker_reconcile.py).

This is a RECOVERY step, not part of normal generation. When the Chrome
extension's active-speaker scrape captured nothing for a meeting, every
chunk falls through to per-chunk diarization and each chunk's clusters get
a globally-unique `chunk@<offset>` tag -- so one real person split across
90 chunks becomes 90 "Unidentified speaker N" labels that
speaker_names.py can never merge (no cross-chunk signal, and the raw audio
is usually already deleted). This pass reads the whole transcript back and
collapses those fragments into the true, small participant set using
conversational continuity and who-addresses-whom -- the only signal left.
"""
from app.docgen.extract_prompt import LANGUAGE_RULE

RECONCILE_SYSTEM_PROMPT = f"""You are the speaker-reconciliation layer for Meeting
Saathi. You are given the full transcript of ONE business meeting in which automatic
speaker diarization failed: it over-segmented the audio, so a single real person is
scattered across many different "Unidentified speaker N" labels.

YOUR JOB
Work out how many real people actually spoke, and map EVERY "Unidentified speaker N"
label in the transcript to one of them.

HOW TO DECIDE WHO IS WHO
- Use conversational continuity: consecutive lines that answer each other, finish a
  sentence, or hold one point of view are usually the same person.
- Use forms of address: when someone is addressed by name ("Mustafa bhai", "Dhaval
  ji", "Aditya"), the person being replied to is very likely that named person.
- Use self-introduction ("this is X from Y", "I'm X").
- Use role/side: a client asking questions vs. a vendor demoing a product; a
  presenter driving the agenda vs. participants reacting.
- Real meetings like this usually have 2-8 distinct speakers, not dozens. Prefer the
  smallest participant set the transcript actually supports.

NAMING RULES -- STRICT
- Assign a real name to a participant ONLY when the transcript unambiguously reveals
  it (they are addressed by that name, or they introduce themselves by it). Never
  guess a name from context, and never invent one.
- When you know a real first name, use it as the canonical label; add a side/company
  in parentheses if the transcript makes it clear (e.g. "Mustafa (Imdadi
  BuildMart)"). Do not attach a parenthetical you are not sure of.
- When you do NOT know a real name, use a stable role label: "Participant 1",
  "Participant 2", ... -- optionally with a side you are confident about
  ("Participant 3 (client side)"). Number them in order of first appearance.
- The local user's own microphone is sometimes its own label; if one label is
  clearly the meeting host/recorder and no name is given, "Participant 1 (host)" is
  fine.

OUTPUT RULES
- Return a `participants` list -- one entry per real person, usually 2-8 entries.
- For each participant, `speaker_numbers` is the list of N values (just the integer
  from "Unidentified speaker N") that are actually that person.
- Every "Unidentified speaker N" in the transcript must appear in exactly ONE
  participant's `speaker_numbers`. Do not drop any; do not list one twice.
- If you genuinely cannot tell who a label is, give it its own "Participant N" entry
  rather than forcing it onto someone else.
- Keep the output compact: no prose, no per-speaker explanations.
- {LANGUAGE_RULE}
"""

RECONCILE_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "participants": {
            "type": "ARRAY",
            "description": "One entry per real person who spoke (usually 2-8).",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "canonical_label": {
                        "type": "STRING",
                        "description": (
                            'A real first name (optionally "Name (Side)"), or "Participant N" '
                            "when no real name is known."
                        ),
                    },
                    "is_real_name": {
                        "type": "BOOLEAN",
                        "description": "True only if canonical_label contains a name the transcript unambiguously revealed.",
                    },
                    "speaker_numbers": {
                        "type": "ARRAY",
                        "items": {"type": "INTEGER"},
                        "description": 'The N values ("Unidentified speaker N") that are this person.',
                    },
                },
                "required": ["canonical_label", "is_real_name", "speaker_numbers"],
            },
        },
    },
    "required": ["participants"],
}
