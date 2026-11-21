from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.analyze import finalize, get_model, resolve_span, validate_step_coverage
from app.privacy_review import review
from app.models import Proposal
from app.video import extract_frames, probe, select_indices
from app.models import Observations
from app import analyze as analysis_module
from app import completion
import asyncio
import httpx
import json


def test_frame_selection_preserves_return_visits():
    dark = np.zeros((108, 192), dtype=np.float32)
    light = dark + 255
    assert select_indices([dark, dark, light, light, dark, dark], 1) == [0, 2, 4, 5]


def test_static_scene_retains_coverage_and_last_frame():
    frames = [np.zeros((108, 192), dtype=np.float32)] * 30
    assert select_indices(frames, 1) == [0, 12, 24, 29]


def test_frame_budget_preserves_beginning_and_end():
    frames = [np.full((10, 10), i % 2 * 255, dtype=np.float32) for i in range(200)]
    selected = select_indices(frames, 1, max_frames=72)
    assert len(selected) == 72
    assert selected[0] == 0 and selected[-1] == 199
    assert selected == sorted(set(selected))


def test_rejects_non_mistral_configuration(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "another-vendor/model")
    with pytest.raises(ValueError, match="Only Mistral"):
        get_model()


def test_api_enforces_mistral_output_and_never_sends_tools(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-test-key")
    def respond(request):
        body = json.loads(request.content)
        assert body["model"] == "mistralai/mistral-large-4-0"
        assert "tools" not in body
        assert body["reasoning"]["effort"] == "low"
        assert body["stream"] is True
        return httpx.Response(200, text='data: {"model":"other-vendor/model","choices":[]}\n\n')
    client_type = httpx.AsyncClient
    monkeypatch.setattr(completion.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(ValueError, match="non-Mistral"):
        asyncio.run(analysis_module.complete("test", Observations, [], "mistralai/mistral-large-4-0"))


def test_timeout_produces_actionable_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-test-key")
    def respond(request):
        raise httpx.ReadTimeout("upstream timeout")
    client_type = httpx.AsyncClient
    monkeypatch.setattr(completion.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(ValueError, match="took too long"):
        asyncio.run(analysis_module.complete("test", Observations, [], "mistralai/mistral-large-4-0"))


def test_evidence_spans_come_from_frames_not_model():
    lookup = {"a": {"timestamp": 1.2}, "b": {"timestamp": 3.8}}
    result = resolve_span({"start_frame_id": "a", "end_frame_id": "b", "observed_span_seconds": 99}, lookup)
    assert result["observed_span_seconds"] == 2.6
    with pytest.raises(ValueError, match="invalid evidence"):
        resolve_span({"start_frame_id": "invented", "end_frame_id": "b"}, lookup)
    with pytest.raises(ValueError, match="invalid evidence"):
        resolve_span({"start_frame_id": "b", "end_frame_id": "a"}, lookup)


def test_rejects_double_counting_of_workflow_instances():
    proposal = Proposal.model_validate({
        "title": "Example", "summary": "Example", "recommendations": [], "limitations": [],
        "workflow": {"name": "Example", "description": "Example", "repetition_assessment": "Unknown",
                     "occurrences": [{"start_frame_id": "a", "end_frame_id": "c", "description": "One"},
                                     {"start_frame_id": "b", "end_frame_id": "c", "description": "Two"}]}})
    with pytest.raises(ValueError, match="overlapping"):
        finalize(proposal, [], [{"id": "a", "timestamp": 0}, {"id": "b", "timestamp": 1}, {"id": "c", "timestamp": 2}])


def test_screenshot_coverage_rejects_hidden_return_visits_and_missing_frames():
    frames = [{"id": "mail", "timestamp": 0}, {"id": "calendar", "timestamp": 4},
              {"id": "return", "timestamp": 9}]
    with pytest.raises(ValueError, match="overlap"):
        validate_step_coverage([
            {"start_frame_id": "mail", "end_frame_id": "return"},
            {"start_frame_id": "calendar", "end_frame_id": "calendar"}], frames)
    with pytest.raises(ValueError, match="calendar"):
        validate_step_coverage([
            {"start_frame_id": "mail", "end_frame_id": "mail"},
            {"start_frame_id": "return", "end_frame_id": "return"}], frames)
    steps = [{"start_frame_id": frame["id"], "end_frame_id": frame["id"]} for frame in reversed(frames)]
    assert [s["start_frame_id"] for s in validate_step_coverage(steps, frames)] == ["mail", "calendar", "return"]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_analysis_repairs_screenshot_coverage_before_making_suggestions(tmp_path, monkeypatch, repair_succeeds):
    frames = [{"id": "f0001", "timestamp": 0}, {"id": "f0002", "timestamp": 4}]
    approval = review_test_frames(tmp_path, frames)
    def observation(frame):
        return {"title": "Visible view", "application": "Mail", "description": "A message is visible.",
                "start_frame_id": frame, "end_frame_id": frame, "evidence_type": "observed",
                "confidence": "high", "uncertainty": "None"}
    calls = []
    async def fake_complete(content, schema, usage, model, on_progress=None):
        calls.append(schema)
        if schema is Observations:
            ids = ["f0001"] if len(calls) == 1 or not repair_succeeds else ["f0001", "f0002"]
            if len(calls) == 2:
                assert "f0002" in content[-1]["text"]
            return Observations.model_validate({"steps": [observation(f) for f in ids], "limitations": []})
        assert calls == [Observations, Observations, Proposal]
        return Proposal.model_validate({"title": "Review messages", "summary": "Two views.",
            "recommendations": [], "limitations": [], "workflow": {"name": "Review", "description": "Review",
            "repetition_assessment": "Not established", "occurrences": []}})
    monkeypatch.setattr(analysis_module, "complete", fake_complete)
    updates = []
    job = {"frames": frames, "metadata": {"duration": 5}, "sample_interval": 1, "privacy_approval": approval}
    if repair_succeeds:
        report = asyncio.run(analysis_module.analyze(tmp_path, job, lambda **values: updates.append(values)))
        assert [s["evidence_frame_ids"] for s in report["timeline"]] == [["f0001"], ["f0002"]]
        assert any(update.get("progress") == 67 for update in updates)
    else:
        with pytest.raises(ValueError, match="not described in order"):
            asyncio.run(analysis_module.analyze(tmp_path, job, lambda **values: updates.append(values)))
        assert calls == [Observations, Observations]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_real_video_extraction_and_corrupt_input(tmp_path):
    video = tmp_path / "input.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=duration=3.1:size=320x180:rate=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    folder = tmp_path / "job"
    folder.mkdir()
    metadata = probe(video)
    frames, interval = extract_frames(video, folder, metadata)
    assert 3 <= metadata["duration"] <= 3.2
    assert frames[0]["timestamp"] == 0
    assert frames[-1]["timestamp"] == 3
    assert all(0 <= f["timestamp"] <= metadata["duration"] for f in frames)
    assert all((folder / "frames" / f"{f['id']}.jpg").is_file() for f in frames)
    invalid = tmp_path / "invalid.mp4"
    invalid.write_text("not a video")
    with pytest.raises(ValueError, match="could not be read"):
        probe(invalid)


def test_retry_reuses_validated_observations_for_the_same_model(tmp_path, monkeypatch):
    frames = [{"id": "f0001", "timestamp": 0}, {"id": "f0002", "timestamp": 4}]
    approval = review_test_frames(tmp_path, frames)
    steps = [{"title": "Visible view", "application": "Mail", "description": "A message is visible.",
              "start_frame_id": f['id'], "end_frame_id": f['id'], "evidence_type": "observed",
              "confidence": "high", "uncertainty": "None"} for f in frames]
    model = get_model()
    job = {"model": model, "frames": frames, "metadata": {"duration": 5}, "sample_interval": 1,
           "privacy_approval": approval, "observation_revision": approval['revision'],
           "observations": steps, "observation_model": model,
           "observation_usage": [{"model": model, "completion_tokens": 200}]}
    async def fake_complete(content, schema, usage, model, on_progress=None):
        assert schema is Proposal  # No second paid vision request.
        assert usage[0]['completion_tokens'] == 200
        return Proposal.model_validate({"title": "Review messages", "summary": "Two views.",
            "recommendations": [], "limitations": [], "workflow": {"name": "Review", "description": "Review",
            "repetition_assessment": "Not established", "occurrences": []}})
    monkeypatch.setattr(analysis_module, 'complete', fake_complete)
    report = asyncio.run(analysis_module.analyze(tmp_path, job, lambda **values: None))
    assert len(report['timeline']) == 2 and report['reused_observations']


def review_test_frames(folder, frames):
    redacted = folder / 'redacted'
    redacted.mkdir()
    for frame in frames:
        (redacted / f"{frame['id']}.jpg").write_bytes(b'\xff\xd8mock-image\xff\xd9')
    return review(folder, frames, [frame['id'] for frame in frames], '', get_model()).selection()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(main, "JOBS", {})
    monkeypatch.setattr(main.automation_store, 'DATABASE', tmp_path / 'automation' / 'state.sqlite3')
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-sent")
    monkeypatch.setenv("OPENROUTER_MODEL", "mistralai/mistral-large-4-0")
    with TestClient(main.app) as client:
        yield client


def test_secrets_are_not_served_and_cross_origin_jobs_rejected(client):
    assert client.get("/.env").status_code == 404
    assert client.get("/api/jobs/../../.env").status_code == 404
    assert "test-key-never-sent" not in client.get("/api/health").text
    assert client.post("/api/jobs", headers={"Origin": "https://unrelated.example"}).status_code == 403


def test_upload_validation_and_cleanup(client, tmp_path, monkeypatch):
    assert client.post("/api/jobs", files={"file": ("file.txt", b"hello")}).status_code == 415
    assert client.post("/api/jobs", files={"file": ("file.mp4", b"")}).status_code == 400
    monkeypatch.setattr(main, "MAX_BYTES", 4)
    assert client.post("/api/jobs", files={"file": ("file.mp4", b"12345")}).status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_corrupt_video_fails_cleanly_without_api_call(client):
    import time
    response = client.post("/api/jobs", files={"file": ("corrupt.mp4", b"not a video")})
    assert response.status_code == 202
    job_id = response.json()["id"]
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "failed":
            break
        time.sleep(.02)
    assert job["status"] == "failed"
    assert "could not be read" in job["message"]
    assert client.get(f"/api/jobs/{job_id}/export/json").status_code == 409



def test_inbox_endpoint_accepts_only_safe_names(client, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert client.post("/api/jobs/from-inbox", data={}).status_code == 422
    assert client.post("/api/jobs/from-inbox", data={"name": "../job.json.mp4"}).status_code == 400
    assert client.post("/api/jobs/from-inbox", data={"name": "zzzz.mp4"}).status_code == 400
    assert client.post("/api/jobs/from-inbox", data={"name": "a" * 32 + ".txt"}).status_code == 400
    assert client.post("/api/jobs/from-inbox", data={"name": "a" * 32 + ".mp4"}).status_code == 404
    good = "b" * 32 + ".mov"
    (inbox / good).write_bytes(b"not a video")
    response = client.post("/api/jobs/from-inbox", data={"name": good, "filename": "../demo.mov"})
    assert response.status_code == 202
    job = response.json()
    assert job["filename"] == "demo.mov"
    assert not (inbox / good).exists()
    assert (tmp_path / job["id"] / "source.mov").read_bytes() == b"not a video"


def test_delete_removes_only_the_saved_recording_and_report(client, tmp_path):
    original = tmp_path / "original.mov"
    original.write_bytes(b"original recording")
    job = main.new_job(original.name, "")
    job.update(status="completed", source_name="source.mov")
    main.persist(job)
    folder = tmp_path / job["id"]
    (folder / "source.mov").write_bytes(original.read_bytes())
    (folder / "frames").mkdir()
    (folder / "frames" / "f0000.jpg").write_bytes(b"screenshot")
    other = main.new_job("another.mov", "")
    other.update(status="failed")
    main.persist(other)
    assert client.delete(f"/api/jobs/{job['id']}", headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert folder.exists()
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert not folder.exists()
    assert original.read_bytes() == b"original recording"
    assert (tmp_path / other["id"] / "job.json").is_file()
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 404
    assert [item["id"] for item in client.get("/api/jobs").json()] == [other["id"]]


@pytest.mark.parametrize("status", ["queued", "processing"])
def test_cannot_delete_active_recordings(client, tmp_path, status):
    job = main.new_job("active.mov", "")
    job.update(status=status)
    main.persist(job)
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 409
    assert (tmp_path / job["id"] / "job.json").is_file()
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == status
