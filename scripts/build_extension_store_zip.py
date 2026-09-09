#!/usr/bin/env python3
"""Packages `Admin personal/extension(personal)/` into a Chrome Web Store
upload ZIP -- a separate path from scripts/build_extension_zip.py (the
existing local/dev "Load Unpacked" ZIP), which this script never touches
or overwrites.

Differs from the dev zip in exactly the ways a Store submission requires:
  - Uses manifest.store.json (renamed to manifest.json in the package)
    instead of the dev manifest.json -- no pinned "key" field (the
    Dashboard rejects any upload containing one), no localhost host
    permission/externally_connectable entry, real product name/description
    instead of the old "(Personal)"/local-server wording.
  - Zips file contents at the ROOT of the archive (no wrapping folder) --
    the Store's own convention; the dev zip deliberately wraps everything
    in a folder instead, for the "select the extracted folder" Load
    Unpacked instructions, which would be wrong here.
  - Excludes DESIGN.md (an internal dev note, not part of the shipped
    extension) and both manifest.json/manifest.store.json themselves get
    resolved into the single manifest.json the package actually needs.

Output is gitignored (dist/) -- a build artifact, not source; re-run this
after any change to the shared extension source files or to
manifest.store.json.

Usage:
    python scripts/build_extension_store_zip.py
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "Admin personal" / "extension(personal)"
STORE_MANIFEST = SOURCE_DIR / "manifest.store.json"
OUTPUT_PATH = ROOT / "dist" / "meeting-saathi-store-build.zip"

# Present in SOURCE_DIR but must not be copied into the Store package as-is.
_EXCLUDE_NAMES = {"manifest.json", "manifest.store.json", "DESIGN.md"}


def main() -> None:
    if not SOURCE_DIR.is_dir():
        print(f"Source directory not found: {SOURCE_DIR}", file=sys.stderr)
        sys.exit(1)
    if not STORE_MANIFEST.is_file():
        print(f"Store manifest not found: {STORE_MANIFEST}", file=sys.stderr)
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        # manifest.store.json becomes manifest.json at the archive root --
        # the only file this package's own manifest.json comes from.
        zf.write(STORE_MANIFEST, "manifest.json")
        for path in sorted(SOURCE_DIR.rglob("*")):
            if not path.is_file() or path.name in _EXCLUDE_NAMES:
                continue
            # Root-level arcname, no wrapping folder -- what the Chrome Web
            # Store Developer Dashboard expects to find manifest.json in.
            arcname = path.relative_to(SOURCE_DIR)
            zf.write(path, arcname)

    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
