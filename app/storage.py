import datetime
import os
import re
import tempfile
from pathlib import Path

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")


def sanitize_name(name: str, max_length: int = 120) -> str:
    cleaned = _ILLEGAL_CHARS.sub(" ", name)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if not cleaned:
        cleaned = "Untitled Meeting"
    return cleaned[:max_length].rstrip()


def folder_name(title: str, when: datetime.datetime) -> str:
    return f"{sanitize_name(title)} - {when.strftime('%Y-%m-%d %H%M')}"


def create_meeting_folder(base_dir: Path, title: str, when: datetime.datetime) -> Path:
    """Creates and returns the *final* target folder for a meeting
    immediately -- named "<Title> - <YYYY-MM-DD HHmm>", suffixed "(2)",
    "(3)", ... on a name collision. Unlike the old all-in-one
    save_meeting_folder() below, the folder exists and is visible (to the
    dashboard, the download route) right away, before any file has been
    written into it -- see write_meeting_file() for how individual files
    get added into it safely as they become ready, instead of the whole
    meeting only ever appearing once every document is done.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    base_name = folder_name(title, when)
    target = base_dir / base_name
    suffix = 2
    while target.exists():
        target = base_dir / f"{base_name} ({suffix})"
        suffix += 1
    target.mkdir(parents=True)
    return target


def write_meeting_file(folder: Path, filename: str, content: bytes | str) -> None:
    """Writes one file into an already-created meeting folder (see
    create_meeting_folder()) -- atomically for that *one* file (a temp file
    in the same folder, then os.replace() into place), so a crash mid-write
    can't leave a corrupt/truncated file behind. Writing one file never
    blocks any other file already present -- or yet to come -- from being
    visible, which is the whole point versus the old whole-folder
    atomicity: a meeting folder with only MOM.pdf in it while Meeting
    Analysis is still generating is a normal, expected interim state now,
    not a bug.
    """
    file_path = folder / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=file_path.parent)
    tmp_path = Path(tmp_name)
    try:
        if isinstance(content, bytes):
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        os.replace(tmp_path, file_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def save_meeting_folder(base_dir: Path, title: str, when: datetime.datetime, files: dict[str, bytes | str]) -> Path:
    """Back-compat, all-at-once wrapper: creates the folder and writes every
    file into it via write_meeting_file(). New code that wants files to
    become visible as each one is ready (rather than all at the end)
    should call create_meeting_folder() once up front and
    write_meeting_file() per file instead of this.
    """
    folder = create_meeting_folder(base_dir, title, when)
    for filename, content in files.items():
        write_meeting_file(folder, filename, content)
    return folder
