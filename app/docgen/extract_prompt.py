import copy

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

# Extraction runs as TWO focused Gemini calls, merged into one facts.json (see
# engine.extract_meeting_facts()). One big schema measurably dilutes the model's
# recall on the less-glamorous lists (risks / assumptions / dependencies /
# open_questions dropped from 3 each to 1 each when the BA-context fields were
# bolted onto the same call). Two smaller, single-purpose schemas each get the
# model's full attention. Cost is 2 calls instead of 1 -- still one-time, still
# well within the free tier, and document generation stays on-demand.

_NO_INVENTION = (
    "Never invent anything not clearly present in the transcript. If something is "
    "ambiguous, still record it but leave the ambiguous field null (or set a "
    '`status` of "needs_clarification"); never fill a gap with a plausible guess. '
    "Attribute each item to the speaker who said it and the timestamp of that "
    f"segment where the field exists. {LANGUAGE_RULE}"
)

_SPEAKER_LABEL_RULE = (
    "Use each speaker's exact label as it appears in the transcript. Some labels "
    "look like `Unidentified speaker 1` -- copy such a label verbatim (with its "
    "number); never shorten, merge, or replace it with a guessed name."
)

CORE_EXTRACT_SYSTEM_PROMPT = f"""You are the extraction layer for Meeting Saathi. You
are given a full, speaker-labeled, timestamped transcript of a business meeting.

YOUR JOB
Extract a structured, faithful record of what was actually said. This is the ONLY
input to the Minutes, Meeting Analysis, BRD, FRD, User Stories, Acceptance Criteria,
and Business Process Flow -- anything you leave out is lost for good.

COMPLETENESS -- be thorough, not brief:
- Capture EVERY distinct topic, sub-topic, and tangent actually discussed. A
  substantive 60-90 minute meeting usually yields 12-25 `topics_discussed` entries.
- Record every decision, action item, and commitment -- including small or in-passing
  ones. Never merge two distinct items into one line.
- Fill `risks`, `assumptions`, `dependencies`, and `open_questions` with the SAME
  diligence as the rest -- these are routinely under-captured. Anything voiced as a
  concern, a "we're assuming...", a "this needs X first", or an unanswered question
  belongs in one of them.
- Prefer recording a borderline item (ambiguous field null / status
  "needs_clarification") over dropping it. This never overrides the no-invention rule.

FIELD NOTES
- Disambiguate: `action_items` are internal follow-up work with no firm promise;
  `commitments` are an explicit promise made to someone; `decisions` are something the
  group agreed on (not a task or promise).
- On each `requirements` entry fill `rationale` (the business reason the client gave)
  and `acceptance_hint` (any "done when..." / "must be able to..." signal) when the
  transcript provides them; null otherwise.
- `business_processes` is a LIST: one entry per distinct AS-IS process a participant
  walked through (inbound leads, field-visit attendance, tour planning, expense
  approval, back-office follow-up calls are separate processes). Record every step,
  actor, hand-off, and branch outcome actually described -- the full sequence, not a
  4-5 step summary. Mark a step "needs_clarification" rather than guessing a branch.

{_SPEAKER_LABEL_RULE}
{_NO_INVENTION}
"""

VERIFY_EXTRACT_SYSTEM_PROMPT = f"""You are the extraction QA reviewer for Meeting
Saathi. You are given the full transcript of a business meeting AND a draft
structured extraction of it. Your job is to return a MORE COMPLETE and MORE ACCURATE
version of that extraction.

DO:
- Add every topic, decision, action item, commitment, requirement, risk, assumption,
  dependency, and open question that was actually discussed but is missing or
  under-captured in the draft. Re-read the transcript specifically hunting for these
  -- the draft is routinely thin on risks/assumptions/dependencies/open_questions and
  on small/in-passing action items.
- Keep every correct item from the draft. Keep requirement `id` values stable; if you
  add a requirement, give it the next free REQ-n.
- Split a draft requirement that actually bundles two distinct needs into two.
- List in `unsupported_items` the exact text of any draft item you believe the
  transcript does NOT support, so it can be removed. Be conservative here -- only
  flag something clearly unsupported, not merely terse.

Return the full corrected extraction in the same shape as the draft, plus
`unsupported_items`. {_NO_INVENTION}
"""

