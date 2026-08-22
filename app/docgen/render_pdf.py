import os
import tempfile
import threading
from pathlib import Path

import markdown as markdown_lib
from playwright.sync_api import sync_playwright

_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 800px;
       margin: 2rem auto; line-height: 1.5; color: #1a1a1a; }}
h1, h2, h3 {{ color: #111; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
</style></head><body>{body}</body></html>"""

# Playwright's sync API is not safe to call from multiple threads in the
# same process -- each call spins up its own background thread + asyncio
# event loop to drive the Node driver subprocess, and those loops race on
# Python's process-global subprocess child watcher. Concurrent calls (an
# earlier design) caused a real crash in production: "RuntimeError: Racing
# with another loop to spawn a process." Now that MOM/Meeting Analysis are
# each rendered as soon as their own Gemini call completes (see
# app/orchestrator_streaming.py), two render calls can legitimately land on
# different worker threads at nearly the same moment, so this lock is what
# keeps them serialized instead of the old simple sequential for-loop.
_RENDER_LOCK = threading.Lock()


def markdown_to_pdf(markdown_text: str, dest_path: Path) -> Path:
    """Renders straight to a temp file in dest_path's own directory, then
    os.replace()s it into place -- so a reader (the dashboard, the download
    route) can never observe a partially-written PDF at dest_path, only
    either nothing yet or the complete file. Matters more now that a
    document's PDF can become visible to a caller (see
    app/storage.py::write_meeting_file()) before its sibling document has
    even finished generating, rather than only once everything is done.
    """
    html_body = markdown_lib.markdown(markdown_text, extensions=["tables"])
    html = _HTML_TEMPLATE.format(body=html_body)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=dest_path.parent, suffix=".pdf")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with _RENDER_LOCK, sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome")
            except Exception:
                # Falls back to Playwright's bundled Chromium if the system
                # Chrome channel isn't registered with Playwright.
                browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(html)
            page.pdf(
                path=str(tmp_path),
                format="A4",
                margin={"top": "1in", "bottom": "1in", "left": "0.75in", "right": "0.75in"},
            )
            browser.close()
        os.replace(tmp_path, dest_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return dest_path
