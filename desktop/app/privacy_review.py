"""Prepare local image masks and bind an analysis to the reviewed evidence bytes."""
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory


FRAME_ID = re.compile(r"f[0-9]{4}")
HELPER = Path.home() / "Applications/chonkytasks.app/Contents/Helpers/RedactEvidence"


def prepare(folder: Path, frames: list[dict]) -> None:
    helper = Path(os.environ.get("CHONKYTASKS_REDACTOR", str(HELPER)))
    if not helper.is_file():
        raise ValueError("The local privacy helper is missing. Build the updated chonkytasks macOS app and retry.")
    args = [frame["id"] for frame in frames]
    with TemporaryDirectory(prefix="redaction-", dir=folder) as staging:
        output = Path(staging) / "redacted"
        output.mkdir(mode=0o700)
        try:
            subprocess.run([str(helper), str(folder / "frames"), str(output), *args],
                           check=True, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for frame_id in args:
                read_image(output, frame_id)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise ValueError("Privacy review could not prepare every screenshot. No screenshots were shared.") from None
        target = folder / "redacted"
        if target.exists():
            shutil.rmtree(target)
        output.rename(target)


def read_image(folder: Path, frame_id: str) -> bytes:
    if not FRAME_ID.fullmatch(frame_id):
        raise ValueError("The privacy review contains an invalid screenshot ID.")
    path = folder / f"{frame_id}.jpg"
    with path.open("rb") as file:
        jpeg = file.read(512 * 1024 + 1)
    if len(jpeg) > 512 * 1024 or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ValueError("A redacted screenshot is unavailable. Prepare the recording again.")
    return jpeg


@dataclass(frozen=True)
class ReviewedEvidence:
    revision: str
    context: str
    model: str
    frames: tuple[dict, ...]
    excluded: tuple[dict, ...]
    images: dict[str, bytes]

    def selection(self) -> dict:
        return {"revision": self.revision, "model": self.model, "context": self.context,
                "frame_ids": [frame["id"] for frame in self.frames]}

    def manifest(self) -> dict:
        return {**self.selection(), "provider": "OpenRouter", "frames": [
            {"id": frame["id"], "timestamp": frame["timestamp"],
             "image_url": "data:image/jpeg;base64," + base64.b64encode(self.images[frame["id"]]).decode("ascii")}
            for frame in self.frames]}


def review(folder: Path, frames: list[dict], selected: list[str], context: str, model: str) -> ReviewedEvidence:
    ids = set(selected)
    known = {frame["id"] for frame in frames}
    if not ids or len(ids) != len(selected) or not ids <= known:
        raise ValueError("Select at least one available redacted screenshot without duplicates.")
    approved, excluded = [], []
    images = {}
    for frame in frames:
        frame_id = frame["id"]
        if frame_id not in ids:
            excluded.append(frame)
            continue
        approved.append(frame)
        images[frame_id] = read_image(folder / "redacted", frame_id)
    stamp = {"format": 1, "job": folder.name, "provider": "OpenRouter", "model": model, "context": context,
             "timeline": [(frame["id"], frame["timestamp"]) for frame in frames], "frames": [
        (frame["id"], frame["timestamp"], hashlib.sha256(images[frame["id"]]).hexdigest()) for frame in approved]}
    revision = hashlib.sha256(json.dumps(stamp, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return ReviewedEvidence(revision, context, model, tuple(approved), tuple(excluded), images)


def stored(folder: Path, job: dict, model: str) -> ReviewedEvidence:
    approved = job.get("privacy_approval") or {}
    if approved.get("model") != model:
        raise ValueError("Review and approve the evidence for the current analysis model.")
    try:
        package = review(folder, job["frames"], approved["frame_ids"], approved["context"], model)
    except (KeyError, OSError, ValueError):
        raise ValueError("Reviewed screenshots could not be verified. Open the privacy review again.") from None
    if approved.get("revision") != package.revision:
        raise ValueError("The screenshots changed after review. Open the privacy review again.")
    return package
