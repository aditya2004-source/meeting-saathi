import os
import re
import tempfile
import threading
from pathlib import Path

import markdown as markdown_lib
from playwright.sync_api import sync_playwright

_MERMAID_JS_PATH = Path(__file__).resolve().parent.parent / "web" / "static" / "vendor" / "mermaid.min.js"

_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 800px;
       margin: 2rem auto; line-height: 1.5; color: #1a1a1a; }}
h1, h2, h3 {{ color: #111; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
</style></head><body>{body}</body></html>"""

# Same page styling as above, plus the vendored Mermaid runtime and a rule that
# keeps a rendered diagram inside the A4 content width. Used only when the
# markdown actually contains a ```mermaid block (the Business Process Flow doc).
_MERMAID_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<script src="{mermaid_js_url}"></script>
<script>
  // Must run synchronously right after the library loads (before
  // DOMContentLoaded) so Mermaid's own auto-run is disabled -- otherwise it
  // renders every pre.mermaid into an <svg>, and a second render pass then
  // reads that SVG's text (CSS + labels) back as diagram source and fails.
  mermaid.initialize({{ startOnLoad: false, securityLevel: 'loose' }});
</script>
<style>
body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 800px;
       margin: 2rem auto; line-height: 1.5; color: #1a1a1a; }}
h1, h2, h3 {{ color: #111; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
pre.mermaid {{ border: none; background: none; }}
pre.mermaid[data-processed="true"] {{ text-align: center; margin: 1rem 0; }}
pre.mermaid svg {{ max-width: 100%; height: auto; }}
</style></head><body>{body}</body></html>"""

# One pass over every pristine <pre class="mermaid"> node. suppressErrors keeps
# a single malformed diagram from aborting the batch -- Mermaid swaps that one
# node for its own inline error graphic and moves on. Each processed node gets
# data-processed="true", which markdown_to_pdf() waits on before taking the PDF.
_MERMAID_RUN_JS = (
    "async () => { await mermaid.run({ querySelector: 'pre.mermaid', suppressErrors: true }); }"
)

_MERMAID_FENCE_RE = re.compile(r"(?ms)^[ \t]*```mermaid[ \t]*\n(.*?)\n[ \t]*```[ \t]*$")

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


def _launch_browser(p):
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:
        # Falls back to Playwright's bundled Chromium if the system Chrome
        # channel isn't registered with Playwright.
        return p.chromium.launch()


def _extract_mermaid_blocks(markdown_text: str) -> tuple[str, bool]:
    """Swap each ```mermaid fenced block for a raw-HTML <pre class="mermaid">
    node (source HTML-escaped, blank lines around it) so python-markdown passes
    it straight through and the browser's Mermaid can render it. Returns
    (transformed_text, had_any).
    """
    found = False

    def _sub(match: "re.Match") -> str:
        nonlocal found
        found = True
        source = (
            match.group(1)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f'\n\n<pre class="mermaid">\n{source}\n</pre>\n\n'

    return _MERMAID_FENCE_RE.sub(_sub, markdown_text), found


def markdown_to_pdf(markdown_text: str, dest_path: Path) -> Path:
    """Renders straight to a temp file in dest_path's own directory, then
    os.replace()s it into place -- so a reader (the dashboard, the download
    route) can never observe a partially-written PDF at dest_path, only
    either nothing yet or the complete file. Matters more now that a
    document's PDF can become visible to a caller (see
    app/storage.py::write_meeting_file()) before its sibling document has
    even finished generating, rather than only once everything is done.

    If the markdown contains a ```mermaid block, the diagram is rendered by
    the vendored Mermaid runtime inside headless Chrome before the PDF is
    taken. That path writes a temp .html and loads it via file:// (rather
    than set_content()) because Chrome silently refuses a
    <script src="file://..."> from an about:blank origin.
    """
    text, has_mermaid = _extract_mermaid_blocks(markdown_text)
    html_body = markdown_lib.markdown(text, extensions=["tables"])
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=dest_path.parent, suffix=".pdf")
    os.close(fd)
    tmp_path = Path(tmp_name)
    html_tmp_path: Path | None = None
    try:
        with _RENDER_LOCK, sync_playwright() as p:
            browser = _launch_browser(p)
            page = browser.new_page()
            if has_mermaid:
                html = _MERMAID_HTML_TEMPLATE.format(
                    body=html_body, mermaid_js_url=_MERMAID_JS_PATH.resolve().as_uri()
                )
                html_fd, html_name = tempfile.mkstemp(dir=dest_path.parent, suffix=".html")
                os.close(html_fd)
                html_tmp_path = Path(html_name)
                html_tmp_path.write_text(html, encoding="utf-8")
                page.goto(html_tmp_path.resolve().as_uri())
                page.wait_for_function("typeof mermaid !== 'undefined'")
                page.evaluate(_MERMAID_RUN_JS)
                # mermaid.run() marks each node it finished; wait for the SVGs to
                # actually be in the DOM before the PDF snapshot (evaluate resolves
                # when run() returns, but give layout a beat regardless).
                try:
                    page.wait_for_function(
                        "Array.from(document.querySelectorAll('pre.mermaid'))"
                        ".every(el => el.getAttribute('data-processed') === 'true')",
                        timeout=15000,
                    )
                except Exception:
                    pass  # fall through -- a stuck diagram shouldn't lose the whole PDF
                page.wait_for_timeout(200)
            else:
                page.set_content(_HTML_TEMPLATE.format(body=html_body))
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
    finally:
        if html_tmp_path is not None:
            html_tmp_path.unlink(missing_ok=True)
    return dest_path
