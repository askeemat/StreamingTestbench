"""End-to-end check of on-the-fly packaging.

For each file: index it, generate the init segment and every media segment
of every track, concatenate them and compare the resulting packets with the
source (packet sizes in order, and presentation timestamps).

Usage: python -m tests.verify_stream FILE [FILE...]
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

from app import media, mpd


def ffprobe_packets(path: Path, select: str) -> list[tuple[float, int]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", select, "-show_entries", "packet=pts_time,size",
         "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        pts, size = line.split(",")[:2]
        if pts != "N/A":
            rows.append((float(pts), int(size)))
    return rows


async def verify(path: Path) -> bool:
    idx = await media.index_media(path)
    print(f"== {path.name}: {idx.video.codecs} {idx.video_info['width']}x{idx.video_info['height']} "
          f"{idx.video_info['fps']:.3f}fps {idx.hdr['format']} "
          f"segments={len(idx.video.segments)} index={idx.index_seconds:.2f}s")
    if idx.audio:
        print(f"   audio: {idx.audio.codecs} segments={len(idx.audio.segments)}")
    for w in idx.warnings:
        print(f"   warning: {w}")
    ok = True
    for track in [idx.video, idx.audio]:
        if track is None:
            continue
        data = bytearray(track.init)
        for seg in track.segments:
            res = await media.generate_segment(idx, track.kind, seg.number)
            data += res.data
        with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
            tmp.write(data)
            tmp.flush()
            got = ffprobe_packets(Path(tmp.name), "0")
        src_all = ffprobe_packets(path, str(track.stream_index))
        first = track.segments[0].first_packet
        src = src_all[first:]
        shift = Fraction(track.segments[0].t, track.timescale) - Fraction(src[0][0]).limit_denominator(1 << 20)
        deltas = {g[1] - s[1] for g, s in zip(got, src)}
        size_ok = len(got) == len(src) and len(deltas) <= 1
        max_err = max((abs(g[0] - (s[0] + float(shift))) for g, s in zip(got, src)), default=0)
        tol = 1.5 / track.timescale + 1e-6
        ts_ok = len(got) == len(src) and max_err <= max(tol, 0.0011)
        print(f"   {track.kind}: packets src={len(src)} out={len(got)} sizes={'OK' if size_ok else 'MISMATCH'} "
              f"pts max_err={max_err * 1000:.3f}ms {'OK' if ts_ok else 'NG'}")
        ok &= size_ok and ts_ok
    m = mpd.build_mpd(idx, title=path.name)
    assert "<MPD" in m
    return ok


async def main(paths: list[str]) -> int:
    results = [await verify(Path(p)) for p in paths]
    print("ALL OK" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
