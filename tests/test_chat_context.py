import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import llm_chatbot.discord_bot as runtime
from llm_chatbot.config import load_config
from llm_chatbot.memory import MemoryStore
from llm_chatbot.personality import DEFAULT_PERSONALITY


def context_fixture(monkeypatch, tmp_path, include_non_addressed=True):
    for name, value in {
        "BOT_MODE": "chat",
        "TEXT_API_BASE_URL": "https://backend.invalid/v1",
        "TEXT_API_KEY": "fixture",
        "TEXT_GUILD_IDS": "1",
        "TEXT_CHANNEL_IDS": "2",
        "CONTEXT_STORE_PATH": str(tmp_path / "context.json"),
        "XDG_CACHE_HOME": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("IMAGE_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("TEXT_ROLE_IDS", raising=False)
    cfg = load_config()
    store = MemoryStore(cfg.store_path)
    monkeypatch.setattr(runtime, "MemoryStore", lambda _: store)
    persona = replace(DEFAULT_PERSONALITY)
    persona.listen = replace(persona.listen, enabled=False, judge_enabled=False)
    persona.context = replace(persona.context, include_non_addressed_messages=include_non_addressed)
    guild = SimpleNamespace(id=1, name="fixture", members=[], emojis=[])
    channel = SimpleNamespace(id=2, name="fixture", guild=guild, send=AsyncMock())

    serial = 100

    def message(content, addressed=False, user=11):
        nonlocal serial
        serial += 1
        return SimpleNamespace(
            id=serial,
            content=content,
            addressed=addressed,
            channel=channel,
            guild=guild,
            webhook_id=None,
            author=SimpleNamespace(id=user, display_name="Alice" if user == 11 else "Bob", bot=False, roles=[]),
        )

    return cfg, store, persona, channel, message


@pytest.mark.parametrize("include", [False, True])
def test_non_addressed_human_is_recorded_without_reply_then_included_by_setting(monkeypatch, tmp_path, include):
    cfg, store, persona, channel, message = context_fixture(monkeypatch, tmp_path, include)

    async def scenario():
        bot = runtime.build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        bot.text_backend.complete = AsyncMock(return_value=("ack", (1, 1, 0)))
        await bot.on_message(message("that joke was awful"))
        channel.send.assert_not_awaited()
        bot.text_backend.complete.assert_not_awaited()
        assert store.get(2).turns == 0
        assert MemoryStore(cfg.store_path).get(2).messages[0]["addressed"] is False
        await bot.on_message(message("what did Alice say?", True, 12))
        convo = bot.text_backend.complete.await_args.args[0]
        contents = [item["content"] for item in convo]
        assert ("Alice: that joke was awful" in contents) is include
        assert contents.count("Bob: what did Alice say?") == 1
        assert store.get(2).turns == 1
        await bot.close()

    asyncio.run(scenario())


def test_messages_during_inference_are_kept_for_next_turn_without_changing_current_input(monkeypatch, tmp_path):
    cfg, store, persona, channel, message = context_fixture(monkeypatch, tmp_path)

    async def scenario():
        bot = runtime.build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        entered, release = asyncio.Event(), asyncio.Event()
        inputs = []

        async def complete(convo):
            inputs.append(convo)
            if len(inputs) == 1:
                async with bot.text_backend.lock:
                    entered.set()
                    await release.wait()
            return "ack", (1, 1, 0)

        bot.text_backend.complete = complete
        task = asyncio.create_task(bot.on_message(message("first question", True, 12)))
        await entered.wait()
        await bot.on_message(message("a comment during inference"))
        channel.send.assert_not_awaited()
        release.set()
        await task
        assert "Alice: a comment during inference" not in [item["content"] for item in inputs[0]]
        await bot.on_message(message("what was that comment?", True, 12))
        assert "Alice: a comment during inference" in [item["content"] for item in inputs[1]]
        await bot.close()

    asyncio.run(scenario())


def test_context_recording_keeps_access_boundaries_commands_and_bot_exclusions(monkeypatch, tmp_path):
    cfg, store, persona, channel, message = context_fixture(monkeypatch, tmp_path)

    async def scenario():
        bot = runtime.build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        for mode in ("guild", "channel", "role", "bot", "webhook", "command"):
            m = message("excluded")
            if mode == "guild":
                m.guild = SimpleNamespace(id=9)
            elif mode == "channel":
                m.channel = SimpleNamespace(id=9)
            elif mode == "role":
                cfg.text_role_ids = frozenset({77})
            elif mode == "bot":
                m.author.bot = True
            elif mode == "webhook":
                m.webhook_id = 99
            else:
                m.content = (persona.command_prefix or cfg.command_prefix) + "context"
            await bot.on_message(m)
            cfg.text_role_ids = frozenset()
        assert store.get(2).messages == []
        for index in range(105):
            await bot.on_message(message(f"observed {index}"))
        assert len(store.get(2).messages) == 100 and store.get(2).turns == 0
        channel.send.assert_not_awaited()
        await bot.close()

    asyncio.run(scenario())


def test_addressed_messages_queue_with_previous_answer_and_without_future_inputs(monkeypatch, tmp_path):
    cfg, store, persona, channel, message = context_fixture(monkeypatch, tmp_path)

    async def scenario():
        bot = runtime.build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        entered, release = asyncio.Event(), asyncio.Event()
        inputs = []

        async def complete(convo):
            inputs.append([item["content"] for item in convo])
            if len(inputs) == 1:
                entered.set()
                await release.wait()
            return "answer " + str(len(inputs)), (1, 1, 0)

        bot.text_backend.complete = complete
        first, second = message("first", True), message("second", True, 12)
        for msg in (first, second):
            msg.add_reaction, msg.remove_reaction = AsyncMock(), AsyncMock()
        one = asyncio.create_task(bot.on_message(first))
        await entered.wait()
        two = asyncio.create_task(bot.on_message(second))
        await asyncio.sleep(0)
        second.add_reaction.assert_awaited_once_with("📝")
        await bot.on_message(message("future comment"))
        assert not two.done() and len(inputs) == 1
        release.set()
        await asyncio.gather(one, two)
        assert "Alice: first" in inputs[1] and "answer 1" in inputs[1]
        assert inputs[1].index("answer 1") < inputs[1].index("Bob: second")
        assert "Alice: future comment" not in inputs[1]
        assert [item["content"] for item in store.get(2).messages] == [
            "Alice: first",
            "answer 1",
            "Bob: second",
            "answer 2",
            "Alice: future comment",
        ]
        assert [call.args[0] for call in channel.send.await_args_list] == ["answer 1", "answer 2"]
        for msg in (first, second):
            msg.remove_reaction.assert_awaited_once()
        await bot.close()

    asyncio.run(scenario())


def test_queued_reply_rechecks_role_before_inference(monkeypatch, tmp_path):
    cfg, store, persona, channel, message = context_fixture(monkeypatch, tmp_path)
    cfg.text_role_ids = frozenset({77})

    async def scenario():
        bot = runtime.build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def complete(convo):
            calls.append(convo)
            entered.set()
            await release.wait()
            return "answer", (1, 1, 0)

        bot.text_backend.complete = complete
        first, second = message("first", True), message("second", True, 12)
        for msg in (first, second):
            msg.author.roles = [SimpleNamespace(id=77)]
        one = asyncio.create_task(bot.on_message(first))
        await entered.wait()
        two = asyncio.create_task(bot.on_message(second))
        await asyncio.sleep(0)
        second.author.roles = []
        release.set()
        await asyncio.gather(one, two)
        assert len(calls) == 1
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("requested,expected", [(5, 5), (100, 100), (150, 100)])
def test_runtime_context_respects_one_hundred_message_window(monkeypatch, tmp_path, requested, expected):
    cfg, _, persona, _, message = context_fixture(monkeypatch, tmp_path)
    persona.context = replace(persona.context, include_last_n=requested)

    async def scenario():
        bot = runtime.build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        bot.text_backend.complete = AsyncMock(return_value=("ack", (1, 1, 0)))
        for index in range(105):
            await bot.on_message(message(f"observed {index}"))
        await bot.on_message(message("question", True))
        conversation = bot.text_backend.complete.await_args.args[0]
        history = [m["content"] for m in conversation if m["role"] in {"user", "assistant"}]
        assert len(history) == expected
        assert history[0] == f"Alice: observed {106 - expected}"
        assert history[-1] == "Alice: question"
        await bot.close()

    asyncio.run(scenario())
