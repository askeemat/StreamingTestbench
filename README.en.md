# Streaming Testbench

[日本語](README.md) | English

A local-network MPEG-DASH streaming server.
It serves video files from a media directory by remuxing them into fMP4 segments **on request, without re-encoding**.

- Video codecs: HEVC / VP9 / AV1
- 4K 60fps HDR (HDR10 / HLG / HDR10+ / Dolby Vision bitstreams are passed through as-is)
- Input containers: `.mp4 .m4v .mov .mkv .webm .ts .m2ts`
- Audio: AAC / Opus / AC-3 / E-AC-3 / FLAC / MP3 / ALAC (stream copy; files with any other audio codec are served video-only)
- VOD only. No ABR, DRM or subtitles. Video is always served at the source resolution
- Segments are never cached on disk (generated in memory and returned directly)
- Web admin UI and a stream list page

## Quick start (Docker)

```bash
# 1. Put video files into ./media using the required layout (see "Directory layout rules" below)
mkdir -p media/hevc/3840x2160/hdr
cp /path/to/your_4k60_hdr.mkv media/hevc/3840x2160/hdr/movie01.mkv

# 2. Start
docker compose up -d --build

# 3. Open the stream list in a browser on your PC
#    http://<server IP>:8080/
```

To serve a different host directory, change `./media` in `docker-compose.yml` (it is mounted read-only).

## Directory layout rules

Video files must be placed in the following layout:

```
media/<codec>/<resolution>/<hdr>/<id>.<ext>
```

| Element | Values | Example |
|---|---|---|
| `<codec>` | `av1` / `hevc` / `vp9` (lowercase) | `hevc` |
| `<resolution>` | `<width>x<height>` | `1920x1080`, `3840x2160` |
| `<hdr>` | `hdr` / `sdr` (lowercase) | `hdr` |
| `<id>` | An ID of your choice, unique within its `<codec>/<resolution>/<hdr>` directory. Allowed characters: letters, digits, `-` and `_` | `video`, `movie01` |
| `<ext>` | `.mp4 .m4v .mov .mkv .webm .ts .m2ts` | `.mkv` |

Example:

```
media/
├── av1/3840x2160/hdr/test-av1-2160p60.mkv
├── hevc/3840x2160/hdr/test-hevc-2160p60.mp4
├── hevc/1920x1080/sdr/movie01.mp4
└── vp9/3840x2160/hdr/test-vp9-2160p60.webm
```

- **A stream's URL is the file path without the extension**: `http://<server IP>:8080/dash/<codec>/<resolution>/<hdr>/<id>/manifest.mpd`
- **The same ID can be reused for the same video at different resolutions, codecs or HDR/SDR.** For example:
  ```
  media/hevc/3840x2160/hdr/video.mp4  →  /dash/hevc/3840x2160/hdr/video/manifest.mpd
  media/hevc/1920x1080/hdr/video.mp4  →  /dash/hevc/1920x1080/hdr/video/manifest.mpd
  media/av1/3840x2160/hdr/video.mkv   →  /dash/av1/3840x2160/hdr/video/manifest.mpd
  ```
- **If an ID is duplicated within the same directory** (e.g. `video.mp4` and `video.mkv`), none of those files are served.
- **Files that do not follow the rules are not served.** This covers files directly under `media/`, unknown codec names, resolutions such as `4k`, uppercase names such as `HDR`, invalid IDs and unsupported extensions. The reason for each is shown under "Ignored files" on the admin page.
- **If the directory names do not match the actual content** (e.g. a VP9 file under `hevc/`, an HDR file under `sdr/`, or a different resolution), the file is still served, with a warning.
- Moving a file to another directory takes effect at the next scan (within 30 seconds, or immediately via "Rescan" on the admin page).

## Playback on VLC

