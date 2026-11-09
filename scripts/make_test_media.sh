#!/usr/bin/env bash
# Generate synthetic HDR10 test clips (HEVC / VP9 / AV1) for the testbench.
# Files are written using the media layout <codec>/<resolution>/hdr/<id>.<ext>
# with id = test-<codec>-<height>p60 (e.g. media/hevc/3840x2160/hdr/test-hevc-2160p60.mp4).
# Usage: scripts/make_test_media.sh [MEDIA_DIR] [SIZE] [SECONDS]
#   e.g. scripts/make_test_media.sh media 3840x2160 30
set -euo pipefail
OUT="${1:-media}"
SIZE="${2:-1920x1080}"
SECS="${3:-20}"
HEIGHT="${SIZE#*x}"
dest() {  # dest <codec> <ext>
  local dir="$OUT/$1/$SIZE/hdr"
  mkdir -p "$dir"
  echo "$dir/test-$1-${HEIGHT}p60.$2"
}
COLOR=(-pix_fmt yuv420p10le -color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc)
SRC=(-f lavfi -i "testsrc2=size=${SIZE}:rate=60,noise=alls=8:allf=t" -f lavfi -i "sine=f=440:sample_rate=48000" -t "$SECS")
MD="G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"

echo "HEVC (MP4, AAC) ..."
ffmpeg -hide_banner -loglevel error -y "${SRC[@]}" "${COLOR[@]}" \
  -c:v libx265 -preset ultrafast -b:v 40M \
  -x265-params "keyint=120:min-keyint=120:log-level=error:hdr10=1:repeat-headers=1:master-display=${MD}:max-cll=1000,400" \
  -tag:v hvc1 -c:a aac -b:a 192k "$(dest hevc mp4)"

echo "VP9 (WebM, Opus) ..."
ffmpeg -hide_banner -loglevel error -y "${SRC[@]}" "${COLOR[@]}" \
  -c:v libvpx-vp9 -profile:v 2 -b:v 30M -g 120 -deadline realtime -cpu-used 8 -row-mt 1 \
  -c:a libopus "$(dest vp9 webm)"

echo "AV1 (MKV, Opus) ..."
ffmpeg -hide_banner -loglevel error -y "${SRC[@]}" "${COLOR[@]}" \
  -c:v libsvtav1 -preset 12 -b:v 25M -g 120 \
  -svtav1-params "enable-hdr=1:mastering-display=${MD}:content-light=1000,400" \
  -c:a libopus "$(dest av1 mkv)"

find "$OUT" -type f -not -name ".*" | sort
