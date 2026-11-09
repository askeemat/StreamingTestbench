"""FastAPI application: DASH endpoints, stream list and admin UI."""

from __future__ import annotations

import logging
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import mpd, packager
from .config import settings
from .library import LAYOUT, Entry, Library
from .media import MediaIndex, generate_segment
from .stats import RequestRecord, request_log

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("streaming_testbench")

library = Library(settings.media_dir)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STARTED = time.time()


def _ffmpeg_version() -> str:
    try:
        out = subprocess.run([settings.ffmpeg, "-version"], capture_output=True, text=True, timeout=5).stdout
        return out.splitlines()[0] if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "not found"


FFMPEG_VERSION = _ffmpeg_version()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Media directory: %s", settings.media_dir)
    library.start()
    yield


app = FastAPI(title="Streaming Testbench", lifespan=lifespan)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_basic = HTTPBasic(auto_error=False)


def require_admin(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    if not settings.admin_password:
        return
    ok = credentials is not None and secrets.compare_digest(
        credentials.username.encode(), settings.admin_user.encode()) and secrets.compare_digest(
        credentials.password.encode(), settings.admin_password.encode())
    if not ok:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})


def base_url(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url
    return str(request.base_url).rstrip("/")


def manifest_url(request: Request, entry: Entry) -> str:
    return f"{base_url(request)}/dash/{entry.key}/manifest.mpd"


def _client(request: Request) -> str:
    return request.client.host if request.client else "-"


# Streams are addressed by their path in the media directory (without extension):
# <codec>/<resolution>/<hdr>/<id>
STREAM = "{codec}/{resolution}/{hdr}/{sid}"


def _entry_or_404(codec: str, resolution: str, hdr: str, sid: str) -> Entry:
    key = f"{codec}/{resolution}/{hdr}/{sid}"
    entry = library.get(key)
    if entry is None:
        raise HTTPException(404, f"Stream {key} not found")
    return entry


async def _index_or_error(entry: Entry) -> MediaIndex:
    try:
        return await library.ensure_index(entry)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Cannot index {entry.rel}: {exc}") from exc


DASH_HEADERS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"}


def _bytes_response(request: Request, data: bytes, media_type: str) -> Response:
    """Return bytes, honouring a single HTTP Range request if present."""
    headers = dict(DASH_HEADERS)
    headers["Accept-Ranges"] = "bytes"
    rng = request.headers.get("range")
    if rng and rng.startswith("bytes=") and "," not in rng:
        start_s, _, end_s = rng[6:].partition("-")
        size = len(data)
        try:
            if start_s:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
            else:
                start = max(0, size - int(end_s))
                end = size - 1
        except ValueError:
            start, end = 0, size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(data[start:end + 1], status_code=206, media_type=media_type, headers=headers)
    return Response(data, media_type=media_type, headers=headers)


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


templates.env.filters["duration"] = _fmt_duration
templates.env.filters["filesize"] = _fmt_size
templates.env.filters["mbps"] = lambda bps: f"{(bps or 0) / 1e6:.1f} Mbps"
templates.env.filters["datetime"] = lambda ts: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
templates.env.filters["clock"] = lambda ts: time.strftime("%H:%M:%S", time.localtime(ts))


def entry_summary(request: Request, e: Entry) -> dict:
    lo = e.layout
    d: dict = {"key": e.key, "id": e.id, "name": e.name, "path": e.rel, "size": e.size, "status": e.status,
               "error": e.error, "manifest": manifest_url(request, e),
               "layout": {"codec": lo.codec, "resolution": lo.resolution, "hdr": lo.hdr},
               "mismatches": e.mismatches}
    idx = e.index
    if idx is not None:
        d.update({
            "duration": idx.duration,
            "video": idx.video_info,
            "audio": idx.audio_info,
            "hdr": idx.hdr,
            "gop": idx.gop,
            "avg_bitrate": idx.avg_bitrate,
            "peak_bitrate": idx.peak_bitrate,
            "warnings": e.mismatches + idx.warnings,
            "index_seconds": idx.index_seconds,
        })
    return d


# --------------------------------------------------------------------------
# DASH endpoints
# --------------------------------------------------------------------------

@app.api_route(f"/dash/{STREAM}/manifest.mpd", methods=["GET", "HEAD"])
async def dash_manifest(codec: str, resolution: str, hdr: str, sid: str, request: Request):
    started = time.perf_counter()
    entry = _entry_or_404(codec, resolution, hdr, sid)
    idx = await _index_or_error(entry)
    body = mpd.build_mpd(idx, title=entry.name).encode()
    request_log.add(RequestRecord(time.time(), _client(request), request.headers.get("user-agent", ""),
                                  entry.key, entry.name, "manifest", 0, 200, len(body),
                                  (time.perf_counter() - started) * 1000, 0.0))
    return Response(body, media_type="application/dash+xml", headers=DASH_HEADERS)


@app.api_route(f"/dash/{STREAM}/{{kind}}/init.mp4", methods=["GET", "HEAD"])
async def dash_init(codec: str, resolution: str, hdr: str, sid: str, kind: str, request: Request):
    entry = _entry_or_404(codec, resolution, hdr, sid)
    idx = await _index_or_error(entry)
    track = idx.video if kind == "video" else idx.audio if kind == "audio" else None
    if track is None:
        raise HTTPException(404, "Track not found")
    request_log.add(RequestRecord(time.time(), _client(request), request.headers.get("user-agent", ""),
                                  entry.key, entry.name, "init", 0, 200, len(track.init), 0.0, 0.0))
    return _bytes_response(request, track.init, f"{kind}/mp4")


@app.api_route(f"/dash/{STREAM}/{{kind}}/{{number}}.m4s", methods=["GET", "HEAD"])
async def dash_segment(codec: str, resolution: str, hdr: str, sid: str, kind: str, number: int,
                       request: Request):
    if kind not in ("video", "audio"):
        raise HTTPException(404, "Track not found")
    started = time.perf_counter()
    entry = _entry_or_404(codec, resolution, hdr, sid)
    idx = await _index_or_error(entry)
    ua = request.headers.get("user-agent", "")
    try:
        res = await generate_segment(idx, kind, number)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Segment generation failed: %s %s #%d", entry.rel, kind, number)
        request_log.add(RequestRecord(time.time(), _client(request), ua, entry.key, entry.name, kind, number, 500, 0,
                                      (time.perf_counter() - started) * 1000, 0.0, error=str(exc)))
        raise HTTPException(500, f"Segment generation failed: {exc}") from exc
    gen_ms = (time.perf_counter() - started) * 1000
    seg_dur = res.segment.d / res.track.timescale
    request_log.add(RequestRecord(time.time(), _client(request), ua, entry.key, entry.name, kind, number, 200,
                                  len(res.data), gen_ms, seg_dur, res.extracted.attempts,
                                  res.extracted.verified))
    return _bytes_response(request, res.data, f"{kind}/mp4")


# --------------------------------------------------------------------------
# Stream list (public)
# --------------------------------------------------------------------------

def _group_key(e: Entry) -> tuple[str, str, str]:
    return (e.layout.codec, e.layout.resolution, e.layout.hdr)


@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    library.scan()
    groups: list[dict] = []
    for e in library.sorted_entries():
        key = _group_key(e)
        if not groups or groups[-1]["key"] != key:
            codec, res, hdr = key
            groups.append({"key": key, "codec": codec, "resolution": res, "hdr": hdr, "entries": [],
                           "playlist": f"{base_url(request)}/playlist/{codec}/{res}/{hdr}.m3u"})
        groups[-1]["entries"].append(entry_summary(request, e))
    return templates.TemplateResponse(request, "index.html", {
        "groups": groups, "count": len(library.entries), "ignored": len(library.ignored),
        "base_url": base_url(request), "settings": settings,
        "playlist_url": f"{base_url(request)}/playlist.m3u",
    })


def _playlist(request: Request, codec: str = "all", resolution: str = "all", hdr: str = "all") -> Response:
    library.scan()
    lines = ["#EXTM3U"]
    for e in library.sorted_entries():
        lo = e.layout
        if e.status == "error" or any(f not in ("all", v) for f, v in
                                      ((codec, lo.codec), (resolution, lo.resolution), (hdr, lo.hdr))):
            continue
        dur = int(e.index.duration) if e.index else -1
        lines.append(f"#EXTINF:{dur},{e.id} [{lo.codec.upper()} {lo.resolution} {lo.hdr.upper()}]")
        lines.append(manifest_url(request, e))
    return PlainTextResponse("\n".join(lines) + "\n", media_type="audio/x-mpegurl",
                             headers={"Content-Disposition": 'inline; filename="playlist.m3u"'})


# Playlists mirror the directory layout; "all" matches any value.
@app.get("/playlist.m3u")
async def playlist_all(request: Request):
    return _playlist(request)


@app.get("/playlist/{codec}.m3u")
async def playlist_codec(request: Request, codec: str):
    return _playlist(request, codec)


@app.get("/playlist/{codec}/{resolution}.m3u")
async def playlist_codec_res(request: Request, codec: str, resolution: str):
    return _playlist(request, codec, resolution)


@app.get("/playlist/{codec}/{resolution}/{hdr}.m3u")
async def playlist_codec_res_hdr(request: Request, codec: str, resolution: str, hdr: str):
    return _playlist(request, codec, resolution, hdr)


@app.get(f"/streams/{STREAM}", response_class=HTMLResponse)
async def stream_page(codec: str, resolution: str, hdr: str, sid: str, request: Request):
    entry = _entry_or_404(codec, resolution, hdr, sid)
    if entry.index is None and entry.status != "error":
        try:
            await library.ensure_index(entry)
        except Exception:  # noqa: BLE001 - shown on the page
            pass
    idx = entry.index
    segments = []
    if idx is not None:
        ts = idx.video.timescale
        segments = [{"number": s.number, "start": s.t / ts, "duration": s.d / ts, "frames": s.packet_count,
                     "size": s.size, "bitrate": s.bitrate(ts)} for s in idx.video.segments]
    return templates.TemplateResponse(request, "stream.html", {
        "e": entry_summary(request, entry), "segments": segments,
    })


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Admin UI / API
# --------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_page(request: Request):
    library.scan()
    return templates.TemplateResponse(request, "admin.html", {
        "settings": settings, "counts": library.counts(), "ffmpeg_version": FFMPEG_VERSION,
        "started": STARTED, "uptime": time.time() - STARTED, "base_url": base_url(request),
        "entries": [entry_summary(request, e) for e in library.sorted_entries()],
        "ignored": library.ignored, "layout": LAYOUT,
    })


@app.post("/admin/rescan", dependencies=[Depends(require_admin)])
async def admin_rescan():
    library.scan(force=True)
    for e in library.entries.values():
        if e.status in ("pending", "error"):
            library.enqueue(e.key)
    return RedirectResponse("/admin", status_code=303)


@app.post(f"/admin/reindex/{STREAM}", dependencies=[Depends(require_admin)])
async def admin_reindex(codec: str, resolution: str, hdr: str, sid: str):
    entry = _entry_or_404(codec, resolution, hdr, sid)
    entry.status = "pending"
    entry.index = None
    library.enqueue(entry.key)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/clear-log", dependencies=[Depends(require_admin)])
async def admin_clear_log():
    request_log.clear()
    return RedirectResponse("/admin", status_code=303)


@app.get("/api/streams")
async def api_streams(request: Request):
    library.scan()
    return [entry_summary(request, e) for e in library.sorted_entries()]


@app.get(f"/api/streams/{STREAM}")
async def api_stream(codec: str, resolution: str, hdr: str, sid: str, request: Request):
    return entry_summary(request, _entry_or_404(codec, resolution, hdr, sid))


@app.get("/api/status", dependencies=[Depends(require_admin)])
async def api_status(limit: int = 100):
    now = time.time()
    return JSONResponse({
        "uptime": now - STARTED,
        "library": library.counts(),
        "jobs": [{"label": j["label"], "elapsed": now - j["started"]} for j in packager.active_jobs.values()],
        "summary": request_log.summary(),
        "requests": [r.as_dict() for r in request_log.recent(limit)],
    })
