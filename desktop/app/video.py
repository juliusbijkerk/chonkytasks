"""Deterministic video sampling. No OCR or non-Mistral ML models."""
import json
import math
from pathlib import Path
import re
import subprocess

import numpy as np
from PIL import Image

MAX_DURATION = 900
MAX_FRAMES = 72


def _seconds(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _video_packet_duration(path: Path) -> float | None:
    """Browser WebM captures can omit duration metadata while keeping valid timestamps."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
         "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        return None

    start, end = math.inf, -math.inf
    for line in result.stdout.splitlines():
        pts, _, duration = line.partition(",")
        timestamp = _seconds(pts)
        if timestamp is None:
            continue
        start = min(start, timestamp)
        end = max(end, timestamp + max(0.0, _seconds(duration) or 0.0))
    return end - start if end > start else None


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, timeout=30,
    )
    if result.returncode:
        raise ValueError("This file could not be read as a video. Try an MP4 or MOV recording.")
    metadata = json.loads(result.stdout)
    video = next((s for s in metadata.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("No video track was found in this file.")
    duration = _seconds(metadata.get("format", {}).get("duration"))
    if duration is None or duration <= 0:
        duration = _seconds(video.get("duration"))
    if duration is None or duration <= 0:
        duration = _video_packet_duration(path)
    if duration is None or duration <= 0:
        raise ValueError("Could not determine this recording’s duration. Try exporting it again.")
    if duration > MAX_DURATION:
        raise ValueError("Please use a recording no longer than 15 minutes.")
    return {"duration": round(duration, 3), "width": video["width"], "height": video["height"],
            "has_audio": any(s.get("codec_type") == "audio" for s in metadata["streams"])}


def select_indices(signatures: list[np.ndarray], interval: float, max_frames: int = MAX_FRAMES) -> list[int]:
    if not signatures:
        return []
    selected = [0]
    for i in range(1, len(signatures)):
        # Compare to last retained frame, not all earlier frames: preserve returns to an app.
        delta = np.abs(signatures[i] - signatures[selected[-1]])
        if delta.mean() > 2.8 or (delta > 22).mean() > 0.018 or (i - selected[-1]) * interval >= 12:
            selected.append(i)
    if selected[-1] != len(signatures) - 1:
        selected.append(len(signatures) - 1)
    if len(selected) > max_frames:
        selected = [selected[i] for i in np.linspace(0, len(selected) - 1, max_frames, dtype=int)]
    return selected


def extract_frames(path: Path, folder: Path, metadata: dict) -> tuple[list[dict], float]:
    candidates = folder / "candidates"
    candidates.mkdir(exist_ok=True)
    frames_dir = folder / "frames"
    frames_dir.mkdir(exist_ok=True)
    interval = max(1.0, metadata["duration"] / 180)
    # Preserve the actual selected presentation timestamp, including variable-frame-rate videos.
    command = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-y", "-i", str(path),
               "-an", "-vf", f"setpts=PTS-STARTPTS,select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval})',scale='min(1920,iw)':-2,showinfo",
               "-fps_mode", "vfr", "-q:v", "3", str(candidates / "%04d.jpg")]
    result = subprocess.run(command, capture_output=True, timeout=240)
    if result.returncode:
        raise ValueError("Frame extraction failed. Please try another MP4 or MOV recording.")
    files = sorted(candidates.glob("*.jpg"))
    timestamps = [float(t) for t in re.findall(r"pts_time:([\d.]+)", result.stderr.decode(errors="replace"))]
    if len(timestamps) != len(files):
        raise ValueError("Could not verify screenshot timestamps. Please try another recording.")
    signatures = []
    for image in files:
        with Image.open(image) as im:
            signatures.append(np.array(im.convert("L").resize((192, 108)), dtype=np.float32))
    selected = select_indices(signatures, interval)
    if not selected:
        raise ValueError("No readable frames were found in the video.")
    frames = []
    for index in selected:
        frame_id = f"f{index:04d}"
        target = frames_dir / f"{frame_id}.jpg"
        files[index].replace(target)
        frames.append({"id": frame_id, "timestamp": round(timestamps[index], 3),
                       "url": f"/api/jobs/{folder.name}/frames/{target.name}"})
    for image in candidates.glob("*.jpg"):
        image.unlink()
    candidates.rmdir()
    return frames, interval


def make_preview(path: Path, folder: Path):
    """Produce a browser-safe, seekable preview, including for MOV/HEVC uploads."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(path),
         "-map", "0:v:0", "-an", "-vf", "scale='min(1440,iw)':-2", "-c:v", "libx264",
         "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         str(folder / "preview.mp4")], capture_output=True, timeout=240,
    )
    if result.returncode:
        raise ValueError("Could not prepare the video preview.")

