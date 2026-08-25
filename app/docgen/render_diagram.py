"""Business Process Flow: a pure, deterministic renderer that turns
facts["business_process"] (see app/docgen/extract_prompt.py's EXTRACT_RESPONSE_SCHEMA)
into a Mermaid flowchart, plus a Playwright-based Mermaid -> PDF renderer.

Deliberately NOT a second Gemini call -- the "never invent a step" grounding rule
lives entirely in the extraction prompt/schema (Call 1); turning already-extracted,
already-grounded structured data into diagram syntax is a mechanical, deterministic
transformation, so there's no risk of the model inventing a step *or* producing
invalid Mermaid syntax mid-diagram, and it costs nothing extra in Gemini quota (same
tier as render_tables.py's render_frd_markdown()).
"""
import os
import re
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.docgen.render_pdf import _RENDER_LOCK

# Vendored locally (app/web/static/vendor/mermaid.min.js) rather than loaded from a
# CDN -- consistent with this project's "runs locally, Gemini is the only external
# API" philosophy (see docs/ARCHITECTURE.md), and avoids this render depending on
# outbound network access at all.
_MERMAID_JS_PATH = Path(__file__).resolve().parent.parent / "web" / "static" / "vendor" / "mermaid.min.js"

_NEEDS_CLARIFICATION_PREFIX = "⚠ Needs Clarification: "

_SHAPE_OPEN_CLOSE = {
    "start": ('(["', '"])'),
    "end": ('(["', '"])'),
    "process": ('["', '"]'),
    "decision": ('{"', '"}'),
    "approval": ('[/"', '"/]'),
    "system": ('[["', '"]]'),
}


def _sanitize_node_id(raw_id: str) -> str:
    """Mermaid node ids must be alphanumeric/underscore and can't start with a
    digit -- a step's own extracted id (e.g. "STEP-1") isn't safe to use verbatim.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw_id or "")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"n_{cleaned}"
    return cleaned


def _escape_label(text: str) -> str:
    """Mermaid node labels here are always double-quoted strings -- neutralize
    characters that would otherwise break out of the quotes or the node's own
    bracket syntax.
    """
    text = (text or "").replace('"', "'").replace("\n", " ").strip()
    return text or "(no description)"


def render_business_process_mermaid(process_name: str, steps: list[dict]) -> str:
    """Pure function, no I/O, no Gemini call -- see module docstring. Any step with
    status == "needs_clarification" gets a distinct dashed/warning style and a
    "Needs Clarification" label prefix, so an unclear part of the process is visually
    impossible to miss rather than silently rendered as if it were confirmed. An
    `alternate_flow`/`exception_notes` value, where present, becomes a dashed side
    note attached to its step rather than silently folding into the main path.
    """
    lines = ["flowchart TD"]
    if process_name:
        lines.append(f"    %% {_escape_label(process_name)}")

    node_ids = {(step.get("id") or ""): _sanitize_node_id(step.get("id") or "") for step in steps}

    clarification_node_ids: list[str] = []
    side_note_counter = 0

    for step in steps:
        raw_id = step.get("id") or ""
        node_id = node_ids.get(raw_id, _sanitize_node_id(raw_id))
        step_type = (step.get("type") or "process").lower()
        open_bracket, close_bracket = _SHAPE_OPEN_CLOSE.get(step_type, _SHAPE_OPEN_CLOSE["process"])

        label = step.get("description") or ""
        actor = step.get("actor")
        if actor:
            label = f"{label}<br/>({actor})"
        needs_clarification = (step.get("status") or "clear") == "needs_clarification"
        if needs_clarification:
            label = f"{_NEEDS_CLARIFICATION_PREFIX}{label}"
            clarification_node_ids.append(node_id)

        lines.append(f"    {node_id}{open_bracket}{_escape_label(label)}{close_bracket}")

        if step_type == "decision":
            on_yes = step.get("on_yes_step_id")
            on_no = step.get("on_no_step_id")
            if on_yes:
                lines.append(f"    {node_id} -->|Yes| {node_ids.get(on_yes, _sanitize_node_id(on_yes))}")
            if on_no:
                lines.append(f"    {node_id} -->|No| {node_ids.get(on_no, _sanitize_node_id(on_no))}")
        else:
            next_id = step.get("next_step_id")
            if next_id:
                lines.append(f"    {node_id} --> {node_ids.get(next_id, _sanitize_node_id(next_id))}")

        for field, tag in (("alternate_flow", "Alternate"), ("exception_notes", "Exception")):
            value = step.get(field)
            if value:
                side_note_counter += 1
                note_id = f"{node_id}_note{side_note_counter}"
                lines.append(f'    {note_id}["{tag}: {_escape_label(value)}"]')
                lines.append(f"    {node_id} -.-> {note_id}")
                lines.append(f"    class {note_id} sideNote")

    lines.append(
        "    classDef needsClarification stroke-dasharray: 5 5,stroke:#d97706,"
        "stroke-width:2px,color:#92400e,fill:#fffbeb;"
    )
    lines.append("    classDef sideNote stroke-dasharray: 3 3,stroke:#6b7280,color:#374151,fill:#f9fafb;")
    if clarification_node_ids:
        lines.append(f"    class {','.join(clarification_node_ids)} needsClarification")

    return "\n".join(lines)


_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<script src="{mermaid_js_url}"></script>
<style>
body {{ margin: 0; padding: 24px; background: #ffffff; }}
svg {{ display: block; }}
</style>
</head><body><div id="target"></div></body></html>"""


