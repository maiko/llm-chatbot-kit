import asyncio
import base64
import re
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import llm_chatbot.image_client as module
from llm_chatbot.image_client import ImageClient
from llm_chatbot.image_jobs import ImageWorker, JobStore
from llm_chatbot.image_status import render_status
from test_images import PNG, admit, settings


@pytest.mark.parametrize(
    "telemetry",
    [
        {"phase": "sampling", "value": 12, "max": 25, "prompt": "must never leave backend"},
        {"phase": "sampling", "value": 26, "max": 25},
        {"phase": "sampling", "value": True, "max": 25},
        {"phase": "sampling", "value": 1, "max": 0},
        {"phase": "<@everyone>"},
        b"x" * 4097,
        "forbidden",
    ],
)
def test_optional_poll_is_correlated_bounded_and_never_retries_generation(monkeypatch, tmp_path, telemetry):
    monkeypatch.setattr(module, "PROGRESS_POLL_SECONDS", 0.001)

    async def scenario():
        cfg = replace(settings(tmp_path), progress_enabled=True)
        progressed = asyncio.Event()
        requests, updates, tokens = [], [], []

        async def handle(request):
            requests.append(request.method)
            assert request.headers["Authorization"] == "Bearer private-key"
            if request.method == "POST":
                token = request.headers["X-Image-Request-ID"]
                assert re.fullmatch(r"[a-f0-9]{32}", token)
                tokens.append(token)
                await progressed.wait()
                return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
            assert request.url.path.endswith("/images/progress/" + tokens[0])
            if telemetry == "forbidden":
                return httpx.Response(403, text="private backend error")
            if isinstance(telemetry, bytes):
                return httpx.Response(200, content=telemetry)
            return httpx.Response(200, json=telemetry)

        def update(value):
            updates.append(value)
            progressed.set()

        client = ImageClient(cfg, httpx.MockTransport(handle))
        assert await asyncio.wait_for(client.generate({"prompt": "fixture"}, on_progress=update), 1) == PNG
        assert requests.count("POST") == 1 and updates
        if isinstance(telemetry, dict) and telemetry.get("value") == 12:
            assert updates[0] == {"phase": "sampling", "value": 12, "max": 25}
        else:
            assert updates[0] == {"phase": "unavailable", "value": None, "max": None}
        count = len(requests)
        await asyncio.sleep(0.01)
        assert len(requests) == count  # Poller cleaned up when POST completes.
        await client.close()

    asyncio.run(scenario())


def test_disabled_progress_preserves_standard_image_contract(tmp_path):
    async def scenario():
        def handle(request):
            assert request.method == "POST" and "X-Image-Request-ID" not in request.headers
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        client = ImageClient(settings(tmp_path), httpx.MockTransport(handle))
        callback = AsyncMock()
        assert await client.generate({"prompt": "fixture"}, on_progress=callback) == PNG
        callback.assert_not_called()
        await client.close()

    asyncio.run(scenario())


def test_sampler_percentage_changes_to_decode_phase_and_is_not_persisted(tmp_path):
    store = JobStore(settings(tmp_path))
    job = admit(store)
    store.update(job["id"], "running")
    store.set_progress(job["id"], {"phase": "sampling", "value": 12, "max": 25})
    text = render_status(store.get(job["id"]), store.queue_snapshot(job["id"]))
    assert "[████░░░░░░] 48% · 12/25" in text and "Échantillonnage" in text
    store.set_progress(job["id"], {"phase": "decoding", "value": None, "max": None})
    text = render_status(store.get(job["id"]), store.queue_snapshot(job["id"]))
    assert "Décodage" in text and "%" not in text
    store.close()
    store = JobStore(settings(tmp_path))
    assert store.get(job["id"])["progress"] is None
    assert store.get(job["id"])["state"] == "unknown"
    store.set_progress(job["id"], {"phase": "sampling", "value": 25, "max": 25})
    assert store.get(job["id"])["progress"] is None
    store.close()


def test_worker_progress_does_not_overwrite_terminal_delivery(tmp_path):
    async def scenario():
        cfg = replace(settings(tmp_path), progress_enabled=True)
        store = JobStore(cfg)
        job = admit(store)
        callbacks = []

        async def generate(payload, on_progress):
            callbacks.append(on_progress)
            on_progress({"phase": "sampling", "value": 5, "max": 10})
            assert "50%" in render_status(store.get(job["id"]), store.queue_snapshot(job["id"]))
            return PNG

        worker = ImageWorker(store, SimpleNamespace(generate=generate), AsyncMock(return_value="999"))
        await worker.process(job)
        callbacks[0]({"phase": "sampling", "value": 10, "max": 10})
        assert store.get(job["id"])["state"] == "sent" and store.get(job["id"])["progress"] is None
        store.close()

    asyncio.run(scenario())
