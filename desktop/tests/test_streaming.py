import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from app import completion, main
from test_pipeline import client


class Result(BaseModel):
    summary: str


def event(payload):
    return 'data: ' + json.dumps(payload) + '\n\n'


def packet(content=None, finish=None, **extra):
    return {"model": "mistralai/mistral-large-4-0", "choices": [{
        "delta": {"content": content}, "finish_reason": finish}], **extra}


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-sent")
    original = httpx.AsyncClient
    def install(handler):
        monkeypatch.setattr(completion.httpx, "AsyncClient", lambda **kwargs: original(
            transport=httpx.MockTransport(handler), **kwargs))
    return install


class Pieces(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data.encode()
        self.closed = False

    async def __aiter__(self):
        for index in range(0, len(self.data), 7):
            yield self.data[index:index + 7]

    async def aclose(self):
        self.closed = True


def test_stream_counts_real_usage_once_and_never_exposes_text(transport, monkeypatch):
    # Ensure multiple content updates are published without waiting in the test.
    ticks = iter(range(1000))
    monkeypatch.setattr(completion, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    stream = Pieces(': heartbeat\n\n' + event(packet('{"summary":')) +
        event({"choices": [{"delta": {"reasoning": "private internal text", "reasoning_details": [
            {"type": "reasoning.text", "text": "private internal text"}]}}]}) +
        event(packet('"Done ✓"}', "stop")) + event({"choices": [], "usage": {
            "prompt_tokens": 250, "completion_tokens": 14, "completion_tokens_details": {"reasoning_tokens": 0}}}) + 'data: [DONE]\n\n')
    transport(lambda request: httpx.Response(200, stream=stream))
    updates, usage = [], []
    result = asyncio.run(completion.complete("test", Result, usage,
                        "mistralai/mistral-large-4-0", updates.append))
    assert result.summary == "Done ✓"
    assert any(s["tokens_estimated"] and s["output_tokens"] > 0 for s in updates)
    final = updates[-1]
    assert final["output_tokens"] == 14 and not final["tokens_estimated"]
    assert final["phase"] == "complete" and final["chunks_received"] == 3
    assert final["reasoning_characters_received"] == len("private internal text")
    assert any(s["phase"] == "thinking" for s in updates)
    assert final["input_tokens"] == 250 and final["finished_at"]
    assert len(usage) == 1 and usage[0]["completion_tokens"] == 14
    assert "private internal text" not in json.dumps(updates)
    assert "Done" not in json.dumps(updates) and "test-key" not in json.dumps(updates)
    assert stream.closed


def test_truncated_response_retries_with_larger_budget(transport):
    budgets = []
    def respond(request):
        body = json.loads(request.content)
        budgets.append(body["max_tokens"])
        if len(budgets) == 1:
            return httpx.Response(200, text=event(packet('{"summary":"unfinished', 'length')) + 'data: [DONE]\n\n')
        return httpx.Response(200, text=event(packet('{"summary":"complete"}', 'stop')) +
            event(packet(finish='stop', usage={"completion_tokens": 6})) + 'data: [DONE]\n\n')
    transport(respond)
    updates, usage = [], []
    result = asyncio.run(completion.complete("test", Result, usage,
                        "mistralai/mistral-large-4-0", updates.append))
    assert result.summary == "complete" and budgets == [16000, 32000]
    assert any(s["phase"] == 'retrying' and s["reason"] == 'response_limit' for s in updates)
    assert updates[-1]["attempt"] == 2 and updates[-1]["output_tokens"] == 6
    assert len(usage) == 2


@pytest.mark.parametrize("body", [event(packet('{"summary":"partial"}')), event(packet('```', 'stop')),
                                       event({"error": {"message": "private upstream payload"}})])
def test_incomplete_streams_and_invalid_results_are_bounded_and_safe(transport, body):
    calls, updates = [], []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, text=body)
    transport(respond)
    with pytest.raises(ValueError, match="complete result after retrying") as caught:
        asyncio.run(completion.complete("test", Result, [], "mistralai/mistral-large-4-0", updates.append))
    assert len(calls) == 3 and updates[-1]["phase"] == "failed"
    assert "private" not in str(caught.value) and "private" not in json.dumps(updates)


def test_exhausted_output_limit_does_not_blame_video_length(transport):
    budgets = []
    def respond(request):
        budgets.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, text=event(packet('{', 'length')))
    transport(respond)
    with pytest.raises(ValueError, match="recording is saved") as caught:
        asyncio.run(completion.complete("test", Result, [], "mistralai/mistral-large-4-0"))
    assert budgets == [16000, 32000, 64000]
    assert 'shorter' not in str(caught.value)


