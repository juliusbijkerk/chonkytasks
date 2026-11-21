#!/usr/bin/env bash
# Record the screen and your voice into one mp4, ready for the pipeline.
#
#   ./pipeline/record.sh                 # writes out/recording.mp4
#   ./pipeline/record.sh shopping        # writes out/shopping.mp4
#
# Press q (or ctrl-C) to stop.
#
# First run will ask for Screen Recording and Microphone permission for whatever
# terminal you launch it from. macOS needs that app RESTARTED after you grant
# screen recording, so do this before you need it, not at 17:00.
set -euo pipefail

cd "$(dirname "$0")/.."
NAME="${1:-recording}"
OUT="out/${NAME}.mp4"
WAV="out/${NAME}.wav"
mkdir -p out

# Resolve device indices rather than hardcoding them: they move when you plug
# in a phone or a headset.
DEVS=$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 || true)
SCREEN=$(echo "$DEVS" | grep -i 'Capture screen 0' | grep -o '\[[0-9]\]' | tr -d '[]' | head -1)
MIC=$(echo "$DEVS" | grep -iE '\[[0-9]\] (MacBook.*Microphone|.*Built-in Mic)' \
      | grep -o '\[[0-9]\]' | tr -d '[]' | head -1)
SCREEN="${SCREEN:-3}"
MIC="${MIC:-1}"

echo
echo "  screen device $SCREEN, mic device $MIC"
echo "  -> $OUT"
echo
echo "  Narrate while you work. Say WHY, not what:"
echo "    \"I always check the review count first, under 50 I ignore it\""
echo "    not \"now I am clicking the search box\"."
echo
echo "  Nothing on screen you would not show a stranger. Close your mail."
echo
read -r -p "  enter to start recording, ctrl-C to back out "

# 10 fps is plenty: we sample a frame every 2-5s downstream anyway, and it keeps
# the file small enough to move around at a hackathon.
ffmpeg -hide_banner -loglevel warning \
  -f avfoundation -framerate 10 -capture_cursor 1 -i "${SCREEN}:${MIC}" \
  -c:v libx264 -preset ultrafast -crf 28 -pix_fmt yuv420p \
  -c:a aac -b:a 128k \
  "$OUT"

echo
echo "  saved $OUT"

# Pull the audio out now so transcribe.py has something to send to Voxtral.
ffmpeg -hide_banner -loglevel error -y -i "$OUT" -vn -ac 1 -ar 16000 "$WAV"
echo "  saved $WAV"
echo
echo "  next:"
echo "    ./pipeline/transcribe.py $WAV"
echo "    ./pipeline/sample_frames.py $OUT --every 3"
echo "    ./pipeline/observe.py"
echo "    ./pipeline/rank.py"
