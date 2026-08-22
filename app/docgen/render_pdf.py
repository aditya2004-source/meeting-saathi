from pathlib import Path
from typing import Optional

import markdown as markdown_lib
from playwright.sync_api import sync_playwright

from app.pipeline.timing import TimingRecorder, timed

_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 800px;
       margin: 2rem auto; line-height: 1.5; color: #1a1a1a; }}
h1, h2, h3 {{ color: #111; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
</style></head><body>{body}</body></html>"""


def markdown_to_pdf(markdown_text: str, dest_path: Path) -> Path:
    html_body = markdown_lib.markdown(markdown_text, extensions=["tables"])
    html = _HTML_TEMPLATE.format(body=html_body)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception:
            # Falls back to Playwright's bundled Chromium if the system
            # Chrome channel isn't registered with Playwright.
            browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        page.pdf(path=str(dest_path), format="A4", margin={"top": "1in", "bottom": "1in", "left": "0.75in", "right": "0.75in"})
        browser.close()
    return dest_path


def render_documents_to_pdf(
    docs: dict,
    mom_pdf_path: Path,
    meeting_analysis_pdf_path: Path,
    recorder: Optional[TimingRecorder] = None,
) -> None:
    """Renders the generated documents to PDF, one at a time.

    Deliberately sequential, not concurrent: Playwright's sync API is not
    safe to call from multiple threads in the same process -- each call
    spins up its own background thread + asyncio event loop to drive the
    Node driver subprocess, and those loops race on Python's process-global
    subprocess child watcher. Running this concurrently (an earlier design,
    back when there were three documents) caused a real crash in
    production: "RuntimeError: Racing with another loop to spawn a
    process." An earlier benchmark that didn't hit the race was never real
    evidence of safety -- concurrency bugs like this are inherently
    intermittent."""

    def _render(stage: str, markdown_text: str, dest_path: Path) -> None:
        if recorder is None:
            markdown_to_pdf(markdown_text, dest_path)
        else:
            with timed(recorder, stage):
                markdown_to_pdf(markdown_text, dest_path)

    jobs = [
        ("render_mom_pdf", docs["mom"]["markdown_body"], mom_pdf_path),
        ("render_meeting_analysis_pdf", docs["meeting_analysis"]["markdown_body"], meeting_analysis_pdf_path),
    ]
    for stage, text, path in jobs:
        _render(stage, text, path)
