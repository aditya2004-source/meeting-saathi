#!/usr/bin/env python3
"""Packages `Admin personal/extension(personal)/` into the zip the website's
/install page offers for download (app/web/static/meeting-saathi-extension.zip).

Re-run this after any change to the extension folder and commit the
resulting zip -- there's no build/deploy pipeline yet (Phase 9) to do this
automatically. Migrating to Chrome Web Store distribution later just means
swapping the /install page's download link for a store link; this script
and the manual zip stay as the interim distribution path until then.

Usage:
    python scripts/build_extension_zip.py
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "Admin personal" / "extension(personal)"
OUTPUT_PATH = ROOT / "app" / "web" / "static" / "meeting-saathi-extension.zip"


def main() -> None:
    if not SOURCE_DIR.is_dir():
        print(f"Source directory not found: {SOURCE_DIR}", file=sys.stderr)
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(SOURCE_DIR.rglob("*")):
            if path.is_file():
                # Zipped with a top-level "meeting-saathi-extension/" folder,
                # so extracting doesn't dump loose files into the user's
                # Downloads folder -- matches the install guide's "select
                # the extracted folder" step.
                arcname = Path("meeting-saathi-extension") / path.relative_to(SOURCE_DIR)
                zf.write(path, arcname)

    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
