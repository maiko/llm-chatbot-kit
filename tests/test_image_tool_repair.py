import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_chatbot.image_tools import _has_unexecuted_tool_text, complete_with_image_tool


def feature():
    cfg = SimpleNamespace(presets={"fixture": {}}, default_preset="fixture", edits_enabled=False, prompt_guidance="")
    return SimpleNamespace(cfg=cfg, language="fr", from_message=AsyncMock())


def valid_response():
    return {
        "content": None,
        "tool_calls": [{"type": "function", "function": {"name": "generate_image", "arguments": '{"prompt":"A sunset over the sea."}'}}],
    }


@pytest.mark.parametrize(
    "text",
    [
        '<call:generate_image{prompt:<|"|>A sunset<|"|>}>',
        '<call:edit_image{prompt:<|"|>Blue hair<|"|>}>',
        '<|tool_call>call:generate_image{prompt:<|"|>A sunset<|"|>}<tool_call|>',
        '<tool_call>{"name":"generate_image","arguments":{"prompt":"A sunset"}}</tool_call>',
    ],
)
def test_completed_textual_call_repairs_once_before_admission(text):
    async def scenario():
        images = feature()
        client = SimpleNamespace(complete_message=AsyncMock(side_effect=[({"content": text}, (2, 3, 0)), (valid_response(), (5, 7, 0))]))
        request = [{"role": "user", "content": "Draw a sunset."}]
        message = object()
        receipt, usage = await complete_with_image_tool(client, images, request, message)
        assert receipt == "" and usage == (7, 10, 0)
        assert client.complete_message.await_count == 2
        images.from_message.assert_awaited_once_with(message, {"prompt": "A sunset over the sea."})
        retry, tools = client.complete_message.await_args.args
        assert retry[0] == request[0] and tools[0]["function"]["name"] == "generate_image"
        assert "Nothing was executed" in retry[-1]["content"]
        assert text not in str(retry)  # Never re-inject the malformed arguments.

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "repaired",
    [
        {"content": '<call:generate_image{prompt:<|"|>A sunset<|"|>}>'},
        {"content": '<call:generate_image{prompt:<|"|>A sunset<|"|>}>', "tool_calls": valid_response()["tool_calls"]},
    ],
)
def test_failed_repair_does_not_leak_call_or_admit(repaired):
    async def scenario():
        images = feature()
        client = SimpleNamespace(
            complete_message=AsyncMock(side_effect=[({"content": "<call:generate_image{}>"}, (1, 2, 0)), (repaired, (3, 4, 0))])
        )
        receipt, usage = await complete_with_image_tool(client, images, [], object())
        assert "Génération non lancée" in receipt and "<call:" not in receipt
        assert usage == (4, 6, 0) and client.complete_message.await_count == 2
        images.from_message.assert_not_awaited()

    asyncio.run(scenario())


def test_repair_can_return_normal_clarification_without_forcing_generation():
    async def scenario():
        images = feature()
        client = SimpleNamespace(
            complete_message=AsyncMock(
                side_effect=[({"content": "<call:generate_image{}>"}, (1, 2, 0)), ({"content": "Which subject?"}, (3, 4, 0))]
            )
        )
        receipt, _ = await complete_with_image_tool(client, images, [], object())
        assert receipt == "Which subject?"
        images.from_message.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [valid_response(), {"content": "Hello!"}, {"content": "`<call:generate_image{}>`"}, {"content": "```\n<call:generate_image{}>\n```"}],
)
def test_valid_calls_normal_text_and_code_examples_are_never_retried(response):
    async def scenario():
        images = feature()
        client = SimpleNamespace(complete_message=AsyncMock(return_value=(response, (1, 2, 0))))
        await complete_with_image_tool(client, images, [], object())
        client.complete_message.assert_awaited_once()

    asyncio.run(scenario())


def test_ambiguous_backend_failure_is_not_retried():
    async def scenario():
        images = feature()
        client = SimpleNamespace(complete_message=AsyncMock(side_effect=TimeoutError()))
        with pytest.raises(TimeoutError):
            await complete_with_image_tool(client, images, [], object())
        client.complete_message.assert_awaited_once()
        images.from_message.assert_not_awaited()

    asyncio.run(scenario())


def test_detection_does_not_execute_or_guess_arguments():
    assert not _has_unexecuted_tool_text(None)
    assert not _has_unexecuted_tool_text("<call:arbitrary_tool{}>")
    assert not _has_unexecuted_tool_text("`<call:generate_image{}>`")
