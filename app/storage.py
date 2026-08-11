import datetime
import re
import shutil
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


def save_meeting_folder(base_dir: Path, title: str, when: datetime.datetime, files: dict[str, bytes | str]) -> Path:
    """Atomically write `files` (relative filename -> bytes/str content) into
    a new folder under base_dir named "<Title> - <YYYY-MM-DD HHmm>". Writes to
    a temp dir first, then renames into place, so a crash mid-write never
    leaves a half-written folder where a completed one is expected.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    final_path = base_dir / folder_name(title, when)

    # mkdtemp (not TemporaryDirectory) because we move this directory into
    # place below; TemporaryDirectory would then try to rmtree a path that
    # no longer exists at context-exit.
    tmp_path = Path(tempfile.mkdtemp(dir=base_dir))
    try:
        for filename, content in files.items():
            file_path = tmp_path / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                file_path.write_bytes(content)
            else:
                file_path.write_text(content, encoding="utf-8")

        target = final_path
        suffix = 2
        while target.exists():
            target = base_dir / f"{final_path.name} ({suffix})"
            suffix += 1
        shutil.move(str(tmp_path), str(target))
    except Exception:
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise

    return target
