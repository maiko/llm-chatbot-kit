import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from llm_chatbot.image_client import ImageError, validate_request
from llm_chatbot.image_commands import ImageCommands
from llm_chatbot.image_jobs import JobStore
from test_images import TEST_PRESETS, settings, tool_message


def config(tmp_path, **changes):
    presets = dict(TEST_PRESETS, preview=dict(TEST_PRESETS["default"], quality="fast"))
    options = dict(presets=presets, mode_presets={"fast": "preview", "quality": "default"}, quality_rerun_enabled=True)
    options.update(changes)
    return settings(tmp_path, **options)


def source(store, state="sent"):
    payload = validate_request("A lighthouse at sunrise.", "preview", "768x1024", 9223372036854775807, store.cfg.presets)
    store.admit(100, 10, 1, 2, payload, "fr", 10000, preset="preview")
    store.update("100", state, message_id="900")
    return payload


def interaction():
    message = tool_message()
    return SimpleNamespace(
        id=101,
        guild_id=1,
        channel_id=2,
        channel=message.channel,
        guild=message.guild,
        user=message.author,
        filesize_limit=10000,
        app_permissions=message.channel.permissions_for(None),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def test_preferences_persist_and_are_scoped(tmp_path):
    cfg = config(tmp_path)
    store = JobStore(cfg)
    assert store.preferred_mode(10, 1) == "quality"
    store.set_mode(10, 1, "fast")
    assert store.preferred_mode(11, 1) == "quality"
    assert store.preferred_mode(10, 2) == "quality"
    with pytest.raises(ImageError):
        store.set_mode(10, 1, "anything")
    store.close()
    store = JobStore(cfg)
    assert store.preferred_mode(10, 1) == "fast"
    store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"mode_presets": {"fast": "missing", "quality": "default"}},
        {"mode_presets": {"fast": "preview"}},
        {"default_mode": "unknown"},
        {"mode_presets": {"fast": "fallback", "quality": "default"}},
    ],
)
def test_mode_config_fails_closed(tmp_path, change):
    with pytest.raises(ValueError):
        config(tmp_path, **change)


def test_quality_button_preserves_prompt_seed_size_and_admission_idempotency(tmp_path):
    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, config(tmp_path), "fr")
        original = source(feature.store)
        view = feature.quality_view(feature.store.get("100"))
        assert view.is_persistent()
        it = interaction()
        await view.children[0].callback(it)
        await view.children[0].callback(it)
        new = feature.store.get("101")
        payload = json.loads(new["payload"])
        assert {key: payload[key] for key in ("prompt", "seed", "size")} == {key: original[key] for key in ("prompt", "seed", "size")}
        assert payload["quality"] == "standard"
        assert "preset" not in payload
        assert feature.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
        assert json.loads(new["options"])["preset"] == "default"
        assert feature.quality_view(new) is None
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["other_user", "role", "view", "send", "unknown", "expired", "quota", "busy"])
def test_quality_rerun_rechecks_access_state_retention_and_limits(tmp_path, failure):
    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, config(tmp_path, daily_limit=1 if failure == "quota" else 10), "fr")
        source(feature.store, "unknown" if failure == "unknown" else "sent")
        it = interaction()
        if failure == "other_user":
            it.user.id = 11
        elif failure == "role":
            it.user.roles = []
        elif failure == "view":
            it.channel.permissions_for(None).view_channel = False
        elif failure == "send":
            it.app_permissions.send_messages = False
        elif failure == "expired":
            with feature.store.db:
                feature.store.db.execute("UPDATE jobs SET updated=?", (time.time() - 90000,))
        elif failure == "busy":
            feature.store.admit(
                102, 10, 1, 2, validate_request("test", "preview", "768x1024", 3, feature.cfg.presets), "fr", 10000, preset="preview"
            )
        await feature.rerun_quality(it, "100")
        assert feature.store.get("101") is None
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


def test_slash_modes_and_tool_default_use_user_preference(tmp_path):
    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, config(tmp_path), "fr")
        mode = bot.tree.get_command("image-mode", guild=discord.Object(id=1))
        it = interaction()
        await mode.callback(it, "fast")
        assert feature.preferred_preset(tool_message()) == "preview"
        await feature.from_message(tool_message(), {"prompt": "a lighthouse"})
        assert json.loads(feature.store.get("100")["payload"])["quality"] == "fast"
        feature.store.cancel("100", 10, 1)
        imagine = bot.tree.get_command("imagine", guild=discord.Object(id=1))
        await imagine.callback(it, "a lighthouse", mode="quality")
        assert json.loads(feature.store.get("101")["payload"])["quality"] == "standard"
        assert feature.store.preferred_mode(10, 1) == "fast"
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


def test_prompt_retention_is_opt_in_and_button_restores_after_restart(tmp_path):
    async def scenario():
        cfg = config(tmp_path)
        store = JobStore(cfg)
        source(store)
        store.close()
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.add_view = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        feature = ImageCommands(bot, cfg, "fr")
        feature.worker.start = lambda: None
        feature.status.start = lambda: None
        await feature.setup()
        assert json.loads(feature.store.get("100")["options"])["prompt"]
        bot.add_view.assert_called_once()
        assert bot.add_view.call_args.kwargs["message_id"] == 900
        await feature.close()
        await bot.close()
        store = JobStore(settings(tmp_path))
        assert "prompt" not in json.loads(store.get("100")["options"])
        store.close()

    asyncio.run(scenario())
