"""Minimal ISO-BMFF (MP4) box utilities.

Only what is needed to:
  * split ffmpeg's fragmented MP4 output into an init segment (ftyp+moov)
    and a media segment (moof+mdat...),
  * rewrite tfdt / mfhd so that independently generated segments line up
    on a single continuous timeline,
  * extract track timescale, codec configuration and colour information
    from the init segment (used to build the MPD).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

CONTAINER_BOXES = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf",
    b"mvex", b"edts", b"dinf", b"udta",
}

# Size of the fixed part of sample entries (after the 8 byte box header).
VISUAL_SAMPLE_ENTRY_FIXED = 78
AUDIO_SAMPLE_ENTRY_FIXED = 28


@dataclass
class Box:
    type: bytes
    start: int  # offset of the box header
    end: int  # offset just after the box
    header: int  # header length (8 or 16)

    @property
    def payload_start(self) -> int:
        return self.start + self.header


def iter_boxes(data: bytes | bytearray, start: int = 0, end: int | None = None) -> Iterator[Box]:
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, btype = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError(f"Malformed box {btype!r} at offset {pos}")
        yield Box(btype, pos, pos + size, header)
        pos += size


def find_box(data: bytes | bytearray, path: list[bytes], start: int = 0, end: int | None = None) -> Box | None:
    """Find the first box matching a path of box types (e.g. [b'moov', b'trak'])."""
    for box in iter_boxes(data, start, end):
        if box.type == path[0]:
            if len(path) == 1:
                return box
            found = find_box(data, path[1:], box.payload_start, box.end)
            if found:
                return found
    return None


def split_init(data: bytes) -> bytes:
    """Return ftyp+moov of a fragmented MP4 file."""
    parts = [data[b.start:b.end] for b in iter_boxes(data) if b.type in (b"ftyp", b"moov")]
    if not any(p[4:8] == b"moov" for p in parts):
        raise ValueError("No moov box in ffmpeg output")
    return b"".join(parts)


# --------------------------------------------------------------------------
# Fragment parsing (ffmpeg output) and writing (DASH media segments)
# --------------------------------------------------------------------------

SYNC_SAMPLE_FLAGS = 0x02000000  # sample_depends_on = 2 (I-frame), not a non-sync sample
NON_SYNC_SAMPLE_FLAGS = 0x01010000  # depends on others, sample_is_non_sync_sample


@dataclass
class Sample:
    dts: int  # decode time in track timescale (as written by ffmpeg)
    duration: int
    flags: int
    cts: int  # composition offset
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class TrexDefaults:
    duration: int = 0
    size: int = 0
    flags: int = 0


def parse_trex(init: bytes) -> TrexDefaults:
    trex = find_box(init, [b"moov", b"mvex", b"trex"])
    if not trex:
        return TrexDefaults()
    p = trex.payload_start + 4
    _track, _desc, dur, size, flags = struct.unpack_from(">IIIII", init, p)
    return TrexDefaults(dur, size, flags)


def parse_fragment(buf: bytes, moof: Box, mdat: Box, trex: TrexDefaults) -> list[Sample]:
    """Parse one moof (+ the mdat right after it) into samples.

    ``buf`` must contain both boxes; offsets are relative to ``buf``.
    Only the first traf is used (ffmpeg outputs a single track).
    """
    traf = next((b for b in iter_boxes(buf, moof.payload_start, moof.end) if b.type == b"traf"), None)
    if traf is None:
        return []
    def_dur, def_size, def_flags = trex.duration, trex.size, trex.flags
    base = moof.start
    base_dts = 0
    samples: list[Sample] = []
    next_data = None
    for box in iter_boxes(buf, traf.payload_start, traf.end):
        p = box.payload_start
        if box.type == b"tfhd":
            flags = int.from_bytes(buf[p + 1:p + 4], "big")
            q = p + 8
            if flags & 0x1:
                # Absolute offsets are not supported (ffmpeg uses default_base_moof).
                q += 8
            if flags & 0x2:
                q += 4
            if flags & 0x8:
                def_dur = struct.unpack_from(">I", buf, q)[0]
                q += 4
            if flags & 0x10:
                def_size = struct.unpack_from(">I", buf, q)[0]
                q += 4
            if flags & 0x20:
                def_flags = struct.unpack_from(">I", buf, q)[0]
        elif box.type == b"tfdt":
            version = buf[p]
            # ffmpeg (frag_discont, -avoid_negative_ts disabled) may write a
            # negative start time as a wrapped 64-bit value.
            base_dts = struct.unpack_from(">q" if version == 1 else ">I", buf, p + 4)[0]
        elif box.type == b"trun":
            version = buf[p]
            flags = int.from_bytes(buf[p + 1:p + 4], "big")
            count = struct.unpack_from(">I", buf, p + 4)[0]
            q = p + 8
            data_pos = next_data if next_data is not None else mdat.payload_start
            if flags & 0x1:
                data_pos = base + struct.unpack_from(">i", buf, q)[0]
                q += 4
            first_flags = None
            if flags & 0x4:
                first_flags = struct.unpack_from(">I", buf, q)[0]
                q += 4
            dts = base_dts + sum(s.duration for s in samples)
            for i in range(count):
                dur, size, sflags, cts = def_dur, def_size, def_flags, 0
                if flags & 0x100:
                    dur = struct.unpack_from(">I", buf, q)[0]
                    q += 4
                if flags & 0x200:
                    size = struct.unpack_from(">I", buf, q)[0]
                    q += 4
                if flags & 0x400:
                    sflags = struct.unpack_from(">I", buf, q)[0]
                    q += 4
                elif i == 0 and first_flags is not None:
                    sflags = first_flags
                if flags & 0x800:
                    cts = struct.unpack_from(">i" if version == 1 else ">I", buf, q)[0]
                    q += 4
                if data_pos + size > mdat.end:
                    raise ValueError("Sample data outside of mdat")
                samples.append(Sample(dts, dur, sflags, cts, bytes(buf[data_pos:data_pos + size])))
                dts += dur
                data_pos += size
            next_data = data_pos
    return samples


class FragmentReader:
    """Incrementally parses a fragmented MP4 byte stream."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.init_parts: list[bytes] = []
        self.init: bytes | None = None
        self.trex = TrexDefaults()
        self.samples: list[Sample] = []
        self.fragments = 0
        self._pending_moof: bytes | None = None

    def feed(self, chunk: bytes) -> None:
        self.buf += chunk
        pos = 0
        while True:
            if len(self.buf) - pos < 8:
                break
            size, btype = struct.unpack_from(">I4s", self.buf, pos)
            header = 8
            if size == 1:
                if len(self.buf) - pos < 16:
                    break
                size = struct.unpack_from(">Q", self.buf, pos + 8)[0]
                header = 16
            if size == 0 or size < header:
                raise ValueError("Unsupported box size in stream")
            if len(self.buf) - pos < size:
                break
            box = bytes(self.buf[pos:pos + size])
            pos += size
            self._on_box(btype, box)
        if pos:
            del self.buf[:pos]

    def _on_box(self, btype: bytes, box: bytes) -> None:
        if btype in (b"ftyp", b"moov"):
            self.init_parts.append(box)
            if btype == b"moov":
                self.init = b"".join(self.init_parts)
                self.trex = parse_trex(self.init)
        elif btype == b"moof":
            self._pending_moof = box
        elif btype == b"mdat" and self._pending_moof is not None:
            buf = self._pending_moof + box
            moof = Box(b"moof", 0, len(self._pending_moof), 8)
            mdat = next(iter_boxes(buf, moof.end))
            self.samples.extend(parse_fragment(buf, moof, mdat, self.trex))
            self.fragments += 1
            self._pending_moof = None


