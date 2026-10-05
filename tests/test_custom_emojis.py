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


@pytest.mark.parametrize(
    "input_text,expected",
    [
        ("hi :fixture_0: :FIXTURE_1:", "hi <:fixture_0:1000> <a:fixture_1:1001>"),
        ("missing :blobfail:", "missing :blobfail:"),
        ("<:fixture_0:1000> <a:fixture_1:1001>", "<:fixture_0:1000> <a:fixture_1:1001>"),
        ("`:fixture_0:` ```\n:fixture_1:\n``` :fixture_0:", "`:fixture_0:` ```\n:fixture_1:\n``` <:fixture_0:1000>"),
        (r"\:fixture_0:", r"\:fixture_0:"),
    ],
)
def test_known_shortcodes_render_without_inventing_or_changing_code(input_text, expected):
    from llm_chatbot.runtime_utils import render_custom_emojis

    assert render_custom_emojis(input_text, SimpleNamespace(emojis=real_emojis())) == expected
    assert render_custom_emojis(input_text, None) == input_text


def test_ambiguous_emoji_names_are_not_guessed_and_tokens_stay_whole():
    from llm_chatbot.runtime_utils import _chunk_message, render_custom_emojis

    guild = SimpleNamespace(emojis=real_emojis() + real_emojis())
    assert render_custom_emojis(":fixture_0:", guild) == ":fixture_0:"
    assert render_custom_emojis("<a:fixture_0:9999>", guild) == "<a:fixture_0:9999>"
    token = "<a:fixture_1:1001>"
    text = "x" * 1985 + token + "tail"
    chunks = _chunk_message(text)
    assert "".join(chunks) == text and token in chunks[1]


def test_runtime_renders_emotes_in_nonstream_response(monkeypatch, tmp_path):
    from llm_chatbot.discord_bot import build_bot
    from test_chat_context import context_fixture

    cfg, _, persona, channel, message = context_fixture(monkeypatch, tmp_path)

    async def scenario():
        bot = build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda m: m.addressed)
        bot.text_backend.complete = AsyncMock(return_value=("hi <:fixture_1:101>", (1, 1, 0)))
        m = message("hello", True)
        m.guild.emojis = real_emojis()
        await bot.on_message(m)
        assert channel.send.await_args.args[0] == "hi <a:fixture_1:1001>"
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "input_text,expected",
    [
        ("hi <fixture_0:1000> <fixture_1:1001>", "hi <:fixture_0:1000> <a:fixture_1:1001>"),
        ("<a:fixture_0:1000> <:fixture_1:1001>", "<:fixture_0:1000> <a:fixture_1:1001>"),
        ("<wrong_name:1000> <a:WRONG_NAME:1001>", "<:fixture_0:1000> <a:fixture_1:1001>"),
        (r"\<fixture_0:1000> \<:fixture_1:1001>", "<:fixture_0:1000> <a:fixture_1:1001>"),
        ("<fixture_0:9999> <:fixture_1:9999> <missing:9999>", "<:fixture_0:1000> <a:fixture_1:1001> <missing:9999>"),
        ("<a:FIXTURE_0:100> <:fixture_1:100> <missing:1000>", "<:fixture_0:1000> <a:fixture_1:1001> <:fixture_0:1000>"),
        (
            "`<fixture_0:1000>` ```\n<fixture_1:1001>\n``` <fixture_0:1000>",
            "`<fixture_0:1000>` ```\n<fixture_1:1001>\n``` <:fixture_0:1000>",
        ),
        ("<fixture_0:1000>:fixture_1:", "<:fixture_0:1000><a:fixture_1:1001>"),
    ],
)
def test_id_tokens_use_actual_guild_metadata_without_guessing(input_text, expected):
    from llm_chatbot.runtime_utils import render_custom_emojis

    guild = SimpleNamespace(emojis=real_emojis())
    assert render_custom_emojis(input_text, guild) == expected
    assert render_custom_emojis(expected, guild) == expected
    assert render_custom_emojis(input_text, None) == input_text
