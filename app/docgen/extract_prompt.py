EXTRACT_SYSTEM_PROMPT = """You are the extraction layer for Sarathi Meeting Bot. You
are given a full, speaker-labeled, timestamped transcript of a business meeting.

YOUR JOB
Read the transcript and extract a structured summary of what was actually said.
This extraction is grounding data for a later step that writes formal documents
(Minutes of Meeting, a Requirement Gathering Sheet, and an Action Points list) --
so it must be complete, accurate, and strictly faithful to the transcript.

RULES
- Never invent a name, date, decision, or action item that is not clearly present
  in the transcript. If something is ambiguous (e.g. an action item with no clear
  owner), still record it, but leave the owner field null rather than guessing.
- Use each speaker's exact label as it appears in the transcript (e.g. "Priya Shah").
  Some labels look like `Unidentified speaker ("a few words they actually said")` --
  this means their real name could not be identified; copy that entire label exactly
  as it appears, verbatim, do not shorten it or invent a real name for it. Never
  rename or merge speakers.
- Attribute each decision, action item, and key quote to the specific speaker who
  said it, using the timestamp of the segment it came from.
- If the meeting produced no decisions, or no action items, or no clear topics,
  return an empty list for that field rather than fabricating content to fill it.
- Keep quotes short (one sentence) and verbatim from the transcript text.
"""

# Gemini structured-output schema (passed as GenerateContentConfig's
# response_json_schema, with response_mime_type="application/json") --
# forces the model to always return exactly this shape, the same grounding
# guarantee the old Anthropic forced-tool-call design gave. Note the
# UPPERCASE type strings ("OBJECT"/"ARRAY"/"STRING"/"NUMBER") and
# "nullable": true for optional fields -- Gemini's schema is not standard
# JSON Schema (it doesn't support `"type": ["string", "null"]` unions).
EXTRACT_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "attendees": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Distinct speaker names/labels who spoke during the meeting.",
        },
        "topics_discussed": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Short phrases naming the topics/agenda items actually discussed.",
        },
        "decisions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "decision": {"type": "STRING"},
                    "made_by": {"type": "STRING", "nullable": True},
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["decision"],
            },
        },
        "action_items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "description": {"type": "STRING"},
                    "owner": {
                        "type": "STRING",
                        "nullable": True,
                        "description": "Name of the person responsible, or null if unclear from the transcript.",
                    },
                    "due_date_mentioned": {"type": "STRING", "nullable": True},
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["description", "owner"],
            },
        },
        "key_quotes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "speaker": {"type": "STRING"},
                    "quote": {"type": "STRING"},
                    "timestamp": {"type": "NUMBER"},
                },
                "required": ["speaker", "quote", "timestamp"],
            },
        },
    },
    "required": ["attendees", "topics_discussed", "decisions", "action_items", "key_quotes"],
}
