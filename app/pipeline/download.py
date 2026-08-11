from pathlib import Path

from app.config import settings


def working_dir_for(run_id: str) -> Path:
    path = settings.working_dir / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path
