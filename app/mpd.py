"""Static MPEG-DASH manifest (MPD) generation."""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from .media import MediaIndex, Segment, Track


def _iso_duration(seconds: float) -> str:
    return f"PT{seconds:.3f}S"


def _timeline(segments: list[Segment]) -> str:
    out: list[str] = []
    i = 0
    n = len(segments)
    while i < n:
        s = segments[i]
        r = 0
        while (i + r + 1 < n and segments[i + r + 1].d == s.d
               and segments[i + r + 1].t == s.t + (r + 1) * s.d):
            r += 1
        out.append(f'<S t="{s.t}" d="{s.d}"' + (f' r="{r}"' if r else "") + "/>")
        i += r + 1
    return "".join(out)


def _segment_template(track: Track) -> str:
    return (f'<SegmentTemplate timescale="{track.timescale}" '
            f'initialization="{track.kind}/init.mp4" media="{track.kind}/$Number$.m4s" startNumber="1">'
            f"<SegmentTimeline>{_timeline(track.segments)}</SegmentTimeline></SegmentTemplate>")


def _frame_rate(idx: MediaIndex) -> str:
    for key in ("r_frame_rate",):
        v = idx.video_info.get(key, "")
        if v and "/" in v and not v.startswith("0"):
            num, den = v.split("/")
            return num if den == "1" else v
    return f"{idx.video_info.get('fps', 0):.3f}"


def build_mpd(idx: MediaIndex, title: str = "") -> str:
    v = idx.video
    vi = idx.video_info
    seg_max = max(s.d / v.timescale for s in v.segments)
    peak = max(1, int(idx.peak_bitrate))

    colour_props = ""
    colour = v.init_info.colour
    if colour:
        for scheme, key in (("ColourPrimaries", "primaries"), ("TransferCharacteristics", "transfer"),
                            ("MatrixCoefficients", "matrix")):
            colour_props += (f'<SupplementalProperty schemeIdUri="urn:mpeg:mpegB:cicp:{scheme}" '
                             f'value="{colour[key]}"/>')

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
        'profiles="urn:mpeg:dash:profile:isoff-live:2011" '
        f'mediaPresentationDuration="{_iso_duration(idx.duration)}" '
        f'minBufferTime="{_iso_duration(seg_max)}">',
    ]
    if title:
        parts.append(f"<ProgramInformation><Title>{escape(title)}</Title></ProgramInformation>")
    parts.append('<Period id="0" start="PT0S">')
    parts.append(
        f'<AdaptationSet id="0" contentType="video" mimeType="video/mp4" segmentAlignment="true" '
        f'startWithSAP="1" maxWidth="{vi["width"]}" maxHeight="{vi["height"]}" '
        f'maxFrameRate="{_frame_rate(idx)}">'
        f"{colour_props}"
        f'<Representation id="video" codecs={quoteattr(v.codecs)} bandwidth="{peak}" '
        f'width="{vi["width"]}" height="{vi["height"]}" frameRate="{_frame_rate(idx)}" sar="1:1">'
        f"{_segment_template(v)}</Representation></AdaptationSet>"
    )
    a = idx.audio
    if a is not None:
        ai = idx.audio_info or {}
        lang = ai.get("language") or ""
        lang_attr = f" lang={quoteattr(lang)}" if lang and lang != "und" else ""
        a_bw = max(1, int(ai.get("bitrate") or 128000))
        parts.append(
            f'<AdaptationSet id="1" contentType="audio" mimeType="audio/mp4" segmentAlignment="true" '
            f'startWithSAP="1"{lang_attr}>'
            f'<Representation id="audio" codecs={quoteattr(a.codecs)} bandwidth="{a_bw}" '
            f'audioSamplingRate="{ai.get("sample_rate") or a.init_info.sample_rate}">'
            '<AudioChannelConfiguration schemeIdUri="urn:mpeg:dash:23003:3:audio_channel_configuration:2011" '
            f'value="{ai.get("channels") or a.init_info.channels}"/>'
            f"{_segment_template(a)}</Representation></AdaptationSet>"
        )
    parts.append("</Period></MPD>")
    return "\n".join(parts) + "\n"
