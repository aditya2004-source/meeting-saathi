# Shared across extract_prompt.py and generate_prompt.py -- every prompt
# that touches transcript content needs this, since the transcript itself
# may still carry non-English text through despite whisper_task="translate"
# (e.g. the AssemblyAI fallback path has no translate mode -- see
# app/pipeline/diarize.py's language_detection=True comment).
LANGUAGE_RULE = (
    "Always write the document in clear, simple English. If any transcript "
    "content is in Hindi or another language, translate it to plain English -- "
    "never copy non-English text verbatim into the output, except for direct "
    "proper nouns/names."
)

EXTRACT_SYSTEM_PROMPT = f"""You are the extraction layer for Meeting Saathi. You
are given a full, speaker-labeled, timestamped transcript of a business meeting.

YOUR JOB
Read the transcript and extract a structured summary of what was actually said.
This extraction is grounding data for later, on-demand steps that write formal
documents (Minutes of Meeting, Meeting Analysis, a Business Requirements Document,
a Functional Requirements Document, User Stories, Acceptance Criteria) and render a
Business Process Flow diagram -- so it must be complete, accurate, and strictly
faithful to the transcript.

RULES
- Never invent a name, date, decision, action item, requirement, risk, assumption,
  dependency, open question, commitment, or business-process step that is not
  clearly present in the transcript. If something is ambiguous (e.g. an action item
  with no clear owner), still record it, but leave the ambiguous field null (or, for
  a requirement/business-process step, mark its `status` as "needs_clarification")
  rather than guessing.
- Use each speaker's exact label as it appears in the transcript (e.g. "Priya Shah").
  Some labels look like `Unidentified speaker 1`, `Unidentified speaker 2`, etc. --
  this means their real name could not be identified; copy that entire label exactly
  as it appears, verbatim (including its number), do not shorten it, merge two
  different numbers together, or invent a real name for it. Never rename or merge
  speakers.
- Attribute each decision, action item, and key quote to the specific speaker who
  said it, using the timestamp of the segment it came from.
- If the meeting produced no decisions, or no action items, or no clear topics,
  return an empty list for that field rather than fabricating content to fill it.
- Keep quotes short (one sentence) and verbatim from the transcript text.
- Disambiguate `action_items`, `commitments`, and `decisions`, which can look
  similar: `action_items` are internal follow-up work with no firm promise attached
  ("someone should look into X"); `commitments` are an explicit promise made *to
  someone* ("we will have this ready by Friday"); `decisions` are something the
  group agreed on, not a task or a promise.
- For `business_process`: only include this field if a participant actually walked
  through how a business process works today (an AS-IS process) -- if no such
  walkthrough happened, leave it null rather than inventing one. Only record a step,
  actor, or branch outcome (e.g. what happens on "No") that was actually described.
  If the transcript doesn't state what happens on a particular branch, or a step's
  actor/order is ambiguous, mark that step's `status` as "needs_clarification" rather
  than assuming a typical/standard process fills the gap -- never complete the flow
  with a plausible-sounding guess.
- {LANGUAGE_RULE}
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
            "description": (
                "Distinct speaker names/labels who spoke during the meeting. Used only "
                "as an internal cross-check -- the authoritative attendee list for the "
                "final documents comes from a separate roster provided outside this "
                "extraction, not from this field."
            ),
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
        "requirements": {
            "type": "ARRAY",
            "description": (
                "The first-class list every downstream document (BRD, FRD, User "
                "Stories, Acceptance Criteria) derives from."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {
                        "type": "STRING",
                        "description": "Stable id assigned in order of first mention, e.g. REQ-1, REQ-2.",
                    },
                    "statement": {"type": "STRING"},
                    "category": {
                        "type": "STRING",
                        "description": (
                            "e.g. functional, non_functional, integration, reporting, data, security, other."
                        ),
                    },
                    "priority": {"type": "STRING", "nullable": True, "description": "e.g. high, medium, low."},
                    "stakeholder": {
                        "type": "STRING",
                        "nullable": True,
                        "description": "Person/role who raised or owns this requirement.",
                    },
                    "status": {
                        "type": "STRING",
                        "description": (
                            '"clear" if the transcript gave enough detail to act on without follow-up, '
                            'else "needs_clarification".'
                        ),
                    },
                    "source_speaker": {"type": "STRING", "nullable": True},
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["id", "statement", "category", "status"],
            },
        },
        "risks": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "description": {"type": "STRING"},
                    "impact": {"type": "STRING", "nullable": True},
                    "raised_by": {"type": "STRING", "nullable": True},
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["description"],
            },
        },
        "assumptions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "statement": {"type": "STRING"},
                    "made_by": {"type": "STRING", "nullable": True},
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["statement"],
            },
        },
        "dependencies": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "description": {"type": "STRING"},
                    "depends_on": {"type": "STRING", "nullable": True},
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["description"],
            },
        },
        "open_questions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "raised_by": {"type": "STRING", "nullable": True},
                    "related_requirement_id": {
                        "type": "STRING",
                        "nullable": True,
                        "description": "Links back to requirements[].id, if this question blocks a specific requirement.",
                    },
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["question"],
            },
        },
        "commitments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "commitment": {"type": "STRING"},
                    "committed_by": {"type": "STRING", "nullable": True},
                    "committed_to": {"type": "STRING", "nullable": True},
                    "due_date_mentioned": {"type": "STRING", "nullable": True},
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["commitment"],
            },
        },
        "business_process": {
            "type": "OBJECT",
            "nullable": True,
            "description": (
                "Only present if a participant actually walked through how a business process works "
                "today (AS-IS). Null if no such walkthrough happened in this meeting."
            ),
            "properties": {
                "process_name": {"type": "STRING"},
                "steps": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "id": {"type": "STRING", "description": "Stable id, e.g. STEP-1."},
                            "type": {
                                "type": "STRING",
                                "description": "One of: start, end, process, decision, approval, system.",
                            },
                            "description": {"type": "STRING"},
                            "actor": {"type": "STRING", "nullable": True},
                            "inputs": {"type": "ARRAY", "items": {"type": "STRING"}},
                            "outputs": {"type": "ARRAY", "items": {"type": "STRING"}},
                            "system_interaction": {"type": "STRING", "nullable": True},
                            "next_step_id": {
                                "type": "STRING",
                                "nullable": True,
                                "description": "For non-decision steps.",
                            },
                            "on_yes_step_id": {"type": "STRING", "nullable": True, "description": "Decision steps only."},
                            "on_no_step_id": {"type": "STRING", "nullable": True, "description": "Decision steps only."},
                            "alternate_flow": {"type": "STRING", "nullable": True},
                            "exception_notes": {"type": "STRING", "nullable": True},
                            "status": {
                                "type": "STRING",
                                "description": '"clear" or "needs_clarification".',
                            },
                        },
                        "required": ["id", "type", "description", "status"],
                    },
                },
            },
            "required": ["process_name", "steps"],
        },
    },
    "required": [
        "attendees",
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
        "business_process",
    ],
}
