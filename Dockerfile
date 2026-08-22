# Pinned to bookworm (Debian 12), not the unpinned "slim" tag -- that now
# resolves to trixie (Debian 13), which Playwright 1.49's `--with-deps`
# installer doesn't recognize (confirmed: it falls back to an Ubuntu
# 20.04 package list referencing font packages -- ttf-ubuntu-font-family,
# ttf-unifont -- that don't exist on trixie, failing the build outright).
FROM python:3.10-slim-bookworm

# ffmpeg: shelled out to directly by app/pipeline/diarize.py (not a pip
# package). Rest of the apt deps are what Playwright's Chromium needs to
# actually launch headless on a bare Debian slim image -- `playwright
# install --with-deps` below installs most of them itself, but ffmpeg has
# to be here since Playwright doesn't know about it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installs Chromium plus the remaining system libraries it needs to launch
# headless on this base image -- app/docgen/render_pdf.py's PDF rendering
# depends on a real browser being present, not just the Python package.
RUN playwright install --with-deps chromium

COPY . .

# Shell form so ${PORT:-8420} actually substitutes at container start --
# Railway injects PORT at runtime; falls back to 8420 for local `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8420}"]
