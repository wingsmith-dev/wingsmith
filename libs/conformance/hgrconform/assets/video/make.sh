#!/usr/bin/env bash
# Regenerates the clips the video probes send. Committed beside them so that a clip
# is a reviewable recipe rather than an opaque binary: one red square moving left to
# right across black, varied along one axis at a time around the 320x240, 4 fps, 4 s
# base. Needs an ffmpeg with libx264; set FFMPEG to use a particular one.
set -euo pipefail

FFMPEG="${FFMPEG:-ffmpeg}"
cd "$(dirname "$0")"

clip() {
  local width=$1 height=$2 fps=$3 seconds=$4
  local side=$((height / 4))
  "$FFMPEG" -hide_banner -loglevel error -y \
    -f lavfi -i "color=c=black:s=${width}x${height}:r=${fps}:d=${seconds}" \
    -f lavfi -i "color=c=red:s=${side}x${side}:r=${fps}:d=${seconds}" \
    -filter_complex "[0][1]overlay=x='(W-w)*t/${seconds}':y='(H-h)/2':shortest=1,format=yuv420p" \
    -c:v libx264 -preset veryslow -crf 30 -g 100000 -an \
    -map_metadata -1 -fflags +bitexact -flags:v +bitexact -movflags +faststart \
    "motion-${width}x${height}-${fps}fps-${seconds}s.mp4"
}

for seconds in 1 2 4 8 16; do clip 320 240 4 "$seconds"; done
for fps in 1 2 8 16 30; do clip 320 240 "$fps" 4; done
for size in 160x120 640x480 1280x720 1920x1080; do clip "${size%x*}" "${size#*x}" 4 4; done
clip 160 120 1 1800
