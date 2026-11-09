"""On-the-fly packaging using ffmpeg (stream copy, no re-encoding).

Every request spawns one ffmpeg process that remuxes the source around the
requested segment into fragmented MP4 on a pipe. The output is parsed
incrementally and ffmpeg is stopped as soon as all samples of the segment
have been received. Nothing is written to disk.

ffmpeg input seeking (-ss) is not packet accurate for every container
(e.g. Matroska may land on an earlier keyframe), so the caller locates the
wanted samples in the output itself (see media.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import mp4
from .config import settings

log = logging.getLogger(__name__)

MOVFLAGS = "frag_keyframe+empty_moov+default_base_moof+frag_discont"
READ_CHUNK = 1 << 20


class PackagerError(RuntimeError):
    pass


@dataclass
class JobRequest:
    path: Path
    stream_index: int
    kind: str  # "video" | "audio"
    seek: float | None  # seconds, None = start of file
    video_timescale: int | None = None


_semaphore: asyncio.Semaphore | None = None
active_jobs: dict[int, dict] = {}
_job_counter = 0


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.max_jobs))
    return _semaphore


def build_command(req: JobRequest) -> list[str]:
    cmd = [settings.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-copyts"]
    if req.seek is not None and req.seek > 0:
        cmd += ["-ss", f"{req.seek:.6f}"]
    cmd += ["-i", str(req.path), "-map", f"0:{req.stream_index}", "-c", "copy",
            "-map_metadata", "-1", "-map_chapters", "-1", "-avoid_negative_ts", "disabled"]
    if req.kind == "video":
        if req.video_timescale:
            cmd += ["-video_track_timescale", str(req.video_timescale)]
    else:
        # Audio has no keyframe-driven fragmentation; flush every 0.5 s.
        cmd += ["-frag_duration", "500000"]
    cmd += ["-strict", "experimental", "-f", "mp4", "-movflags", MOVFLAGS, "pipe:1"]
    return cmd


async def run_until(req: JobRequest, done: Callable[[mp4.FragmentReader], bool],
                    label: str = "") -> mp4.FragmentReader:
    """Run ffmpeg, parse its output, stop when ``done(reader)`` returns True or at EOF."""
    global _job_counter
    cmd = build_command(req)
    reader = mp4.FragmentReader()
    async with _get_semaphore():
        _job_counter += 1
        job_id = _job_counter
        active_jobs[job_id] = {"label": label, "started": time.time(), "cmd": " ".join(cmd)}
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())
        finished_early = False
        try:
            while True:
                chunk = await proc.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                reader.feed(chunk)
                if reader.fragments and done(reader):
                    finished_early = True
                    break
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            err = await stderr_task
            active_jobs.pop(job_id, None)
    if not finished_early and proc.returncode != 0:
        msg = err.decode(errors="replace").strip()[-2000:]
        raise PackagerError(f"ffmpeg failed (rc={proc.returncode}): {msg}")
    if reader.init is None:
        raise PackagerError("ffmpeg produced no init segment")
    return reader