CONTEXT_EXTRACT_SYSTEM_PROMPT = f"""You are the business-analysis context extractor for
Meeting Saathi. You are given a full, speaker-labeled transcript of a business meeting
whose concrete facts (topics, decisions, requirements, risks, ...) are already being
captured separately.

YOUR JOB
Extract only the higher-level business-analysis context a BA needs to frame a BRD/FRD:
- `meeting_purpose`: one sentence on why this meeting was held; null if not clear.
- `goals`: the business outcomes the client wants (the "why", not the system "what"),
  with the client's stated rationale where given.
- `current_state`: how things work today and where they hurt -- one entry per distinct
  area of the as-is situation, with the pain point if voiced. Capture this even when
  no step-by-step process was walked through.
- `constraints`: hard limits stated -- budget, timeline, technology, policy, staffing,
  existing-tool lock-in. Distinct from assumptions (things taken as true) and risks.
- `systems_and_integrations`: every external system/tool/software named (ERPs,
  accounting, telephony, maps, messaging, existing apps), what it's used for, and
  whether an integration with it was asked for.
- `glossary`: domain terms, acronyms, product names, and jargon a reader outside the
  room wouldn't know, each with a plain-English definition grounded in how it was
  used. Skip a term if you're unsure of its meaning.
- `non_functional_notes`: short phrases on performance, security, reliability,
  availability, scale, usability, offline behaviour, or data retention -- only if
  actually mentioned.

Be thorough but strict: {_NO_INVENTION}
"""

# Gemini structured-output schemas (passed as GenerateContentConfig's
# response_json_schema, with response_mime_type="application/json"). Note the
# UPPERCASE type strings and "nullable": true for optional fields -- Gemini's
# schema is not standard JSON Schema (no `"type": ["string", "null"]` unions).

CORE_EXTRACT_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "attendees": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": (
                "Distinct speaker names/labels who spoke. Internal cross-check only -- the "
                "authoritative attendee list comes from a roster provided outside this extraction."
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
                        "description": "Name of the person responsible, or null if unclear.",
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
                "The first-class list every downstream document (BRD, FRD, User Stories, "
                "Acceptance Criteria) derives from."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING", "description": "Stable id in order of first mention, e.g. REQ-1."},
                    "statement": {"type": "STRING"},
                    "category": {
                        "type": "STRING",
                        "description": "e.g. functional, non_functional, integration, reporting, data, security, other.",
                    },
                    "priority": {"type": "STRING", "nullable": True, "description": "e.g. high, medium, low."},
                    "stakeholder": {
                        "type": "STRING",
                        "nullable": True,
                        "description": "Person/role who raised or owns this requirement.",
                    },
                    "status": {
                        "type": "STRING",
                        "description": '"clear" if actionable without follow-up, else "needs_clarification".',
                    },
                    "rationale": {
                        "type": "STRING",
                        "nullable": True,
                        "description": "The business reason the client gave for wanting this; null if not stated.",
                    },
                    "acceptance_hint": {
                        "type": "STRING",
                        "nullable": True,
                        "description": 'Any "done when..." / "must be able to..." signal; null if none.',
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
        "business_processes": {
            "type": "ARRAY",
            "description": (
                "One entry per distinct AS-IS/current business process a participant actually "
                "walked through. Empty list if no process walkthrough happened."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "process_name": {"type": "STRING"},
                    "steps": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "id": {"type": "STRING", "description": "Stable id, unique within this process, e.g. STEP-1."},
                                "type": {
                                    "type": "STRING",
                                    "description": "One of: start, end, process, decision, approval, system.",
                                },
                                "description": {"type": "STRING"},
                                "actor": {"type": "STRING", "nullable": True},
                                "inputs": {"type": "ARRAY", "items": {"type": "STRING"}},
                                "outputs": {"type": "ARRAY", "items": {"type": "STRING"}},
                                "system_interaction": {"type": "STRING", "nullable": True},
                                "next_step_id": {"type": "STRING", "nullable": True, "description": "For non-decision steps."},
                                "on_yes_step_id": {"type": "STRING", "nullable": True, "description": "Decision steps only."},
                                "on_no_step_id": {"type": "STRING", "nullable": True, "description": "Decision steps only."},
                                "alternate_flow": {"type": "STRING", "nullable": True},
                                "exception_notes": {"type": "STRING", "nullable": True},
                                "status": {"type": "STRING", "description": '"clear" or "needs_clarification".'},
                            },
                            "required": ["id", "type", "description", "status"],
                        },
                    },
                },
                "required": ["process_name", "steps"],
            },
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
        "business_processes",
    ],
}

