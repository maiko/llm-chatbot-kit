import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_chatbot.rate_limit import MultiKeySlidingWindow
from llm_chatbot.streaming import send_stream_as_messages


@asynccontextmanager
async def typing():
    yield


def test_reservation_waits_for_all_windows_without_consuming_other_dimensions():
    clock = [0.0]
    limiter = MultiKeySlidingWindow({"global": [(10, 3)], "user": [(5, 1), (30, 2)]}, now_func=lambda: clock[0])
    keys = {"global": "all", "user": "a"}
    assert limiter.reserve(keys) == 0
    clock[0] = 1
    assert limiter.reserve(keys) == pytest.approx(4.001)
    assert limiter.buckets["global"]["all"] == [0]
    clock[0] = 5.001
    assert limiter.reserve(keys) == 0
    clock[0] = 11
    assert limiter.reserve(keys) == pytest.approx(19.001)
    assert limiter.buckets["global"]["all"] == [5.001]
    clock[0] = 30.001
    assert limiter.reserve(keys) == 0


def test_invalid_quota_fails_instead_of_waiting_forever():
    limiter = MultiKeySlidingWindow({"user": [(30, 0)]})
    with pytest.raises(ValueError):
        limiter.reserve({"user": "a"})


@pytest.mark.parametrize("capped", [False, True])
def test_every_discord_length_split_and_final_tail_obeys_gate(capped):
    async def scenario():
        channel = SimpleNamespace(typing=typing, send=AsyncMock())
        gate = AsyncMock(return_value=True)
        text = "x" * 5800

        async def deltas():
            yield text

        result = await send_stream_as_messages(channel, deltas(), send_gate=gate, max_total_chars=4200 if capped else None)
        expected = text[:4200] if capped else text
        assert result == expected
        assert "".join(call.args[0] for call in channel.send.await_args_list) == expected
        assert gate.await_count == channel.send.await_count == (3 if capped else 4)
        assert all(len(call.args[0]) <= 1900 for call in channel.send.await_args_list)

    asyncio.run(scenario())


def test_denied_delivery_never_reports_unsent_text_as_complete():
    async def scenario():
        channel = SimpleNamespace(typing=typing, send=AsyncMock())

        async def deltas():
            yield "not delivered"

        with pytest.raises(RuntimeError, match="stream_delivery_denied"):
            await send_stream_as_messages(channel, deltas(), send_gate=lambda: False)
        channel.send.assert_not_awaited()

    asyncio.run(scenario())


def test_admitted_response_ignores_fragment_count_without_losing_text(monkeypatch):
    async def sleep(delay):
        assert delay < 1

    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def scenario():
        limiter = MultiKeySlidingWindow({"user": [(30, 1)]}, now_func=lambda: 0)
        assert limiter.reserve({"user": "a"}) == 0
        channel = SimpleNamespace(typing=typing, send=AsyncMock())
        parts = [f"part {i}.\n" for i in range(12)] + ["final tail"]

        async def deltas():
            for part in parts:
                yield part

        result = await send_stream_as_messages(channel, deltas(), min_first=1, min_next=1, rate_hz=1e9)
        assert result == "".join(parts) == "".join(call.args[0] for call in channel.send.await_args_list)
        assert channel.send.await_count > 3
        assert limiter.buckets["user"]["a"] == [0]
        assert limiter.reserve({"user": "a"}) > 0

    asyncio.run(scenario())
