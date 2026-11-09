"""Runtime configuration (environment variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    media_dir: Path = field(default_factory=lambda: Path(os.environ.get("MEDIA_DIR", "/media")))
    # Target segment duration in seconds. Actual segments are cut at
    # keyframes, so they are at least this long (unless the file ends).
    segment_duration: float = float(os.environ.get("SEGMENT_DURATION", "4"))
    ffmpeg: str = os.environ.get("FFMPEG", "ffmpeg")
    ffprobe: str = os.environ.get("FFPROBE", "ffprobe")
    max_jobs: int = int(os.environ.get("MAX_CONCURRENT_JOBS", "4"))
    preindex: bool = _env_bool("PREINDEX", True)
    enable_audio: bool = _env_bool("ENABLE_AUDIO", True)
    admin_user: str = os.environ.get("ADMIN_USER", "admin")
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
    public_base_url: str = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    request_log_size: int = int(os.environ.get("REQUEST_LOG_SIZE", "1000"))
    extensions: tuple[str, ...] = tuple(
        e.strip().lower() for e in os.environ.get(
            "MEDIA_EXTENSIONS", ".mp4,.m4v,.mov,.mkv,.webm,.ts,.m2ts").split(",") if e.strip()
    )


settings = Settings()