# The verification pass returns the same shape as the core extraction, plus a
# list of draft items it judged unsupported (for removal). Built from
# CORE_EXTRACT_RESPONSE_SCHEMA so the two never drift.
VERIFY_EXTRACT_RESPONSE_SCHEMA = copy.deepcopy(CORE_EXTRACT_RESPONSE_SCHEMA)
VERIFY_EXTRACT_RESPONSE_SCHEMA["properties"]["unsupported_items"] = {
    "type": "ARRAY",
    "items": {"type": "STRING"},
    "description": "Exact text of any draft item the transcript does not support; empty if none.",
}
VERIFY_EXTRACT_RESPONSE_SCHEMA["required"] = [*CORE_EXTRACT_RESPONSE_SCHEMA["required"], "unsupported_items"]


CONTEXT_EXTRACT_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "meeting_purpose": {
            "type": "STRING",
            "nullable": True,
            "description": "One sentence on why this meeting was held; null if not stated or obvious.",
        },
        "goals": {
            "type": "ARRAY",
            "description": "Business goals/outcomes the client wants (the 'why'), distinct from requirements.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "statement": {"type": "STRING"},
                    "rationale": {"type": "STRING", "nullable": True},
                },
                "required": ["statement"],
            },
        },
        "current_state": {
            "type": "ARRAY",
            "description": "How things work today and where they hurt -- raw material for a BRD Current-State section.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "area": {"type": "STRING", "description": "Which part of the business this describes."},
                    "description": {"type": "STRING", "description": "How it works today."},
                    "pain_point": {"type": "STRING", "nullable": True, "description": "The problem with it, if voiced."},
                },
                "required": ["area", "description"],
            },
        },
        "constraints": {
            "type": "ARRAY",
            "description": "Hard limits stated in the meeting (budget, timeline, technology, policy, staffing).",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "description": {"type": "STRING"},
                    "type": {"type": "STRING", "nullable": True, "description": "e.g. budget, timeline, technical, policy, resource."},
                    "timestamp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["description"],
            },
        },
        "systems_and_integrations": {
            "type": "ARRAY",
            "description": "External systems/tools named, what they're used for, and whether an integration was requested.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "purpose": {"type": "STRING", "nullable": True},
                    "integration_need": {"type": "STRING", "nullable": True},
                },
                "required": ["name"],
            },
        },
        "glossary": {
            "type": "ARRAY",
            "description": "Domain terms/acronyms/product names used in the meeting, with plain-English definitions.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "term": {"type": "STRING"},
                    "definition": {"type": "STRING"},
                },
                "required": ["term", "definition"],
            },
        },
        "non_functional_notes": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Short phrases on performance/security/reliability/scale/usability, only if mentioned.",
        },
    },
    "required": [
        "meeting_purpose",
        "goals",
        "current_state",
        "constraints",
        "systems_and_integrations",
        "glossary",
        "non_functional_notes",
    ],
}
