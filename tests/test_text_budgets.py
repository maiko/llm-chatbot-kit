import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from llm_chatbot.config import load_config
from llm_chatbot.discord_bot import build_bot
from llm_chatbot.text_client import ChatCompletionsClient
from test_chat_context import context_fixture


@pytest.mark.parametrize("stream,tools", [(False, False), (False, True), (True, False), (True, True)])
def test_configured_output_budgets_apply_to_both_transports(stream, tools):
    async def scenario():
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            assert body["max_tokens"] == (3072 if tools else 777)
            if stream:
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

        client = ChatCompletionsClient(
            "https://fixture.invalid/v1",
            "fixture",
            "fixture",
            transport=httpx.MockTransport(handle),
            max_tokens=777,
            tool_max_tokens=3072,
            timeout_seconds=240,
        )
        assert client.http.timeout.read == 240 and client.http.timeout.connect == 10
        offered = [{"type": "function", "function": {"name": "fixture"}}] if tools else None
        if stream:
            assert len([event async for event in client.events([], offered)]) == 1
        else:
            assert (await client.complete_message([], offered))[0]["content"] == "ok"
        assert len(requests) == 1
        await client.close()

    asyncio.run(scenario())


def test_runtime_uses_environment_budgets(monkeypatch, tmp_path):
    cfg, _, persona, _, _ = context_fixture(monkeypatch, tmp_path)
    for key, value in {"TEXT_MAX_TOKENS": "777", "TEXT_TOOL_MAX_TOKENS": "3072", "TEXT_TIMEOUT_SECONDS": "240"}.items():
        monkeypatch.setenv(key, value)
    cfg = load_config()

    async def scenario():
        bot = build_bot(cfg, persona)
        assert bot.text_backend.max_tokens == 777 and bot.text_backend.tool_max_tokens == 3072
        assert bot.text_backend.http.timeout.read == 240
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "key,value",
    [
        ("TEXT_MAX_TOKENS", "0"),
        ("TEXT_TOOL_MAX_TOKENS", "16385"),
        ("TEXT_TIMEOUT_SECONDS", "29"),
        ("TEXT_TIMEOUT_SECONDS", "601"),
        ("TEXT_MAX_TOKENS", "abc"),
    ],
)
def test_bad_environment_budget_fails_at_startup(monkeypatch, tmp_path, key, value):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        load_config()


@pytest.mark.parametrize("stream", [False, True])
def test_runtime_reports_truncation_without_retry(monkeypatch, tmp_path, stream):
    cfg, _, persona, channel, message = context_fixture(monkeypatch, tmp_path)
    persona = replace(persona, language="fr")
    from test_stream_delivery import typing

    channel.typing = typing

    async def scenario():
        bot = build_bot(cfg, persona, stream=stream)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        attempts = []

        async def complete(_):
            attempts.append(1)
            raise RuntimeError("text_backend_response_truncated")

        async def deltas(_):
            attempts.append(1)
            raise RuntimeError("text_backend_response_truncated")
            yield "unreachable"

        bot.text_backend.complete = complete
        bot.text_backend.deltas = deltas
        await bot.on_message(message("hello", True))
        assert len(attempts) == 1
        assert channel.send.await_count == 1
        assert "limite de sortie" in channel.send.await_args.args[0]
        await bot.close()

    asyncio.run(scenario())


def test_truncated_tool_never_admitted_and_long_complete_tool_is_accepted():
    from llm_chatbot.image_config import ImageConfig
    from llm_chatbot.image_tools import complete_with_image_tool

    async def scenario():
        attempts = []
        truncated = True
        prompt = "neutral landscape " * 180
        call = {"type": "function", "function": {"name": "generate_image", "arguments": json.dumps({"prompt": prompt})}}

        def handle(request):
            attempts.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"tool_calls": [call]}, "finish_reason": "length" if truncated else "tool_calls"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 1500},
                },
            )

        client = ChatCompletionsClient("https://fixture.invalid/v1", "fixture", "fixture", transport=httpx.MockTransport(handle))
        feature = SimpleNamespace(
            cfg=ImageConfig(
                "http://fixture.invalid/v1", "fixture", frozenset({1}), frozenset({2}), frozenset(), __import__("pathlib").Path("unused")
            ),
            language="fr",
            from_message=AsyncMock(),
        )
        with pytest.raises(RuntimeError, match="truncated"):
            await complete_with_image_tool(client, feature, [], object())
        feature.from_message.assert_not_awaited()
        assert len(attempts) == 1 and attempts[0]["max_tokens"] == 4096
        truncated = False
        receipt, usage = await complete_with_image_tool(client, feature, [], object())
        assert receipt == "" and usage[1] == 1500
        feature.from_message.assert_awaited_once()
        assert feature.from_message.await_args.args[1]["prompt"] == prompt
        await client.close()

    asyncio.run(scenario())
