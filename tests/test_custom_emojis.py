import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from llm_chatbot.commands import register_commands
from llm_chatbot.config import Config
from llm_chatbot.i18n import load_i18n
from llm_chatbot.memory import MemoryStore
from llm_chatbot.personality import DEFAULT_PERSONALITY
from llm_chatbot.runtime_utils import _build_env_context


def real_emojis(count=2):
    # Use the real library objects: neither Emoji nor PartialEmoji has .mention.
    return [
        discord.Emoji(
            guild=SimpleNamespace(id=1),
            state=None,
            data={"id": str(1000 + i), "name": f"fixture_{i}", "animated": bool(i % 2), "roles": []},
        )
        for i in range(count)
    ]


@pytest.mark.parametrize("language", ["fr", "en"])
def test_real_discord_static_and_animated_codes_reach_environment(language):
    persona = replace(DEFAULT_PERSONALITY, env_include_emojis=True, env_emojis_limit=200, language=language)
    message = SimpleNamespace(guild=SimpleNamespace(emojis=real_emojis(138), members=[]), channel=None)
    context = _build_env_context(message, persona, load_i18n(language))
    assert "<:fixture_0:1000>" in context
    assert "<a:fixture_1:1001>" in context
    assert "<a:fixture_137:1137>" in context
    assert "138/138" in context
    assert ("pas les images" if language == "fr" else "not image pixels") in context


def test_emoji_context_limit_and_opt_out():
    message = SimpleNamespace(guild=SimpleNamespace(emojis=real_emojis(), members=[]), channel=None)
    persona = replace(DEFAULT_PERSONALITY, env_include_emojis=True, env_emojis_limit=1)
    context = _build_env_context(message, persona, load_i18n("en"))
    assert "1/2" in context and "<:fixture_0:1000>" in context
    assert "fixture_1" not in context
    assert "fixture_0" not in _build_env_context(message, replace(persona, env_include_emojis=False), load_i18n("en"))


def test_prefix_emoji_list_uses_actual_library_codes(tmp_path):
    async def scenario():
        bot = commands.Bot(command_prefix="§", intents=discord.Intents.none())
        cfg = Config("fixture", "fixture", "model", None, "42", "§", 20, tmp_path / "context.json")
        register_commands(bot, MemoryStore(cfg.store_path), cfg, load_i18n("fr"), DEFAULT_PERSONALITY, "§")
        ctx = SimpleNamespace(guild=SimpleNamespace(emojis=real_emojis()), send=AsyncMock())
        await bot.get_command("emoji list").callback(ctx)
        output = ctx.send.await_args.args[0]
        assert "<:fixture_0:1000>" in output and "<a:fixture_1:1001>" in output
        await bot.close()

    asyncio.run(scenario())
