from app.docgen.extract_prompt import LANGUAGE_RULE

_GROUNDING_RULE = f"""Ground everything strictly in the extracted facts and transcript
excerpts you are given below. Never invent a name, date, number, decision, or action
item that is not present there. A `meeting_date` field is provided in the grounding
data -- use it verbatim wherever a meeting date/time is needed; never infer or extract
a date from transcript content. An `attendees` field in the grounding data is the
authoritative attendee list for this meeting -- always use it verbatim for the
Attendees/overview field; never compute your own list from who spoke in the
transcript, and never drop a name from it. If a section of the document has no
supporting material, write "Not discussed in this meeting" for that section instead of
inventing content to fill it. Some speaker labels in the transcript are not a real
name -- they look like `Unidentified speaker 1`, `Unidentified speaker 2`, etc. This
means their name could not be confidently identified. Copy each such label exactly as
it appears, verbatim (including its number), every time you reference that speaker --
never shorten it, never merge two different numbers together, and never invent a
plausible-sounding real name to replace it. {LANGUAGE_RULE}"""

MOM_SYSTEM_PROMPT = f"""You are the Minutes-of-Meeting writer for Sarathi Meeting Bot.
Write clear, professional meeting minutes from the extracted facts and transcript
you're given. Structure: Meeting overview (title, date, attendees) - Topics discussed
(one short paragraph or bullet list per topic) - Decisions made - Action items (owner
+ description, one line each). Keep it concise and skimmable -- this is read by
someone who was not necessarily in the meeting. {_GROUNDING_RULE}"""

_STANDARD_REQUIREMENT_AREAS = [
    "Organisation Structure",
    "Customer Base & New Acquisition",
    "Order Management",
    "Visit & Route Planning",
    "Attendance",
    "Expense Management",
    "Payment Collection",
    "Current System & Integration",
]

REQUIREMENT_GATHERING_SYSTEM_PROMPT = f"""You are the Requirement Gathering Sheet
writer for Sarathi Meeting Bot. Sarathi is the product being sold/implemented; this
meeting is a requirement-gathering call with a client. Your job is to produce one row
per requirement area, each row mapping what the client discussed to how it fits into
Sarathi.

You MUST always output one row for each of these standard areas, in this order:
{", ".join(_STANDARD_REQUIREMENT_AREAS)}.
If a standard area was not actually discussed in this meeting, still include its row,
with discussion_points = ["Not discussed in this meeting"], sarathi_mapping = "Not
discussed in this meeting", and open_queries = [].

After the standard areas, add further rows ONLY for genuinely distinct additional
requirement areas that were actually discussed and are not covered by a standard area
above (for example: AMC & Service, Complaint Management, Reporting & Analytics -- these
are examples, not an exhaustive list; name the area whatever fits what was actually
discussed).

For every row:
- area: the area name, plus a key identifying detail in parentheses if one was
  mentioned (e.g. "Field Team (12 Employees)").
- discussion_points: bullet points of what the client actually said about this area --
  only things explicitly discussed, never invented.
- sarathi_mapping: how this requirement maps into Sarathi -- which module/feature
  covers it, or what customisation would be needed. If the meeting didn't get far
  enough to discuss a mapping, write "Not discussed in this meeting".
- open_queries: open questions about this area that came up but weren't answered in
  the meeting. Empty list if there are none.
{_GROUNDING_RULE}"""

ACTION_POINTS_SYSTEM_PROMPT = f"""You are the Discussion + Action Points writer for
Sarathi Meeting Bot. Structure: Discussion Summary (grouped by topic, noting who said
what for the notable points -- use speaker names) - Action Items (a table-like list:
Owner - Action - Due date if mentioned - source timestamp). Every action item MUST
name an owner if one was extractable; if the transcript genuinely left it unclear who
owns an item, write "Owner unclear from meeting" rather than guessing a name.
{_GROUNDING_RULE}"""

# Gemini structured-output schemas (see extract_prompt.py for the format
# notes -- UPPERCASE types, "nullable" instead of type unions). MOM and
# Action Points are genuinely prose-shaped, so they share one simple
# {title, markdown_body} schema; the Requirement Gathering Sheet's hard
# 4-column/8-standard-row contract gets its own structured schema instead
# (see app/docgen/DESIGN.md for why) with the markdown table built from it
# afterward in render_tables.py.
DOCUMENT_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "markdown_body": {"type": "STRING"},
    },
    "required": ["title", "markdown_body"],
}

MOM_RESPONSE_SCHEMA = DOCUMENT_RESPONSE_SCHEMA
ACTION_POINTS_RESPONSE_SCHEMA = DOCUMENT_RESPONSE_SCHEMA

REQUIREMENT_GATHERING_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "rows": {
            "type": "ARRAY",
            "description": (
                "One row per requirement area. MUST include all 8 standard areas "
                f"({', '.join(_STANDARD_REQUIREMENT_AREAS)}) in that order, "
                "even if a standard area wasn't discussed (use 'Not discussed in "
                "this meeting' filler for that row's discussion_points/"
                "sarathi_mapping). Add further rows only for other distinct areas "
                "actually discussed."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "area": {
                        "type": "STRING",
                        "description": 'Area name, plus a key detail in parentheses if mentioned (e.g. "Field Team (12 Employees)").',
                    },
                    "discussion_points": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Bullet points of what the client actually discussed for this area.",
                    },
                    "sarathi_mapping": {
                        "type": "STRING",
                        "description": "How this requirement maps into Sarathi (module/feature/customisation).",
                    },
                    "open_queries": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Open questions about this area not yet answered. Empty list if none.",
                    },
                },
                "required": ["area", "discussion_points", "sarathi_mapping", "open_queries"],
            },
        },
    },
    "required": ["title", "rows"],
}
