import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import llm_chatbot.discord_bot as runtime
from llm_chatbot.config import load_config
from llm_chatbot.memory import MemoryStore
from test_chat_context import context_fixture


def peer_fixture(monkeypatch, tmp_path):
    cfg, store, persona, channel, make_message = context_fixture(monkeypatch, tmp_path)
    cfg.text_bot_chat_enabled = True
    cfg.text_bot_chat_peer_ids = frozenset({777})

    def peer(content="hello", author=777, direct=True):
        msg = make_message(content, True, author)
        msg.author.bot = True
        msg.mentions = [SimpleNamespace(id=555)] if direct else []
        return msg

    def build():
        bot = runtime.build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        bot.text_backend.complete = AsyncMock(return_value=("reply", (1, 1, 0)))
        return bot

    return cfg, store, persona, channel, make_message, peer, build


@pytest.mark.parametrize(
    "case", ["disabled", "unlisted", "self", "not_direct", "webhook", "dm", "guild", "channel", "role", "command", "mention_off"]
)
def test_peer_messages_preserve_default_and_access_exclusions(monkeypatch, tmp_path, case):
    cfg, store, persona, _, _, peer, build = peer_fixture(monkeypatch, tmp_path)

    async def scenario():
        bot = build()
        msg = peer()
        if case == "disabled":
            cfg.text_bot_chat_enabled = False
        elif case == "unlisted":
            msg.author.id = 999
        elif case == "self":
            cfg.text_bot_chat_peer_ids = frozenset({555})
            msg.author.id = 555
        elif case == "not_direct":
            msg.mentions = []
        elif case == "webhook":
            msg.webhook_id = 123
        elif case == "dm":
            msg.guild = None
        elif case == "guild":
            msg.guild.id = 9
        elif case == "channel":
            msg.channel.id = 9
        elif case == "role":
            cfg.text_role_ids = frozenset({42})
        elif case == "mention_off":
            persona.triggers = replace(persona.triggers, on_mention=False)
        else:
            msg.content = cfg.command_prefix + "reset"
        await bot.on_message(msg)
        bot.text_backend.complete.assert_not_awaited()
        bot.process_commands.assert_not_awaited()
        assert not store.get(2).messages
        await bot.close()

    asyncio.run(scenario())


def test_peer_budget_stops_loop_persists_and_resets_only_on_human_peer_mention(monkeypatch, tmp_path):
    cfg, store, _, channel, human, peer, build = peer_fixture(monkeypatch, tmp_path)

    async def scenario():
        bot = build()
        for _ in range(5):
            await bot.on_message(peer())
        assert bot.text_backend.complete.await_count == 3
        assert channel.send.await_count == 3
        assert MemoryStore(cfg.store_path).guild_settings(1)["bot_chat_replies"]["2"] == 3
        await bot.close()
        monkeypatch.setattr(runtime, "MemoryStore", MemoryStore)
        bot = build()
        await bot.on_message(peer())
        bot.text_backend.complete.assert_not_awaited()
        chatter = human("ordinary human chatter")
        chatter.mentions = []
        await bot.on_message(chatter)
        await bot.on_message(peer())
        bot.text_backend.complete.assert_not_awaited()
        restart = human("hello peer")
        restart.mentions = [SimpleNamespace(id=777)]
        await bot.on_message(restart)
        await bot.on_message(peer())
        bot.text_backend.complete.assert_awaited_once()
        await bot.close()

    asyncio.run(scenario())


def test_peer_turn_is_text_only_never_runs_commands_or_fetches_photos(monkeypatch, tmp_path):
    cfg, _, _, channel, _, peer, build = peer_fixture(monkeypatch, tmp_path)
    resolver = AsyncMock()
    monkeypatch.setattr("llm_chatbot.discord_media.resolve_source", resolver)

    async def scenario():
        bot = build()
        cfg.image_enabled = cfg.image_tools_enabled = cfg.text_vision_enabled = True
        bot.images = SimpleNamespace(cfg=SimpleNamespace(edits_enabled=True), close=AsyncMock())
        await bot.on_message(peer("please create an image"))
        resolver.assert_not_awaited()
        bot.process_commands.assert_not_awaited()
        bot.text_backend.complete.assert_awaited_once()
        assert channel.send.await_args.args[0] == "reply"
        mentions = channel.send.await_args.kwargs["allowed_mentions"]
        assert mentions.users and not mentions.everyone and not mentions.roles
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing_peers", "no_text", "zero_budget", "excess_budget", "negative_peer"])
def test_invalid_bot_chat_configuration_is_rejected(monkeypatch, tmp_path, failure):
    context_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("TEXT_BOT_CHAT_ENABLED", "true")
    monkeypatch.setenv("TEXT_BOT_CHAT_PEER_IDS", "777")
    if failure == "missing_peers":
        monkeypatch.setenv("TEXT_BOT_CHAT_PEER_IDS", "")
    elif failure == "no_text":
        monkeypatch.setenv("BOT_MODE", "image")
    elif failure == "negative_peer":
        monkeypatch.setenv("TEXT_BOT_CHAT_PEER_IDS", "-1")
    else:
        monkeypatch.setenv("TEXT_BOT_CHAT_MAX_REPLIES", "0" if failure == "zero_budget" else "11")
    with pytest.raises(ValueError, match="TEXT_BOT_CHAT"):
        load_config()
