import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_chatbot.reply_queue import ReplyQueue, ReplyQueueFull


def message(number):
    return SimpleNamespace(id=number, add_reaction=AsyncMock(), remove_reaction=AsyncMock())


def test_fifo_capacity_reaction_failure_and_shutdown():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def respond(msg, primary, history):
            order.append(msg.id)
            if msg.id == 1:
                entered.set()
                await release.wait()

        user = object()
        queue = ReplyQueue(respond, lambda: user, limit=1)
        first, second, third = message(1), message(2), message(3)
        second.add_reaction.side_effect = PermissionError()
        first_task = asyncio.create_task(queue.submit(first, []))
        await entered.wait()
        second_task = asyncio.create_task(queue.submit(second, []))
        await asyncio.sleep(0)
        # Admission is synchronous after the mocked reaction returns.
        assert queue.busy and order == [1]
        with pytest.raises(ReplyQueueFull):
            await queue.submit(third, [])
        third.add_reaction.assert_not_called()
        release.set()
        await asyncio.gather(first_task, second_task)
        assert order == [1, 2] and not queue.busy
        first.add_reaction.assert_awaited_once_with("📝")
        for msg in (first, second):
            msg.remove_reaction.assert_awaited_once_with("📝", user)
        await queue.close()

    asyncio.run(scenario())


def test_shutdown_cleans_active_and_pending_reactions_without_inference_replay():
    async def scenario():
        entered = asyncio.Event()
        calls = []

        async def respond(msg, primary, history):
            calls.append(msg.id)
            entered.set()
            await asyncio.Event().wait()

        queue = ReplyQueue(respond, lambda: object())
        first, second = message(1), message(2)
        tasks = [asyncio.create_task(queue.submit(first, []))]
        await entered.wait()
        tasks.append(asyncio.create_task(queue.submit(second, [])))
        await asyncio.sleep(0)
        await queue.close()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert calls == [1] and not queue.busy
        first.remove_reaction.assert_awaited_once()
        second.remove_reaction.assert_awaited_once()

    asyncio.run(scenario())


def test_text_client_waits_for_shared_backend_lock_instead_of_rejecting():
    import httpx

    from llm_chatbot.text_client import ChatCompletionsClient

    async def scenario():
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]})

        client = ChatCompletionsClient("https://backend.invalid/v1", "fixture", "fixture", transport=httpx.MockTransport(handle))
        await client.lock.acquire()
        pending = asyncio.create_task(client.complete([{"role": "user", "content": "question"}]))
        await asyncio.sleep(0)
        assert not pending.done() and not calls
        client.lock.release()
        assert (await pending)[0] == "answer" and len(calls) == 1
        await client.http.aclose()

    asyncio.run(scenario())