1. Install VLC for Android.
2. In VLC, open "New stream" (network stream) and enter one of these URLs:
   - **Playlist of all streams**: `http://<server IP>:8080/playlist.m3u`
     (you only need to type the URL once; after that you can pick videos from the VLC playlist)
   - **Filtered playlists**: use the same structure as the directory layout. `all` matches any value.
     - `/playlist/hevc.m3u`: HEVC only
     - `/playlist/hevc/3840x2160.m3u`: HEVC 4K only
     - `/playlist/hevc/3840x2160/hdr.m3u`: HEVC 4K HDR only
     - `/playlist/all/3840x2160/hdr.m3u`: 4K HDR in all codecs
   - **Single stream**: `http://<server IP>:8080/dash/<codec>/<resolution>/<hdr>/<id>/manifest.mpd`
     (e.g. `/dash/hevc/3840x2160/hdr/video/manifest.mpd`; the "Copy" button on the stream list copies it. These URLs are long, so on the TV it is easier to choose from a filtered playlist)
3. You can watch the server side during playback on the admin page: `http://<server IP>:8080/admin`.

> Note: tunneled vs. non-tunneled mode is decided by the player (how it uses MediaCodec). The server cannot control which mode VLC uses.

## Pages

| URL | Content |
|---|---|
| `/` | Stream list (grouped by codec / resolution / HDR, with a playlist URL per group and the MPD URLs) |
| `/streams/<codec>/<resolution>/<hdr>/<id>` | Stream details (HDR metadata, keyframe interval, segment list, warnings) |
| `/admin` | Admin page (server settings, live statistics, running ffmpeg jobs, request log, rescan / re-index, list of ignored files) |
| `/playlist.m3u`, `/playlist/<codec>[/<resolution>[/<hdr>]].m3u` | M3U playlists for VLC |

### Reading the request log on the admin page

Each segment request records:

- **Gen (ms)**: the time the server took to generate the segment
- **Gen / media**: generation time ÷ segment duration. As it approaches 100%, the server can no longer keep up with playback. Values above 50% are shown in red.
- **Notes**: problems such as retried seeks or failed packet-size checks

Use this to tell whether stutter comes from the server not delivering fast enough or from decoding/rendering on the TV.
For reference, serving 4K60 HDR10 HEVC (about 60 Mbps) on the development machine took about 60–100 ms per segment (4 s, 30 MB), with the file in the OS page cache.

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `MEDIA_DIR` | `/media` | Media directory |
| `SEGMENT_DURATION` | `4` | Target segment duration (seconds). Segments are cut at keyframes, so they are at least this long |
| `MAX_CONCURRENT_JOBS` | `4` | Maximum number of concurrent ffmpeg processes |
| `PREINDEX` | `true` | Build indexes in the background at startup and when files are added |
| `ENABLE_AUDIO` | `true` | Set to `false` to serve video only |
| `ADMIN_PASSWORD` | (empty) | When set, `/admin` and the admin API require Basic authentication (user name from `ADMIN_USER`, default `admin`) |
| `PUBLIC_BASE_URL` | (empty) | Base URL used on the stream list and in playlists (e.g. `http://192.168.1.10:8080`). When empty, it is taken from the request's Host header |
| `MEDIA_EXTENSIONS` | `.mp4,.m4v,.mov,.mkv,.webm,.ts,.m2ts` | File extensions to serve |
| `REQUEST_LOG_SIZE` | `1000` | Number of request log entries kept in memory |

## How it works

```
VLC ──GET manifest.mpd──▶ FastAPI ──(first time only) ffprobe: list packets──▶ segment plan
    ──GET video/N.m4s───▶ FastAPI ──ffmpeg -ss … -c copy → fMP4 (pipe)──▶ cut out only the needed samples
                                                                     └─▶ build moof+mdat and return it
```

1. **Indexing** (once per file; kept in memory only)
   - `ffprobe` reads the PTS, size and keyframe flag of every packet (demuxing only, no decoding).
   - Segments are cut at keyframes so that each is at least the target duration. Audio is cut at the video segment boundaries.
   - The init segment (a few KB) and the codecs string are derived (from `hvcC` for HEVC, `vpcC` for VP9 and `av1C` for AV1).
