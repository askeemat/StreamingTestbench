"""Media library: scans the media directory and keeps per-file indexes in memory."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .media import MediaIndex, index_media

log = logging.getLogger(__name__)

SCAN_MIN_INTERVAL = 5.0


# Required layout: <media>/<codec>/<resolution>/<hdr>/<id>.<ext>
CODEC_DIRS = {"av1": "av1", "hevc": "hevc", "vp9": "vp9"}  # directory name -> ffprobe codec_name
HDR_DIRS = ("hdr", "sdr")
RESOLUTION_RE = re.compile(r"^([1-9][0-9]{1,4})x([1-9][0-9]{1,4})$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
LAYOUT = "<codec>/<resolution>/<hdr>/<id>.<ext>"


@dataclass
class Layout:
    codec: str
    resolution: str
    width: int
    height: int
    hdr: str
    id: str

    @property
    def key(self) -> str:
        """Stream key: unique per <codec>/<resolution>/<hdr>/<id>. Used in URLs."""
        return f"{self.codec}/{self.resolution}/{self.hdr}/{self.id}"


def parse_layout(rel: str) -> tuple[Layout | None, str]:
    """Parse a path relative to the media directory. Returns (layout, error)."""
    parts = rel.split("/")
    if len(parts) != 4:
        return None, f"Path must be {LAYOUT}"
    codec, resolution, hdr, filename = parts
    if codec not in CODEC_DIRS:
        return None, f"Unknown codec directory '{codec}' (expected: {', '.join(CODEC_DIRS)})"
    m = RESOLUTION_RE.match(resolution)
    if not m:
        return None, f"Invalid resolution directory '{resolution}' (expected e.g. 3840x2160)"
    if hdr not in HDR_DIRS:
        return None, f"Invalid HDR directory '{hdr}' (expected: hdr or sdr)"
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in settings.extensions:
        return None, f"Unsupported extension '{ext}' (expected: {' '.join(settings.extensions)})"
    if not ID_RE.match(stem):
        return None, f"Invalid ID '{stem}' (allowed characters: A-Z a-z 0-9 _ -)"
    return Layout(codec, resolution, int(m.group(1)), int(m.group(2)), hdr, stem), ""


def layout_mismatches(layout: Layout, idx: MediaIndex) -> list[str]:
    """Compare the directory metadata with the probed stream."""
    out: list[str] = []
    codec = idx.video_info.get("codec", "")
    if CODEC_DIRS[layout.codec] != codec:
        out.append(f"Directory says codec '{layout.codec}', but the video stream is '{codec}'.")
    w, h = idx.video_info.get("width", 0), idx.video_info.get("height", 0)
    if (w, h) != (layout.width, layout.height):
        out.append(f"Directory says resolution {layout.resolution}, but the video stream is {w}x{h}.")
    is_hdr = idx.hdr.get("format", "SDR") != "SDR"
    if (layout.hdr == "hdr") != is_hdr:
        out.append(f"Directory says '{layout.hdr}', but the video stream is {idx.hdr.get('format')}.")
    return out


@dataclass
class Entry:
    key: str  # <codec>/<resolution>/<hdr>/<id>
    path: Path
    rel: str
    size: int
    mtime_ns: int
    layout: Layout
    status: str = "pending"  # pending | indexing | ready | error
    error: str = ""
    index: MediaIndex | None = None
    mismatches: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def id(self) -> str:
        return self.layout.id

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def sort_key(self) -> tuple:
        lo = self.layout
        return (lo.codec, -lo.height, -lo.width, lo.hdr, self.id.lower())

    def is_stale(self) -> bool:
        return self.index is not None and (self.index.size != self.size or self.index.mtime_ns != self.mtime_ns)


@dataclass
class Ignored:
    rel: str
    reason: str


class Library:
    def __init__(self, root: Path):
        self.root = root
        self.entries: dict[str, Entry] = {}
        self.ignored: list[Ignored] = []
        self.last_scan = 0.0
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    # -- scanning ---------------------------------------------------------

    def _walk(self) -> list[tuple[Path, str, os.stat_result]]:
        files = []
        if not self.root.is_dir():
            return files
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=True):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((p, p.relative_to(self.root).as_posix(), st))
        return files

    def scan(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_scan < SCAN_MIN_INTERVAL:
            return
        self.last_scan = now
        ignored: list[Ignored] = []
        by_key: dict[str, list[tuple[Path, str, os.stat_result, Layout]]] = {}
        for p, rel, st in self._walk():
            layout, reason = parse_layout(rel)
            if layout is None:
                ignored.append(Ignored(rel, reason))
                continue
            by_key.setdefault(layout.key, []).append((p, rel, st, layout))

        found: dict[str, tuple[Path, str, os.stat_result, Layout]] = {}
        for key, items in by_key.items():
            if len(items) > 1:
                # Same ID in the same <codec>/<resolution>/<hdr> directory (different extensions).
                others = ", ".join(i[1] for i in items)
                for _, rel, _, lo in items:
                    ignored.append(Ignored(rel, f"Duplicate ID '{lo.id}' in {key.rsplit('/', 1)[0]} (used by: {others})"))
                continue
            found[key] = items[0]
        self.ignored = sorted(ignored, key=lambda i: i.rel)

        for key in list(self.entries):
            if key not in found or self.entries[key].path != found[key][0]:
                del self.entries[key]
        for key, (p, rel, st, layout) in found.items():
            e = self.entries.get(key)
            if e is None:
                e = Entry(key, p, rel, st.st_size, st.st_mtime_ns, layout)
                self.entries[key] = e
                if settings.preindex:
                    self._queue.put_nowait(key)
            elif e.size != st.st_size or e.mtime_ns != st.st_mtime_ns:
                e.size, e.mtime_ns = st.st_size, st.st_mtime_ns
                e.status, e.error = "pending", ""
                if settings.preindex:
                    self._queue.put_nowait(key)

    def sorted_entries(self) -> list[Entry]:
        return sorted(self.entries.values(), key=lambda e: e.sort_key)

    def get(self, key: str) -> Entry | None:
        e = self.entries.get(key)
        if e is None:
            self.scan(force=True)
            e = self.entries.get(key)
        return e

    # -- indexing ---------------------------------------------------------

    async def ensure_index(self, entry: Entry, force: bool = False) -> MediaIndex:
        async with entry.lock:
            if entry.index is not None and not force and not entry.is_stale():
                return entry.index
            entry.status, entry.error = "indexing", ""
            try:
                idx = await index_media(entry.path)
            except Exception as exc:  # noqa: BLE001 - reported in the UI
                log.exception("Indexing failed: %s", entry.path)
                entry.status, entry.error, entry.index = "error", str(exc), None
                raise
            entry.index, entry.status = idx, "ready"
            entry.mismatches = layout_mismatches(entry.layout, idx)
            log.info("Indexed %s in %.2fs (%d video segments)", entry.rel, idx.index_seconds,
                     len(idx.video.segments))
            return idx

    def enqueue(self, key: str) -> None:
        self._queue.put_nowait(key)

    async def _work(self) -> None:
        while True:
            key = await self._queue.get()
            e = self.entries.get(key)
            if e is None or (e.status == "ready" and not e.is_stale()):
                continue
            try:
                await self.ensure_index(e, force=e.status == "ready")
            except Exception:  # noqa: BLE001
                pass

    async def _periodic_scan(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                self.scan(force=True)
            except Exception:  # noqa: BLE001
                log.exception("Scan failed")

    def start(self) -> None:
        self.scan(force=True)
        self._worker = asyncio.create_task(self._work())
        self._scanner = asyncio.create_task(self._periodic_scan())

    def counts(self) -> dict[str, int]:
        c = {"pending": 0, "indexing": 0, "ready": 0, "error": 0}
        for e in self.entries.values():
            c[e.status] = c.get(e.status, 0) + 1
        return c