def test_rename_and_notes_persist_without_changing_source_or_context(client, tmp_path):
    job = main.new_job("original.mov", "original context")
    job.update(status="processing", source_name="source.mov")
    main.persist(job)
    source = tmp_path / job['id'] / 'source.mov'
    source.write_bytes(b'video')
    endpoint = f"/api/jobs/{job['id']}"
    updated = client.patch(endpoint, json={"display_name": "  Weekly planning  ", "notes": "Check calendar\nThen draft reply"})
    assert updated.status_code == 200
    saved = json.loads((source.parent / 'job.json').read_text())
    assert saved['display_name'] == 'Weekly planning' and saved['notes'] == 'Check calendar\nThen draft reply'
    assert saved['filename'] == 'original.mov' and saved['source_name'] == 'source.mov'
    assert saved['context'] == 'original context' and saved['status'] == 'processing'
    assert source.read_bytes() == b'video'
    assert client.get('/api/jobs').json()[0]['display_name'] == 'Weekly planning'
    assert client.patch(endpoint, json={"notes": ""}).json()['notes'] == ''
    assert client.get(endpoint).json()['display_name'] == 'Weekly planning'
    assert client.patch(endpoint, json={"display_name": "   "}).status_code == 422
    assert client.patch(endpoint, json={"display_name": "x\ny"}).status_code == 422
    assert client.patch(endpoint, json={"notes": "x" * 4001}).status_code == 422
    assert client.patch(endpoint, json={"source_name": "elsewhere.mov"}).status_code == 422
    assert client.patch(endpoint, json={"notes": "bad"}, headers={"Origin": "https://elsewhere.example"}).status_code == 403


def test_terminal_job_event_and_missing_job(client):
    job = main.new_job('finished.mov', '')
    job['status'] = 'failed'
    main.persist(job)
    response = client.get(f"/api/jobs/{job['id']}/events")
    assert response.headers['content-type'].startswith('text/event-stream')
    assert json.loads(response.text.removeprefix('data: ').strip())['id'] == job['id']
    assert job['id'] not in main.SUBSCRIBERS
    assert client.get('/api/jobs/missing/events').status_code == 404


def test_job_events_coalesce_slow_clients_and_clean_up_on_disconnect(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA', tmp_path)
    monkeypatch.setattr(main, 'JOBS', {})
    monkeypatch.setattr(main, 'SUBSCRIBERS', {})
    job = main.new_job('working.mov', '')
    main.persist(job)
    async def run():
        response = await main.job_events(job['id'])
        stream = response.body_iterator
        assert 'queued' in await anext(stream)
        for count in range(50):
            job.update(model_request={"output_tokens": count})
            main.persist(job)
        assert len(main.SUBSCRIBERS[job['id']]) == 1
        assert next(iter(main.SUBSCRIBERS[job['id']])).qsize() == 1
        latest = json.loads((await anext(stream)).removeprefix('data: ').strip())
        assert latest['model_request']['output_tokens'] == 49
        await stream.aclose()
        assert job['id'] not in main.SUBSCRIBERS
    asyncio.run(run())