2. **MPD**: a static MPD with `SegmentTemplate` + `SegmentTimeline`. Video and audio are separate AdaptationSets.
3. **Segment generation** (per request)
   - Runs `ffmpeg -copyts -ss <start> -i <file> -map <stream> -c copy -f mp4 -movflags frag_keyframe+empty_moov+default_base_moof+frag_discont pipe:1`.
   - For MKV / WebM and similar containers, seeking is not packet-accurate (it can land on an earlier keyframe). The server therefore matches the tfdt of the first output fragment against the source packet PTS, and also checks the sequence of packet sizes, to find which packet the output starts at.
   - ffmpeg is stopped as soon as enough samples have arrived. Only the required range is rebuilt into moof+mdat and returned, with tfdt rewritten onto one continuous timeline across all segments.
   - Nothing is written to disk.

## Limitations and notes

- **Segment duration depends on the keyframe interval.** Files with long keyframe intervals (e.g. 10 s or more) get long segments (a warning is shown on the details page).
- **HDR metadata**: HDR information inside HEVC / AV1 bitstreams (SEI / metadata OBUs) is preserved as-is, and colour information (`colr`) is written into the init segment. However, ffmpeg 6.1 does not write MP4 `mdcv` / `clli` boxes, so for **VP9 (WebM), where the static HDR metadata exists only at the container level**, the mastering display information may not reach the player.
- Only the first video track and one audio track (the default one) are served per file.
- Codec directories are limited to `av1` / `hevc` / `vp9` (H.264 files cannot be placed).
- HDR display and passthrough support in VLC depends on the VLC version and the TV.
- Without `ADMIN_PASSWORD`, anyone can use the admin page. Use the server only on a trusted LAN.

## Development and testing

```bash
# Run locally (requires ffmpeg / ffprobe 6.1)
pip install -r requirements.txt
MEDIA_DIR=./media uvicorn app.main:app --host 0.0.0.0 --port 8080

# Generate HDR10 test clips (HEVC/MP4, VP9/WebM, AV1/MKV)
# → written to media/<codec>/3840x2160/hdr/test-<codec>-2160p60.<ext>
scripts/make_test_media.sh media 3840x2160 20

# Verify: generate and concatenate all segments, then check that the packets (sizes, PTS) match the source
python3 -m tests.verify_stream media/*/*/*/*

# Verify inside the Docker container
docker compose exec streaming-testbench python3 -m tests.verify_stream /media/<file>
```

## Project structure

```
app/
  main.py        FastAPI app (DASH endpoints, list / details / admin pages, API)
  media.py       Indexing with ffprobe, segment planning, sample extraction
  packager.py    Running ffmpeg (incremental reading, concurrency limit)
  mp4.py         ISO-BMFF parsing and writing (splitting and rebuilding fragments, codecs strings)
  mpd.py         MPD generation
  library.py     Media directory scanning (layout validation), index management
  stats.py       Request log and statistics
  templates/     Page templates (Jinja2)
tests/verify_stream.py     End-to-end verification script
scripts/make_test_media.sh Test clip generator
Dockerfile, docker-compose.yml
```

## API

| Method and path | Description |
|---|---|
| `GET /dash/<codec>/<resolution>/<hdr>/<id>/manifest.mpd` | MPD |
| `GET /dash/<codec>/<resolution>/<hdr>/<id>/{video,audio}/init.mp4` | Init segment |
| `GET /dash/<codec>/<resolution>/<hdr>/<id>/{video,audio}/<N>.m4s` | Media segment (supports Range requests) |
| `GET /api/streams` / `GET /api/streams/<codec>/<resolution>/<hdr>/<id>` | Stream information (JSON) |
| `GET /api/status` | Statistics, running jobs and request log (admin) |
| `POST /admin/rescan` / `POST /admin/reindex/<codec>/<resolution>/<hdr>/<id>` / `POST /admin/clear-log` | Admin operations |
