"""Media probing, packet indexing and segment planning.

The index holds only small metadata (packet timestamps / sizes / keyframe
flags, init segments of a few KB). Media segments are never stored.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
from array import array
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import mp4
from .config import settings
from .packager import JobRequest, PackagerError, run_until

log = logging.getLogger(__name__)

VIDEO_CODECS = {"hevc", "vp9", "av1", "h264"}
# Audio codecs that ffmpeg can put into fragmented MP4 by stream copy.
AUDIO_CODECS = {"aac", "opus", "ac3", "eac3", "flac", "mp3", "alac"}


class ProbeError(RuntimeError):
    pass


@dataclass
class Segment:
    number: int  # 1-based (MPD $Number$)
    first_packet: int  # index into the track's packet list
    packet_count: int
    start_pts: int  # pts (source time base) of the first packet
    t: int  # timeline start (track timescale)
    d: int  # timeline duration (track timescale)
    size: int  # sum of packet sizes in bytes

    def duration_s(self, timescale: int) -> float:
        return self.d / timescale

    def bitrate(self, timescale: int) -> float:
        dur = self.d / timescale
        return self.size * 8 / dur if dur > 0 else 0.0


@dataclass
class Track:
    kind: str  # video | audio
    stream_index: int
    codec_name: str
    time_base: Fraction
    timescale: int
    init: bytes
    init_info: mp4.TrackInit
    segments: list[Segment]
    packets: "PacketList"
    candidates: "Candidates"

    @property
    def codecs(self) -> str:
        return self.init_info.codecs


@dataclass
class MediaIndex:
    path: Path
    size: int
    mtime_ns: int
    duration: float
    format_name: str
    video: Track
    audio: Track | None
    video_info: dict
    audio_info: dict | None
    hdr: dict
    gop: dict
    warnings: list[str] = field(default_factory=list)
    index_seconds: float = 0.0
    indexed_at: float = 0.0

    @property
    def peak_bitrate(self) -> float:
        return max((s.bitrate(self.video.timescale) for s in self.video.segments), default=0.0)

    @property
    def avg_bitrate(self) -> float:
        size = sum(s.size for s in self.video.segments)
        dur = sum(s.d for s in self.video.segments) / self.video.timescale
        return size * 8 / dur if dur else 0.0


# --------------------------------------------------------------------------
# ffprobe helpers
# --------------------------------------------------------------------------

async def _run(cmd: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ProbeError(err.decode(errors="replace").strip()[-1000:] or f"{cmd[0]} failed")
    return out


async def probe_streams(path: Path) -> dict:
    out = await _run([settings.ffprobe, "-v", "error", "-show_format", "-show_streams",
                      "-of", "json", str(path)])
    return json.loads(out)


async def probe_first_frame_side_data(path: Path, stream_index: int) -> list[dict]:
    try:
        out = await _run([settings.ffprobe, "-v", "error", "-select_streams", str(stream_index),
                          "-read_intervals", "%+#1", "-show_frames",
                          "-show_entries", "frame=side_data_list", "-of", "json", str(path)])
        frames = json.loads(out).get("frames", [])
        return frames[0].get("side_data_list", []) if frames else []
    except (ProbeError, json.JSONDecodeError, IndexError):
        return []


@dataclass
class PacketList:
    """Per-stream packet metadata in decode order (compact arrays)."""
    pts: array = field(default_factory=lambda: array("q"))
    duration: array = field(default_factory=lambda: array("q"))
    size: array = field(default_factory=lambda: array("q"))
    key: bytearray = field(default_factory=bytearray)


async def probe_packets(path: Path, wanted: set[int]) -> dict[int, PacketList]:
    """Read packet metadata for the wanted streams (demux only, no decoding)."""
    cmd = [settings.ffprobe, "-v", "error", "-show_entries",
           "packet=stream_index,pts,duration,size,flags", "-of", "compact=p=0:nk=0", str(path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 20)
    result = {i: PacketList() for i in wanted}
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        fields = dict(kv.split("=", 1) for kv in line.decode().strip().split("|") if "=" in kv)
        try:
            idx = int(fields["stream_index"])
        except (KeyError, ValueError):
            continue
        pl = result.get(idx)
        if pl is None:
            continue
        pts = fields.get("pts", "N/A")
        if pts == "N/A":
            continue  # packets without pts cannot be placed on the timeline
        dur = fields.get("duration", "N/A")
        flags = fields.get("flags", "")
        pl.pts.append(int(pts))
        pl.duration.append(0 if dur == "N/A" else int(dur))
        pl.size.append(int(fields.get("size", "0") or 0))
        pl.key.append(1 if flags.startswith("K") else 0)
    err = await proc.stderr.read() if proc.stderr else b""
    await proc.wait()
    if proc.returncode != 0:
        raise ProbeError(err.decode(errors="replace").strip()[-1000:] or "ffprobe failed")
    return result


# --------------------------------------------------------------------------
# Segment planning
# --------------------------------------------------------------------------

def _frac(tb: str) -> Fraction:
    num, den = tb.split("/")
    return Fraction(int(num), int(den))


def _rate(r: str | None) -> float:
    try:
        f = _frac(r or "0/1")
        return float(f) if f else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def plan_video_cuts(pkts: PacketList, tb: Fraction, target: float) -> list[tuple[int, int]]:
    """Return (first_packet, packet_count) of each segment, cut at keyframes."""
    keys = [i for i, k in enumerate(pkts.key) if k]
    if not keys:
        raise ProbeError("Video stream has no keyframes")
    starts = [keys[0]]
    for k in keys[1:]:
        if float((pkts.pts[k] - pkts.pts[starts[-1]]) * tb) >= target - 1e-6:
            starts.append(k)
    n = len(pkts.pts)
    return [(s, (starts[i + 1] if i + 1 < len(starts) else n) - s) for i, s in enumerate(starts)]


def build_segments(pkts: PacketList, cuts: list[tuple[int, int]], tb: Fraction, timescale: int,
                   shift: Fraction, end_time: Fraction | None = None) -> list[Segment]:
    def ticks(pts: int) -> int:
        return round((pts * tb + shift) * timescale)

    segs: list[Segment] = []
    for i, (first, count) in enumerate(cuts):
        start_pts = pkts.pts[first]
        t = ticks(start_pts)
        if i + 1 < len(cuts):
            d = ticks(pkts.pts[cuts[i + 1][0]]) - t
        else:
            rng = range(first, first + count)
            end = max(pkts.pts[j] + pkts.duration[j] for j in rng)
            end_ticks = ticks(end)
            if end_time is not None:
                end_ticks = max(end_ticks, round((end_time + shift) * timescale))
            d = end_ticks - t
        size = sum(pkts.size[first:first + count])
        segs.append(Segment(i + 1, first, count, start_pts, t, max(d, 1), size))
    return segs


def plan_audio_cuts(apkts: PacketList, atb: Fraction, boundaries: list[Fraction]) -> list[tuple[int, int]]:
    """Group audio packets so that each segment starts at the video boundaries."""
    cuts: list[tuple[int, int]] = []
    n = len(apkts.pts)
    j = 0
    for b in boundaries[1:] + [None]:
        first = j
        while j < n and (b is None or apkts.pts[j] * atb < b):
            j += 1
        if j > first:
            cuts.append((first, j - first))
    return cuts


def _gop_stats(pkts: PacketList, tb: Fraction) -> dict:
    key_pts = [float(p * tb) for p, k in zip(pkts.pts, pkts.key) if k]
    gaps = [b - a for a, b in zip(key_pts, key_pts[1:])]
    if not gaps:
        return {"keyframes": len(key_pts), "min": 0.0, "max": 0.0, "avg": 0.0}
    return {"keyframes": len(key_pts), "min": min(gaps), "max": max(gaps), "avg": sum(gaps) / len(gaps)}


def _hdr_info(vstream: dict, side_data: list[dict]) -> dict:
    trc = vstream.get("color_transfer", "")
    info: dict = {
        "format": "SDR",
        "color_primaries": vstream.get("color_primaries", "unknown"),
        "color_transfer": trc or "unknown",
        "color_space": vstream.get("color_space", "unknown"),
        "pix_fmt": vstream.get("pix_fmt", ""),
        "mastering_display": None,
        "content_light": None,
        "dolby_vision": None,
    }
    if trc == "smpte2084":
        info["format"] = "HDR10"
    elif trc == "arib-std-b67":
        info["format"] = "HLG"
    for sd in vstream.get("side_data_list", []) + side_data:
        t = sd.get("side_data_type", "")
        if "DOVI" in t or "Dolby Vision" in t:
            info["dolby_vision"] = {k: v for k, v in sd.items() if k != "side_data_type"}
        elif t == "Mastering display metadata":
            info["mastering_display"] = {k: v for k, v in sd.items() if k != "side_data_type"}
        elif t == "Content light level metadata":
            info["content_light"] = {k: v for k, v in sd.items() if k != "side_data_type"}
        elif "SMPTE2094-40" in t or "HDR10+" in t:
            info["format"] = "HDR10+"
    if info["dolby_vision"]:
        info["format"] = f"Dolby Vision ({info['format']} base)" if info["format"] != "SDR" else "Dolby Vision"
    return info


def _pick_streams(streams: list[dict]) -> tuple[dict | None, dict | None, list[str]]:
    warnings: list[str] = []
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    video = videos[0] if videos else None
    if len(videos) > 1:
        warnings.append(f"{len(videos)} video streams found; only stream #{video['index']} is served.")
    audio = None
    if settings.enable_audio and audios:
        usable = [a for a in audios if a.get("codec_name") in AUDIO_CODECS]
        default = [a for a in usable if a.get("disposition", {}).get("default")]
        audio = (default or usable or [None])[0]
        if audio is None:
            codecs = ", ".join(sorted({a.get('codec_name', '?') for a in audios}))
            warnings.append(f"Audio codec ({codecs}) cannot be stream-copied into MP4; serving video only.")
        elif len(audios) > 1:
            warnings.append(f"{len(audios)} audio streams found; only stream #{audio['index']} is served.")
    return video, audio, warnings


def _video_timescale(tb: Fraction) -> int:
    return tb.denominator if tb.numerator == 1 else 90000


# --------------------------------------------------------------------------
# Sample extraction
# --------------------------------------------------------------------------

@dataclass
class Candidates:
    """Packets at which ffmpeg output may start (keyframes for video)."""
    index: array  # packet indices, sorted by pts
    pts: array  # their pts

    @classmethod
    def build(cls, pkts: PacketList, keyframes_only: bool) -> "Candidates":
        idx = [i for i in range(len(pkts.pts)) if not keyframes_only or pkts.key[i]]
        idx.sort(key=lambda i: pkts.pts[i])
        return cls(array("q", idx), array("q", (pkts.pts[i] for i in idx)))


@dataclass
class Extracted:
    samples: list[mp4.Sample]
    init: bytes
    start_packet: int  # source packet index of ffmpeg's first output sample
    seek: float | None
    attempts: int
    verified: bool  # sample sizes matched the source packet index


SIZE_CHECK = 8


def _sizes_match(samples: list[mp4.Sample], pkts: PacketList, j: int) -> bool:
    n = min(SIZE_CHECK, len(samples), len(pkts.size) - j)
    if n <= 0:
        return False
    # Allow a constant per-packet difference (e.g. ADTS headers stripped).
    deltas = {samples[i].size - pkts.size[j + i] for i in range(n)}
    return len(deltas) == 1


def locate_start(samples: list[mp4.Sample], out_timescale: int, pkts: PacketList,
                 cands: Candidates, tb: Fraction) -> tuple[int, bool]:
    """Find the source packet index of the first output sample.

    With -copyts and movflags frag_discont, ffmpeg writes the pts of the
    first packet into the tfdt of the first fragment. The nearest candidate
    by time is cross-checked with the packet sizes.
    """
    t0 = Fraction(samples[0].dts, out_timescale) / tb  # in source time base units
    pos = bisect.bisect_left(cands.pts, round(t0))
    near = sorted(range(max(0, pos - 3), min(len(cands.pts), pos + 3)),
                  key=lambda k: abs(cands.pts[k] - t0))
    for k in near:
        j = cands.index[k]
        if _sizes_match(samples, pkts, j):
            return j, True
    # Fall back to a size-only search in a wider window.
    for k in range(max(0, pos - 200), min(len(cands.pts), pos + 200)):
        j = cands.index[k]
        if _sizes_match(samples, pkts, j):
            log.warning("Located output start by packet sizes only (packet %d)", j)
            return j, True
    if not near:
        raise PackagerError("Cannot locate ffmpeg output in packet index")
    return cands.index[near[0]], False


async def extract_samples(path: Path, stream_index: int, kind: str, pkts: PacketList,
                          cands: Candidates, tb: Fraction, first: int, count: int,
                          video_timescale: int | None, label: str = "") -> Extracted:
    """Get exactly ``count`` samples starting at source packet ``first``."""
    if first == 0:
        seeks: list[float | None] = [None]
    else:
        base = float(pkts.pts[first] * tb)
        seeks = [base, base - 5, base - 30, None]
    last_error = "no output"
    for attempt, seek in enumerate(seeks, 1):
        if seek is not None and seek <= 0:
            seek = None
        state: dict = {}

        def done(reader: mp4.FragmentReader) -> bool:
            if "start" not in state:
                ts = mp4.parse_init(reader.init).timescale if reader.init else 0
                state["start"], state["verified"] = locate_start(reader.samples, ts, pkts, cands, tb)
            start = state["start"]
            if start > first:
                return True  # landed too late, retry with an earlier seek
            return len(reader.samples) >= first - start + count

        reader = await run_until(JobRequest(path, stream_index, kind, seek, video_timescale), done,
                                 label=label)
        if not reader.samples:
            last_error = "ffmpeg produced no samples"
            continue
        if "start" not in state:
            done(reader)
        start = state["start"]
        if start > first:
            last_error = f"seek landed after the segment (packet {start} > {first})"
            continue
        a = first - start
        samples = reader.samples[a:a + count]
        if len(samples) < count:
            log.warning("%s: expected %d samples, got %d", label, count, len(samples))
            if not samples:
                last_error = "segment is empty"
                continue
        assert reader.init is not None
        return Extracted(samples, reader.init, start, seek, attempt, state["verified"])
    raise PackagerError(f"{label}: {last_error}")


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------

async def index_media(path: Path) -> MediaIndex:
    started = time.monotonic()
    st = path.stat()
    info = await probe_streams(path)
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    vstream, astream, warnings = _pick_streams(streams)
    if vstream is None:
        raise ProbeError("No video stream")
    vcodec = vstream.get("codec_name", "")
    if vcodec not in VIDEO_CODECS:
        raise ProbeError(f"Unsupported video codec: {vcodec}")
    if vcodec == "h264":
        warnings.append("H.264 is served, but the test targets are HEVC / VP9 / AV1.")

    vidx = int(vstream["index"])
    aidx = int(astream["index"]) if astream else None
    wanted = {vidx} | ({aidx} if aidx is not None else set())
    packets, side_data = await asyncio.gather(
        probe_packets(path, wanted), probe_first_frame_side_data(path, vidx))
    vp = packets[vidx]
    if not vp.pts:
        raise ProbeError("Video stream has no packets")
    vtb = _frac(vstream["time_base"])
    vcands = Candidates.build(vp, keyframes_only=True)

    cuts = plan_video_cuts(vp, vtb, settings.segment_duration)
    if cuts[0][0] > 0:
        warnings.append(f"{cuts[0][0]} video packets before the first keyframe are skipped.")

    ap = packets.get(aidx) if aidx is not None else None
    atb = _frac(astream["time_base"]) if astream else None
    if ap is not None and not ap.pts:
        warnings.append("Audio stream has no packets; serving video only.")
        ap = None
        astream = None

    # Package the first video segment once to obtain the init segment and
    # the composition offset ffmpeg assigns to the first sample.
    vfirst, vcount = cuts[0]
    v_ex = await extract_samples(path, vidx, "video", vp, vcands, vtb, vfirst, vcount,
                                 _video_timescale(vtb), label=f"index {path.name} video")
    v_init_info = mp4.parse_init(v_ex.init)
    vts = v_init_info.timescale
    cts0 = v_ex.samples[0].cts

    a_init = a_init_info = acands = None
    if ap is not None:
        acands = Candidates.build(ap, keyframes_only=False)
        a_ex = await extract_samples(path, aidx, "audio", ap, acands, atb, 0, 1, None,
                                     label=f"index {path.name} audio")
        a_init = a_ex.init
        a_init_info = mp4.parse_init(a_init)

    # Timeline shift: start the presentation near zero and make sure the
    # first video sample does not get a negative decode time.
    first_seg_pts = vp.pts[vfirst:vfirst + vcount]
    starts = [min(first_seg_pts) * vtb]
    if ap is not None:
        starts.append(ap.pts[0] * atb)
    shift = -min(starts)
    k0 = vp.pts[vfirst] * vtb + shift
    lead = Fraction(cts0, vts) - k0
    if lead > 0:
        shift += lead

    fmt_duration = Fraction(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
    vsegs = build_segments(vp, cuts, vtb, vts, shift)
    video = Track("video", vidx, vcodec, vtb, vts, v_ex.init, v_init_info, vsegs, vp, vcands)

    audio = None
    if ap is not None:
        boundaries = [vp.pts[f] * vtb for f, _ in cuts]
        acuts = plan_audio_cuts(ap, atb, boundaries)
        ats = a_init_info.timescale
        asegs = build_segments(ap, acuts, atb, ats, shift)
        audio = Track("audio", aidx, astream.get("codec_name", ""), atb, ats, a_init, a_init_info, asegs,
                      ap, acands)

    gop = _gop_stats(vp, vtb)
    if gop["max"] > 10:
        warnings.append(f"Long keyframe interval (max {gop['max']:.1f}s): segments will be long.")
    seg_durs = [s.d / vts for s in vsegs]

    fps = _rate(vstream.get("avg_frame_rate")) or _rate(vstream.get("r_frame_rate"))
    video_info = {
        "codec": vcodec,
        "profile": vstream.get("profile", ""),
        "width": int(vstream.get("width", 0)),
        "height": int(vstream.get("height", 0)),
        "fps": fps,
        "r_frame_rate": vstream.get("r_frame_rate", ""),
        "avg_frame_rate": vstream.get("avg_frame_rate", ""),
        "pix_fmt": vstream.get("pix_fmt", ""),
        "codecs": video.codecs,
        "sample_entry": v_init_info.sample_entry,
        "time_base": str(vtb),
        "timescale": vts,
        "packets": len(vp.pts),
        "segments": len(vsegs),
        "segment_min": min(seg_durs),
        "segment_max": max(seg_durs),
    }
    audio_info = None
    if audio is not None:
        audio_info = {
            "codec": audio.codec_name,
            "profile": astream.get("profile", ""),
            "channels": int(astream.get("channels", 0)),
            "channel_layout": astream.get("channel_layout", ""),
            "sample_rate": int(astream.get("sample_rate", 0)),
            "codecs": audio.codecs,
            "language": astream.get("tags", {}).get("language", ""),
            "packets": len(ap.pts),
            "segments": len(audio.segments),
            "bitrate": sum(ap.size) * 8 / float(fmt_duration) if fmt_duration else 0,
        }

    end = max(s.t + s.d for s in vsegs) / vts
    return MediaIndex(
        path=path, size=st.st_size, mtime_ns=st.st_mtime_ns,
        duration=float(end), format_name=fmt.get("format_long_name") or fmt.get("format_name", ""),
        video=video, audio=audio, video_info=video_info, audio_info=audio_info,
        hdr=_hdr_info(vstream, side_data), gop=gop, warnings=warnings,
        index_seconds=time.monotonic() - started, indexed_at=time.time(),
    )


@dataclass
class SegmentResult:
    data: bytes
    segment: Segment
    track: Track
    extracted: Extracted


async def generate_segment(idx: MediaIndex, kind: str, number: int) -> SegmentResult:
    track = idx.video if kind == "video" else idx.audio
    if track is None or not 1 <= number <= len(track.segments):
        raise KeyError(f"{kind} segment {number} not found")
    seg = track.segments[number - 1]
    ex = await extract_samples(
        idx.path, track.stream_index, kind, track.packets, track.candidates, track.time_base,
        seg.first_packet, seg.packet_count, track.timescale if kind == "video" else None,
        label=f"{idx.path.name} {kind} #{number}")
    # seg.t is the presentation time of the first sample; tfdt is its decode time.
    base = max(0, seg.t - ex.samples[0].cts)
    data = mp4.write_fragment(ex.samples, base, number)
    return SegmentResult(data, seg, track, ex)