def render_mermaid_to_pdf(mermaid_source: str, dest_path: Path) -> Path:
    """Renders a Mermaid flowchart to PDF via the vendored mermaid.js (no CDN
    dependency) + Playwright/headless Chrome -- the same rendering machinery
    render_pdf.py already uses for markdown documents, reusing its process-wide
    _RENDER_LOCK since Playwright's sync API isn't safe to call concurrently from
    multiple threads in one process (confirmed the hard way once already for
    markdown_to_pdf(); the same constraint applies here).

    The PDF page size is computed dynamically from the rendered diagram's actual
    bounding box rather than a fixed page size -- a flow with many steps/branches can
    render very tall or wide, and a naive fixed size would clip it. Capped so one
    pathologically large diagram can't produce an unusable multi-hundred-inch PDF.
    """
    html = _HTML_TEMPLATE.format(mermaid_js_url=_MERMAID_JS_PATH.resolve().as_uri())
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=dest_path.parent, suffix=".pdf")
    os.close(fd)
    tmp_path = Path(tmp_name)
    # Written to a real temp .html file and loaded via page.goto("file://...")
    # rather than page.set_content() -- a page loaded with set_content() has no
    # real origin (about:blank), and headless Chrome silently refuses to load a
    # <script src="file://..."> from a non-file:// page (confirmed: the script
    # never ran, `mermaid` stayed undefined, wait_for_function() timed out).
    # Giving the HTML document itself a file:// origin, in the same directory
    # as the vendored script, avoids that restriction entirely.
    html_fd, html_tmp_name = tempfile.mkstemp(dir=dest_path.parent, suffix=".html")
    os.close(html_fd)
    html_tmp_path = Path(html_tmp_name)
    html_tmp_path.write_text(html, encoding="utf-8")
    try:
        with _RENDER_LOCK, sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome")
            except Exception:
                browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(html_tmp_path.resolve().as_uri())
            page.wait_for_function("typeof mermaid !== 'undefined'")
            box = page.evaluate(
                """async (source) => {
                    mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' });
                    const { svg } = await mermaid.render('bpfDiagram', source);
                    document.getElementById('target').innerHTML = svg;
                    const svgEl = document.querySelector('svg');
                    svgEl.removeAttribute('width');
                    svgEl.removeAttribute('height');
                    svgEl.style.maxWidth = 'none';
                    const bbox = svgEl.getBBox();
                    return { width: bbox.width, height: bbox.height };
                }""",
                mermaid_source,
            )
            # 96 CSS px/inch; +96 accounts for the body's own 24px padding on every
            # side (48px each dimension) plus a small buffer so nothing renders
            # flush against the page edge.
            width_in = min(max((box["width"] + 96) / 96, 4), 60)
            height_in = min(max((box["height"] + 96) / 96, 4), 200)
            page.pdf(
                path=str(tmp_path),
                width=f"{width_in}in",
                height=f"{height_in}in",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
            browser.close()
        os.replace(tmp_path, dest_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        html_tmp_path.unlink(missing_ok=True)
    return dest_path