def _box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), btype) + payload


def _full_box(btype: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(btype, struct.pack(">I", (version << 24) | flags) + payload)


def write_fragment(samples: list[Sample], base_media_decode_time: int, sequence_number: int,
                   track_id: int = 1) -> bytes:
    """Write a single moof+mdat containing ``samples``."""
    if not samples:
        raise ValueError("No samples to write")
    signed = any(s.cts < 0 for s in samples)
    trun_version = 1 if signed else 0
    cts_fmt = ">i" if signed else ">I"
    entries = bytearray()
    for s in samples:
        entries += struct.pack(">III", s.duration, s.size, s.flags) + struct.pack(cts_fmt, s.cts)
    trun_flags = 0x1 | 0x100 | 0x200 | 0x400 | 0x800

    def build(data_offset: int) -> bytes:
        mfhd = _full_box(b"mfhd", 0, 0, struct.pack(">I", sequence_number))
        tfhd = _full_box(b"tfhd", 0, 0x020000, struct.pack(">I", track_id))
        tfdt = _full_box(b"tfdt", 1, 0, struct.pack(">Q", base_media_decode_time))
        trun = _full_box(b"trun", trun_version, trun_flags,
                         struct.pack(">Ii", len(samples), data_offset) + bytes(entries))
        return _box(b"moof", mfhd + _box(b"traf", tfhd + tfdt + trun))

    moof_len = len(build(0))
    moof = build(moof_len + 8)
    mdat_payload_len = sum(s.size for s in samples)
    mdat_header = struct.pack(">I4s", 8 + mdat_payload_len, b"mdat")
    return b"".join([moof, mdat_header] + [s.data for s in samples])


# --------------------------------------------------------------------------
# Init segment inspection
# --------------------------------------------------------------------------

@dataclass
class TrackInit:
    timescale: int
    sample_entry: str  # e.g. hvc1, hev1, vp09, av01, mp4a, Opus
    codecs: str  # RFC 6381 codecs string
    width: int = 0
    height: int = 0
    channels: int = 0
    sample_rate: int = 0
    colour: dict | None = None  # {"primaries", "transfer", "matrix", "full_range"}
    has_mdcv: bool = False
    has_clli: bool = False
    extra: dict = field(default_factory=dict)


def _mdhd_timescale(data: bytes, mdhd: Box) -> int:
    p = mdhd.payload_start
    version = data[p]
    off = p + (20 if version == 1 else 12)
    return struct.unpack_from(">I", data, off)[0]


def _parse_colr(data: bytes, box: Box) -> dict | None:
    p = box.payload_start
    if data[p:p + 4] not in (b"nclx", b"nclc"):
        return None
    primaries, transfer, matrix = struct.unpack_from(">HHH", data, p + 4)
    full_range = 0
    if data[p:p + 4] == b"nclx" and box.end > p + 10:
        full_range = data[p + 10] >> 7
    return {"primaries": primaries, "transfer": transfer, "matrix": matrix, "full_range": full_range}


class _BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v


def _hevc_codecs(entry: str, cfg: bytes) -> str:
    br = _BitReader(cfg)
    br.read(8)  # configurationVersion
    profile_space = br.read(2)
    tier = br.read(1)
    profile_idc = br.read(5)
    compat = br.read(32)
    constraints = [br.read(8) for _ in range(6)]
    level = br.read(8)
    # Compatibility flags are written in reverse bit order.
    rev = int(f"{compat:032b}"[::-1], 2)
    space = ["", "A", "B", "C"][profile_space]
    parts = [entry, f"{space}{profile_idc}", f"{rev:X}", f"{'H' if tier else 'L'}{level}"]
    while constraints and constraints[-1] == 0:
        constraints.pop()
    parts += [f"{c:X}" for c in constraints]
    return ".".join(parts)


def _vp9_codecs(cfg: bytes) -> str:
    # vpcC is a FullBox: version(1) flags(3)
    p = 4
    profile, level = cfg[p], cfg[p + 1]
    b = cfg[p + 2]
    bit_depth = b >> 4
    chroma = (b >> 1) & 0x7
    full_range = b & 1
    primaries, transfer, matrix = cfg[p + 3], cfg[p + 4], cfg[p + 5]
    return (f"vp09.{profile:02d}.{level:02d}.{bit_depth:02d}.{chroma:02d}."
            f"{primaries:02d}.{transfer:02d}.{matrix:02d}.{full_range:02d}")


def _av1_codecs(cfg: bytes, colour: dict | None) -> str:
    seq_profile = cfg[1] >> 5
    seq_level = cfg[1] & 0x1F
    tier = cfg[2] >> 7
    high_bitdepth = (cfg[2] >> 6) & 1
    twelve_bit = (cfg[2] >> 5) & 1
    mono = (cfg[2] >> 4) & 1
    subx = (cfg[2] >> 3) & 1
    suby = (cfg[2] >> 2) & 1
    chroma_pos = cfg[2] & 0x3
    depth = 12 if twelve_bit else (10 if high_bitdepth else 8)
    s = f"av01.{seq_profile}.{seq_level:02d}{'H' if tier else 'M'}.{depth:02d}"
    if colour:
        s += (f".{mono}.{subx}{suby}{chroma_pos if (subx and suby) else 0}"
              f".{colour['primaries']:02d}.{colour['transfer']:02d}.{colour['matrix']:02d}.{colour['full_range']}")
    return s


def _avc_codecs(entry: str, cfg: bytes) -> str:
    return f"{entry}.{cfg[1]:02X}{cfg[2]:02X}{cfg[3]:02X}"


def _mp4a_codecs(data: bytes, entry_box: Box) -> str:
    esds = find_box(data, [b"esds"], entry_box.payload_start + AUDIO_SAMPLE_ENTRY_FIXED, entry_box.end)
    if not esds:
        return "mp4a.40.2"
    d = data[esds.payload_start + 4:esds.end]
    i = 0

    def read_desc(i: int) -> tuple[int, int, int]:
        tag = d[i]
        i += 1
        size = 0
        for _ in range(4):
            b = d[i]
            i += 1
            size = (size << 7) | (b & 0x7F)
            if not b & 0x80:
                break
        return tag, size, i

    try:
        tag, size, i = read_desc(i)  # ES_Descriptor
        if tag != 0x03:
            return "mp4a.40.2"
        flags = d[i + 2]
        i += 3
        if flags & 0x80:
            i += 2
        if flags & 0x40:
            i += 1 + d[i]
        if flags & 0x20:
            i += 2
        tag, size, i = read_desc(i)  # DecoderConfigDescriptor
        oti = d[i]
        if oti != 0x40:
            return f"mp4a.{oti:02X}"
        i += 13
        tag, size, i = read_desc(i)  # DecoderSpecificInfo
        aot = d[i] >> 3
        if aot == 31:
            aot = 32 + (((d[i] & 0x7) << 3) | (d[i + 1] >> 5))
        return f"mp4a.40.{aot}"
    except IndexError:
        return "mp4a.40.2"


AUDIO_ENTRY_CODECS = {
    "Opus": "opus", "fLaC": "flac", "ac-3": "ac-3", "ec-3": "ec-3",
    "alac": "alac", ".mp3": "mp4a.6B",
}


def parse_init(data: bytes) -> TrackInit:
    trak = find_box(data, [b"moov", b"trak"])
    if not trak:
        raise ValueError("No trak in init segment")
    mdhd = find_box(data, [b"mdia", b"mdhd"], trak.payload_start, trak.end)
    stsd = find_box(data, [b"mdia", b"minf", b"stbl", b"stsd"], trak.payload_start, trak.end)
    hdlr = find_box(data, [b"mdia", b"hdlr"], trak.payload_start, trak.end)
    if not (mdhd and stsd and hdlr):
        raise ValueError("Incomplete trak in init segment")
    timescale = _mdhd_timescale(data, mdhd)
    handler = data[hdlr.payload_start + 8:hdlr.payload_start + 12]
    entry = next(iter_boxes(data, stsd.payload_start + 8, stsd.end))
    entry_type = entry.type.decode("latin-1")

    if handler == b"vide":
        p = entry.payload_start
        width, height = struct.unpack_from(">HH", data, p + 24)
        colour = None
        cfg_box = None
        has_mdcv = has_clli = False
        for child in iter_boxes(data, p + VISUAL_SAMPLE_ENTRY_FIXED, entry.end):
            if child.type == b"colr":
                colour = _parse_colr(data, child) or colour
            elif child.type in (b"hvcC", b"vpcC", b"av1C", b"avcC"):
                cfg_box = child
            elif child.type in (b"mdcv", b"SmDm"):
                has_mdcv = True
            elif child.type in (b"clli", b"CoLL"):
                has_clli = True
        cfg = data[cfg_box.payload_start:cfg_box.end] if cfg_box else b""
        if cfg_box is None:
            codecs = entry_type
        elif cfg_box.type == b"hvcC":
            codecs = _hevc_codecs(entry_type, cfg)
        elif cfg_box.type == b"vpcC":
            codecs = _vp9_codecs(cfg)
            if colour is None:
                colour = {"primaries": cfg[7], "transfer": cfg[8], "matrix": cfg[9], "full_range": cfg[6] & 1}
        elif cfg_box.type == b"av1C":
            codecs = _av1_codecs(cfg, colour)
        else:
            codecs = _avc_codecs(entry_type, cfg)
        return TrackInit(timescale, entry_type, codecs, width=width, height=height,
                         colour=colour, has_mdcv=has_mdcv, has_clli=has_clli)

    if handler == b"soun":
        p = entry.payload_start
        channels = struct.unpack_from(">H", data, p + 16)[0]
        sample_rate = struct.unpack_from(">I", data, p + 24)[0] >> 16
        if entry_type == "mp4a":
            codecs = _mp4a_codecs(data, entry)
        else:
            codecs = AUDIO_ENTRY_CODECS.get(entry_type, entry_type)
        return TrackInit(timescale, entry_type, codecs, channels=channels, sample_rate=sample_rate)

    raise ValueError(f"Unsupported handler {handler!r}")
