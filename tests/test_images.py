import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from discord.ext import commands

from llm_chatbot.commands import register_commands
from llm_chatbot.config import Config, load_config, read_json
from llm_chatbot.discord_bot import build_bot
from llm_chatbot.i18n import load_i18n
from llm_chatbot.image_client import BackendBusy, ImageClient, ImageError
from llm_chatbot.image_client import validate_request as make_request
from llm_chatbot.image_config import ImageConfig
from llm_chatbot.image_jobs import ImageWorker, JobStore
from llm_chatbot.image_status import ImageStatus, render_status
from llm_chatbot.memory import MemoryStore
from llm_chatbot.personality import DEFAULT_PERSONALITY
from llm_chatbot.rate_limit import MultiKeySlidingWindow
from llm_chatbot.text_client import ChatCompletionsClient

PNG = b"\x89PNG\r\n\x1a\nfixture"
TEST_PRESETS = {
    "default": {
        "model": "image-model",
        "quality": "standard",
        "size_multiple": 32,
        "min_size": 256,
        "max_size": 1536,
        "max_pixels": 1572864,
        "supports_seed": True,
    },
    "fallback": {"model": "alternate-model", "size_multiple": 64, "min_size": 256, "max_size": 1536, "max_pixels": 1572864},
}


def validate_request(prompt, preset, size, seed):
    return make_request(prompt, preset, size, seed, TEST_PRESETS)


def settings(tmp_path, **changes):
    return replace(
        ImageConfig(
            "https://local.invalid/v1", "private-key", frozenset({1}), frozenset({2}), frozenset({3}), tmp_path, presets=TEST_PRESETS
        ),
        **changes,
    )


def admit(store, job=100, user=10):
    return store.admit(job, user, 1, 2, validate_request("lighthouse", "default", "1024x1024", 7), "fr", 10000)


@pytest.mark.parametrize(
    "guild,channel,roles,expected", [(None, 2, {3}, False), (1, 2, {3}, True), (1, 4, {3}, False), (4, 2, {3}, False), (1, 2, {4}, False)]
)
def test_access(tmp_path, guild, channel, roles, expected):
    assert settings(tmp_path).permits(guild, channel, roles) is expected


@pytest.mark.parametrize(
    "prompt,preset,size,seed",
    [
        ("", "default", "1024x1024", 1),
        ("x", "other", "1024x1024", 1),
        ("x", "default", "1536x1536", 1),
        ("x", "fallback", "800x800", 1),
        ("x", "default", "1024x1024", -1),
    ],
)
def test_validation(prompt, preset, size, seed):
    with pytest.raises(ImageError):
        validate_request(prompt, preset, size, seed)


def test_admission_dedupe_ownership_capacity_and_quota(tmp_path):
    store = JobStore(settings(tmp_path, queue_limit=2, daily_limit=1))
    assert admit(store)["id"] == admit(store)["id"]
    with pytest.raises(ImageError, match="user_busy"):
        admit(store, 101)
    admit(store, 102, 11)
    with pytest.raises(ImageError, match="queue_full"):
        admit(store, 103, 12)
    assert not store.cancel("100", 11, 1)
    assert not store.cancel("100", 10, 9)
    assert store.cancel("100", 10, 1)
    with pytest.raises(ImageError, match="daily_limit"):
        admit(store, 104)
    store.close()


def test_store_lock_and_restart_preserve_pending_not_running(tmp_path):
    cfg = settings(tmp_path)
    store = JobStore(cfg)
    with pytest.raises(RuntimeError, match="already owns"):
        JobStore(cfg)
    admit(store)
    admit(store, 101, 11)
    store.update("100", "running")
    store.close()
    store = JobStore(cfg)
    assert store.get("100")["state"] == "unknown"
    assert store.get("101")["state"] == "queued"
    assert store.uncertain()
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "jobs.sqlite3").stat().st_mode & 0o777 == 0o600
    store.close()


def test_worker_unknown_no_retry_and_upload_failure_retains_artifact(tmp_path):
    async def scenario():
        store = JobStore(settings(tmp_path))
        client = SimpleNamespace(generate=AsyncMock(side_effect=ImageError("outcome_unknown")), close=AsyncMock())
        delivery = AsyncMock(return_value="999")
        worker = ImageWorker(store, client, delivery)
        await worker.process(admit(store))
        assert client.generate.await_count == 1
        assert store.get("100")["state"] == "unknown"
        delivery.assert_not_awaited()
        store.update("100", "failed", "owner_resolved")
        client.generate = AsyncMock(return_value=PNG)
        delivery.side_effect = ImageError("attachment_too_large")
        await worker.process(admit(store, 101))
        assert store.get("101")["state"] == "delivery_failed"
        assert store.artifact("101").read_bytes() == PNG
        assert store.get("101")["payload"] is None
        await worker.close()

    asyncio.run(scenario())


def test_restart_ready_delivers_without_regeneration(tmp_path):
    async def scenario():
        cfg = settings(tmp_path)
        store = JobStore(cfg)
        admit(store)
        store.artifact("100").write_bytes(PNG)
        store.update("100", "ready")
        store.close()
        store = JobStore(cfg)
        client = SimpleNamespace(generate=AsyncMock(), close=AsyncMock())
        worker = ImageWorker(store, client, AsyncMock(return_value="999"))
        await worker.process(store.next_job())
        client.generate.assert_not_awaited()
        assert store.get("100")["message_id"] == "999"
        await worker.close()

    asyncio.run(scenario())


def test_worker_pause_and_shared_text_lock(tmp_path):
    async def scenario():
        store = JobStore(settings(tmp_path))
        client = SimpleNamespace(generate=AsyncMock(return_value=PNG), close=AsyncMock())
        worker = ImageWorker(store, client, AsyncMock(return_value="999"))
        worker.execution_lock = asyncio.Lock()
        await worker.execution_lock.acquire()
        admit(store)
        worker.start()
        first = worker.task
        worker.start()
        assert worker.task is first
        await asyncio.sleep(0.02)
        client.generate.assert_not_awaited()
        assert store.cancel("100", 10, 1)
        worker.execution_lock.release()
        await asyncio.sleep(0.02)
        client.generate.assert_not_awaited()
        admit(store, 101, 11)
        store.update("101", "unknown", "outcome_unknown")
        admit(store, 102, 12)
        worker.wake.set()
        await asyncio.sleep(0.02)
        client.generate.assert_not_awaited()
        await worker.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,error",
    [
        ("success", None),
        ("busy", BackendBusy),
        ("timeout", ImageError),
        ("redirect", ImageError),
        ("external_url", ImageError),
        ("server", ImageError),
    ],
)
def test_async_image_contract(tmp_path, mode, error):
    async def handler(request):
        assert str(request.url) == "https://local.invalid/v1/images/generations"
        assert request.headers["Authorization"] == "Bearer private-key"
        assert json.loads(request.content)["response_format"] == "b64_json"
        if mode == "busy":
            return httpx.Response(429, headers={"Retry-After": "1"})
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://external.invalid/steal"})
        if mode == "server":
            return httpx.Response(504)
        data = {"url": "https://external.invalid/image"} if mode == "external_url" else {"b64_json": base64.b64encode(PNG).decode()}
        return httpx.Response(200, json={"data": [data]})

    async def scenario():
        client = ImageClient(settings(tmp_path), httpx.MockTransport(handler))
        if error:
            with pytest.raises(error):
                await client.generate(validate_request("x", "default", "1024x1024", None))
        else:
            assert await client.generate(validate_request("x", "default", "1024x1024", None)) == PNG
        await client.close()

    asyncio.run(scenario())


def image_env(monkeypatch, tmp_path):
    for name, value in {
        "BOT_MODE": "image",
        "IMAGE_API_BASE_URL": "https://local.invalid/v1",
        "IMAGE_API_KEY": "private",
        "IMAGE_GUILD_IDS": "1",
        "IMAGE_CHANNEL_IDS": "2",
        "IMAGE_ROLE_IDS": "3",
        "IMAGE_STATE_DIR": str(tmp_path / "images"),
        "IMAGE_PRESETS_JSON": json.dumps(TEST_PRESETS),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TEXT_API_BASE_URL", raising=False)
    monkeypatch.delenv("TEXT_API_KEY", raising=False)


def test_image_only_boot_without_openai_privileged_intents_or_command_sync(monkeypatch, tmp_path):
    image_env(monkeypatch, tmp_path)

    async def scenario():
        cfg = load_config()
        assert not cfg.openai_api_key
        bot = build_bot(cfg, DEFAULT_PERSONALITY)
        await bot.__aenter__()
        assert not bot.intents.message_content and not bot.intents.members and not bot.intents.presences
        assert bot.text_backend is None
        assert bot.get_command("reboot") is None
        assert {command.name for command in bot.tree.get_commands(guild=discord.Object(id=1))} == {
            "imagine",
            "image-status",
            "image-cancel",
            "image-result",
            "image-resolve",
        }
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        first = bot.images.worker.task
        await bot.on_ready()
        await bot.on_ready()
        assert bot.images.worker.task is first
        assert bot.save_task is None
        bot.tree.sync.assert_not_awaited()
        await bot.close()

    asyncio.run(scenario())


def test_slash_denial_deferral_and_seed(monkeypatch, tmp_path):
    image_env(monkeypatch, tmp_path)

    async def scenario():
        bot = build_bot(load_config(), DEFAULT_PERSONALITY)
        interaction = SimpleNamespace(
            id=100,
            guild_id=1,
            channel_id=2,
            channel=SimpleNamespace(id=2, send=AsyncMock(return_value=SimpleNamespace(id=900))),
            guild=SimpleNamespace(filesize_limit=10000),
            user=SimpleNamespace(id=10, bot=False, roles=[SimpleNamespace(id=3)]),
            filesize_limit=10000,
            app_permissions=SimpleNamespace(attach_files=True, view_channel=True, send_messages=True, send_messages_in_threads=False),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        command = bot.tree.get_command("imagine", guild=discord.Object(id=1))
        await command.callback(interaction, "lighthouse", seed="9223372036854775807")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        job = bot.images.store.get("100")
        assert json.loads(job["payload"])["seed"] == 2**63 - 1
        assert job["status_message_id"] == "900"
        assert "Position 1/1" in interaction.channel.send.await_args.args[0]
        interaction.guild_id = None
        await command.callback(interaction, "lighthouse")
        interaction.response.send_message.assert_awaited_once()
        assert len(bot.images.store.db.execute("SELECT * FROM jobs").fetchall()) == 1
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("owner,user,allowed", [(None, 10, False), ("11", 10, False), ("10", 10, True)])
def test_owner_commands_fail_closed(tmp_path, owner, user, allowed):
    async def scenario():
        bot = commands.Bot(command_prefix="~", intents=discord.Intents.none())
        cfg = Config("", "", "", None, owner, "~", 20, tmp_path / "context.json")
        store = MemoryStore(cfg.store_path)
        store.get(1).messages = [{"role": "user", "content": "keep"}]
        register_commands(bot, store, cfg, load_i18n("en"), DEFAULT_PERSONALITY, "~")
        ctx = SimpleNamespace(author=SimpleNamespace(id=user), send=AsyncMock(), guild=SimpleNamespace(id=1))
        await bot.get_command("reboot").callback(ctx)
        assert bool(store.get(1).messages) is not allowed
        await bot.get_command("listen on").callback(ctx)
        assert store.guild_settings(1)["listen_enabled"] is allowed
        await bot.close()

    asyncio.run(scenario())


def test_multirate_window_regression():
    clock = [0]
    limiter = MultiKeySlidingWindow({"user": [(10, 10), (60, 2)]}, now_func=lambda: clock[0])
    assert limiter.allow("user", "1")
    clock[0] = 1
    assert limiter.allow("user", "1")
    clock[0] = 20
    assert not limiter.allow("user", "1")
    clock[0] = 62
    assert limiter.allow("user", "1")


def test_corrupt_context_is_explicit(tmp_path):
    path = tmp_path / "context.json"
    path.write_text("{broken")
    with pytest.raises(RuntimeError):
        read_json(path)


def test_text_backend_contract_and_stream_without_cloud(tmp_path):
    calls = []

    async def handler(request):
        assert str(request.url) == "https://local.invalid/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private"
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "text-model"
        assert body["messages"][0] == {"role": "system", "content": "persona"}
        assert set(body["messages"][1]) == {"role", "content"}
        if body["stream"]:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"bonjour"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "bonjour"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
        )

    async def scenario():
        client = ChatCompletionsClient("https://local.invalid/v1", "private", "text-model", transport=httpx.MockTransport(handler))
        messages = [{"role": "user", "content": "hello", "addressed": True}, {"role": "system", "content": "persona"}]
        assert await client.complete(messages) == ("bonjour", (5, 2, 0))
        assert [chunk async for chunk in client.deltas(messages)] == ["bonjour"]
        await client.close()

    asyncio.run(scenario())
    assert len(calls) == 2


def test_delivery_rechecks_attachment_permissions_and_roles(tmp_path):
    async def scenario():
        bot = commands.Bot(command_prefix="~", intents=discord.Intents.none())
        feature = __import__("llm_chatbot.image_commands", fromlist=["ImageCommands"]).ImageCommands(bot, settings(tmp_path), "fr")
        job = admit(feature.store)
        feature.store.artifact(job["id"]).write_bytes(PNG)
        bot.wait_until_ready = AsyncMock()
        member = SimpleNamespace(roles=[SimpleNamespace(id=3)])
        permissions = SimpleNamespace(view_channel=True, send_messages=True, send_messages_in_threads=False, attach_files=True)
        guild = SimpleNamespace(id=1, me=object(), filesize_limit=1, get_member=lambda _: member)
        channel = SimpleNamespace(
            id=2, guild=guild, permissions_for=lambda _: permissions, send=AsyncMock(return_value=SimpleNamespace(id=999))
        )
        bot.get_channel = lambda _: channel
        with pytest.raises(ImageError, match="attachment_too_large"):
            await feature.deliver(job, feature.store.artifact(job["id"]))
        channel.send.assert_not_awaited()
        guild.filesize_limit = 10000
        member.roles = []
        with pytest.raises(ImageError, match="requester_access_revoked"):
            await feature.deliver(job, feature.store.artifact(job["id"]))
        member.roles = [SimpleNamespace(id=3)]
        permissions.attach_files = False
        with pytest.raises(ImageError, match="missing_channel_permissions"):
            await feature.deliver(job, feature.store.artifact(job["id"]))
        permissions.attach_files = True
        assert await feature.deliver(job, feature.store.artifact(job["id"])) == "999"
        assert channel.send.await_args.kwargs["allowed_mentions"].users is False
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


def test_shutdown_marks_running_generation_unknown(tmp_path):
    async def scenario():
        started = asyncio.Event()

        async def generate(_):
            started.set()
            await asyncio.Event().wait()

        store = JobStore(settings(tmp_path))
        client = SimpleNamespace(generate=generate, close=AsyncMock())
        worker = ImageWorker(store, client, AsyncMock())
        admit(store)
        worker.start()
        await asyncio.wait_for(started.wait(), 1)
        await worker.close()
        store = JobStore(settings(tmp_path))
        assert store.get("100")["state"] == "unknown"
        assert not store.cancel("100", 10, 1)
        store.close()

    asyncio.run(scenario())


def test_uncertain_upload_never_automatically_resent(tmp_path):
    async def scenario():
        store = JobStore(settings(tmp_path))
        client = SimpleNamespace(generate=AsyncMock(return_value=PNG), close=AsyncMock())
        upload = AsyncMock(side_effect=RuntimeError("connection lost"))
        worker = ImageWorker(store, client, upload)
        await worker.process(admit(store))
        assert store.get("100")["state"] == "delivery_unknown"
        assert store.next_job() is None
        assert store.artifact("100").read_bytes() == PNG
        assert upload.await_count == 1
        await worker.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("serialize", [False, True])
def test_combined_mode_lifecycle_uses_only_local_client(monkeypatch, tmp_path, serialize):
    image_env(monkeypatch, tmp_path)
    for name, value in {
        "BOT_MODE": "both",
        "TEXT_API_BASE_URL": "https://local.invalid/v1",
        "TEXT_API_KEY": "private",
        "TEXT_MODEL": "text-model",
        "TEXT_GUILD_IDS": "1",
        "TEXT_CHANNEL_IDS": "2",
        "SERIALIZE_BACKENDS": str(serialize).lower(),
    }.items():
        monkeypatch.setenv(name, value)

    async def scenario():
        bot = build_bot(load_config(), DEFAULT_PERSONALITY)
        assert bot.text_backend.model == "text-model"
        assert (bot.images.worker.execution_lock is bot.text_backend.lock) is serialize
        await bot.__aenter__()
        await bot.setup_hook()
        task = bot.save_task
        await bot.on_ready()
        assert bot.save_task is task
        await bot.close()
        assert task.cancelled()

    asyncio.run(scenario())


def test_local_stream_failure_has_no_second_request():
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')

    async def scenario():
        client = ChatCompletionsClient("https://local.invalid/v1", "private", "local", transport=httpx.MockTransport(handler))
        parts = []
        with pytest.raises(RuntimeError, match="incomplete"):
            async for part in client.deltas([{"role": "user", "content": "hello"}]):
                parts.append(part)
        assert parts == ["partial"]
        assert len(calls) == 1
        await client.close()

    asyncio.run(scenario())


def test_local_message_runtime_does_not_call_cloud(monkeypatch, tmp_path):
    image_env(monkeypatch, tmp_path)
    for name, value in {
        "BOT_MODE": "both",
        "TEXT_API_BASE_URL": "https://local.invalid/v1",
        "TEXT_API_KEY": "private",
        "TEXT_MODEL": "text-model",
        "TEXT_GUILD_IDS": "1",
        "TEXT_CHANNEL_IDS": "2",
    }.items():
        monkeypatch.setenv(name, value)
    import llm_chatbot.discord_bot as runtime
    from llm_chatbot.personality import load_personality

    cloud = AsyncMock(side_effect=AssertionError("cloud backend must not be called"))
    monkeypatch.setattr(runtime, "chat_complete_with_usage", cloud)
    monkeypatch.setattr(runtime, "judge_intervention", cloud)
    cloud_price = __import__("unittest.mock", fromlist=["Mock"]).Mock(side_effect=AssertionError("no cloud pricing for local tokens"))
    monkeypatch.setattr(runtime, "usd_cost", cloud_price)

    async def scenario():
        bot = build_bot(load_config(), load_personality("examples/local-bot.yml"), stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda _: True)
        bot.text_backend.complete = AsyncMock(return_value=("bonjour", (5, 2, 0)))
        job = admit(bot.images.store)
        bot.images.store.update(job["id"], "running")
        await bot.images.worker.execution_lock.acquire()
        guild = SimpleNamespace(id=1, name="fixture", members=[], emojis=[])
        channel = SimpleNamespace(id=2, name="fixture", send=AsyncMock())
        message = SimpleNamespace(
            guild=guild,
            channel=channel,
            content="hello",
            webhook_id=None,
            author=SimpleNamespace(id=10, display_name="member", bot=False, roles=[]),
        )
        await bot.on_message(message)
        bot.text_backend.complete.assert_awaited_once()
        assert channel.send.await_args.args[0] == "bonjour"
        bot.images.worker.execution_lock.release()
        message.guild = None
        await bot.on_message(message)
        assert bot.text_backend.complete.await_count == 1
        cloud.assert_not_called()
        cloud_price.assert_not_called()
        await bot.close()

    asyncio.run(scenario())


def test_new_backend_keys_are_redacted(monkeypatch):
    import logging

    from llm_chatbot.logging_setup import RedactionFilter

    monkeypatch.setenv("IMAGE_API_KEY", "image-private-credential")
    monkeypatch.setenv("TEXT_API_KEY", "text-private-credential")
    record = logging.LogRecord(
        "fixture", logging.INFO, __file__, 1, "keys %s %s", ("image-private-credential", "text-private-credential"), None
    )
    assert RedactionFilter().filter(record)
    assert "image-private-credential" not in record.getMessage()
    assert "text-private-credential" not in record.getMessage()


def test_status_position_cancellation_restart_and_pause(tmp_path):
    async def scenario():
        store = JobStore(settings(tmp_path))
        message = SimpleNamespace(id=900, edit=AsyncMock(return_value=SimpleNamespace(id=900)))
        channel = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock(return_value=message), get_partial_message=lambda _: message)
        bot = SimpleNamespace(get_channel=lambda _: channel)
        status = ImageStatus(bot, store)
        first = admit(store)
        second = admit(store, 101, 11)
        await status.publish(second, channel)
        await status.publish(second, channel)
        channel.send.assert_awaited_once()
        assert "Position 2/2" in channel.send.await_args.args[0]
        assert "2 en attente" in channel.send.await_args.args[0]
        store.update(first["id"], "running")
        await status.refresh()
        assert "1 en attente" in message.edit.await_args.kwargs["content"]
        store.update(first["id"], "sent", message_id="901")
        await status.refresh()
        assert "Position 1/1" in message.edit.await_args.kwargs["content"]
        await status.close()
        store.close()
        store = JobStore(settings(tmp_path))
        status = ImageStatus(bot, store)
        assert store.get("101")["status_message_id"] == "900"
        store.update("101", "running")
        await status.refresh()
        assert "Génération en cours" in message.edit.await_args.kwargs["content"]
        store.update("101", "unknown", "outcome_unknown")
        third = admit(store, 102, 12)
        assert "File suspendue" in render_status(third, store.queue_snapshot("102"))
        await status.publish(third, channel)
        assert store.cancel("102", 12, 1)
        await status.refresh()
        assert "Annulée" in message.edit.await_args.kwargs["content"]
        count = message.edit.await_count
        await status.refresh()
        assert message.edit.await_count == count
        await status.close()
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("prompt", ["lighthouse", "é" * 2500])
def test_delivery_edits_status_and_refresh_never_overwrites_image(tmp_path, prompt):
    async def scenario():
        bot = commands.Bot(command_prefix="~", intents=discord.Intents.none())
        feature = __import__("llm_chatbot.image_commands", fromlist=["ImageCommands"]).ImageCommands(
            bot, settings(tmp_path, include_prompt=True), "fr"
        )
        job = feature.store.admit(100, 10, 1, 2, validate_request(prompt, "default", "1024x1024", 7), "fr", 10000)
        member = SimpleNamespace(roles=[SimpleNamespace(id=3)])
        permissions = SimpleNamespace(view_channel=True, send_messages=True, attach_files=True)
        message = SimpleNamespace(id=900, edit=AsyncMock(return_value=SimpleNamespace(id=900)))
        uploaded = []

        async def edit(**kwargs):
            uploaded.extend((file.filename, file.fp.read()) for file in kwargs.get("attachments", []))
            return SimpleNamespace(id=900)

        message.edit.side_effect = edit
        guild = SimpleNamespace(id=1, me=object(), filesize_limit=10000, get_member=lambda _: member)
        channel = SimpleNamespace(
            guild=guild,
            id=2,
            permissions_for=lambda _: permissions,
            get_partial_message=lambda _: message,
            send=AsyncMock(return_value=message),
        )
        bot.wait_until_ready = AsyncMock()
        bot.get_channel = lambda _: channel
        await feature.status.publish(job, channel)
        feature.worker.client.generate = AsyncMock(return_value=PNG)
        await feature.worker.process(job)
        assert feature.store.get("100")["state"] == "sent"
        channel.send.assert_awaited_once()
        assert message.edit.await_args.kwargs["attachments"][0].filename == "image-100.png"
        assert "seed 7" in message.edit.await_args.kwargs["content"]
        if len(prompt) < 2000:
            assert prompt in message.edit.await_args.kwargs["content"]
            assert len(uploaded) == 1
        else:
            assert uploaded[1] == ("prompt-100.txt", prompt.encode("utf-8"))
        assert "prompt" not in json.loads(feature.store.get("100")["options"])
        count = message.edit.await_count
        await feature.status.refresh()
        assert message.edit.await_count == count
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


def test_ambiguous_status_publish_is_not_repeated(tmp_path):
    async def scenario():
        store = JobStore(settings(tmp_path))
        status = ImageStatus(SimpleNamespace(), store)
        channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("connection lost")))
        job = admit(store)
        await status.publish(job, channel)
        await status.publish(job, channel)
        channel.send.assert_awaited_once()
        assert store.get("100")["status_message_id"] == "unavailable"
        await status.close()
        store.close()

    asyncio.run(scenario())


def test_image_model_and_protocol_options_are_configuration(tmp_path):
    payload = make_request("scene", "default", "1024x1024", None)
    assert payload == {"model": "default", "prompt": "scene", "size": "1024x1024", "n": 1, "response_format": "b64_json"}
    with pytest.raises(ImageError, match="seed_unsupported"):
        make_request("scene", "default", "1024x1024", 7)
    presets = {"draft": {"model": "another-provider-model", "quality": "draft", "size_multiple": 16, "max_size": 4096}}
    cfg = settings(tmp_path, presets=presets, default_preset="draft")
    payload = make_request("scene", cfg.default_preset, "2048x1024", None, cfg.presets)
    assert payload["model"] == "another-provider-model" and payload["quality"] == "draft"
    assert "seed" not in payload
    with pytest.raises(ImageError, match="invalid_size"):
        make_request("scene", "draft", "1025x1024", None, cfg.presets)


@pytest.mark.parametrize(
    "presets",
    [
        {},
        {"bad name": {"model": "fixture"}},
        {"default": {"model": "fixture", "size_multiple": 0}},
        {"default": {"model": "fixture", "unknown": True}},
    ],
)
def test_invalid_image_preset_configuration_fails_closed(tmp_path, presets):
    with pytest.raises(ValueError):
        settings(tmp_path, presets=presets)


def test_prompt_delivery_is_opt_in_and_erased_after_cancel_or_uncertain_restart(tmp_path):
    store = JobStore(settings(tmp_path))
    assert "prompt" not in json.loads(admit(store)["options"])
    store.close()
    cfg = settings(tmp_path, include_prompt=True)
    store = JobStore(cfg)
    job = admit(store, 101, 11)
    assert json.loads(job["options"])["prompt"] == "lighthouse"
    assert store.cancel("101", 11, 1)
    assert "prompt" not in json.loads(store.get("101")["options"])
    admit(store, 102, 12)
    store.update("102", "running")
    store.close()
    store = JobStore(cfg)
    assert store.get("102")["state"] == "unknown"
    assert "prompt" not in json.loads(store.get("102")["options"])
    store.close()


def tool_message():
    permissions = SimpleNamespace(view_channel=True, send_messages=True, send_messages_in_threads=False, attach_files=True)
    guild = SimpleNamespace(id=1, me=object(), filesize_limit=10000)
    channel = SimpleNamespace(
        id=2, guild=guild, permissions_for=lambda _: permissions, send=AsyncMock(return_value=SimpleNamespace(id=800))
    )
    return SimpleNamespace(
        id=100, guild=guild, channel=channel, author=SimpleNamespace(id=10, roles=[SimpleNamespace(id=3)], bot=False), webhook_id=None
    )


def tool_response(arguments=None):
    return {
        "content": None,
        "tool_calls": [
            {
                "type": "function",
                "function": {
                    "name": "generate_image",
                    "arguments": json.dumps(arguments or {"prompt": "A lighthouse at sunrise, wide composition, soft light."}),
                },
            }
        ],
    }


def test_image_tool_uses_requester_and_durable_queue_without_followup(tmp_path):
    from llm_chatbot.image_commands import ImageCommands
    from llm_chatbot.image_tools import complete_with_image_tool

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, settings(tmp_path, include_prompt=True, prompt_guidance="Use complete visual sentences."), "fr")
        client = SimpleNamespace(complete_message=AsyncMock(return_value=(tool_response(), (4, 6, 0))))
        message = tool_message()
        receipt, usage = await complete_with_image_tool(client, feature, [{"role": "user", "content": "draw a lighthouse"}], message)
        assert "En attente" in receipt and "Position 1/1" in receipt
        job = feature.store.get("100")
        assert (job["user_id"], job["guild_id"], job["channel_id"], job["status_message_id"]) == ("10", "1", "2", "800")
        assert json.loads(job["payload"])["prompt"].startswith("A lighthouse")
        assert json.loads(job["options"])["prompt"] == json.loads(job["payload"])["prompt"]
        assert usage == (4, 6, 0)
        conversation, tools = client.complete_message.await_args.args
        assert "Use complete visual sentences." in conversation[-1]["content"]
        assert tools[0]["function"]["parameters"]["properties"]["preset"]["enum"] == list(TEST_PRESETS)
        await complete_with_image_tool(client, feature, [], message)
        assert feature.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        message.channel.send.assert_awaited_once()
        message.id = 101
        receipt, _ = await complete_with_image_tool(client, feature, [], message)
        assert "refusée" in receipt
        assert feature.store.get("101") is None
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["multiple", "name", "json", "duplicate", "destination", "size", "role", "permissions", "dm", "webhook", "bot", "type"]
)
def test_image_tool_rejects_invalid_calls_and_access_before_admission(tmp_path, failure):
    from llm_chatbot.image_commands import ImageCommands
    from llm_chatbot.image_tools import complete_with_image_tool

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, settings(tmp_path), "fr")
        message, response = tool_message(), tool_response()
        function = response["tool_calls"][0]["function"]
        if failure == "multiple":
            response["tool_calls"] *= 2
        elif failure == "name":
            function["name"] = "exec"
        elif failure == "json":
            function["arguments"] = "{broken"
        elif failure == "duplicate":
            function["arguments"] = '{"prompt":"first","prompt":"second"}'
        elif failure == "destination":
            function["arguments"] = json.dumps({"prompt": "a tree", "channel_id": "999"})
        elif failure == "size":
            function["arguments"] = json.dumps({"prompt": "a tree", "size": "8192x8192"})
        elif failure == "type":
            function["arguments"] = json.dumps({"prompt": ["a tree"]})
        elif failure == "role":
            message.author.roles = []
        elif failure == "permissions":
            message.channel.permissions_for(None).attach_files = False
        elif failure == "dm":
            message.guild = None
        elif failure == "webhook":
            message.webhook_id = 10
        elif failure == "bot":
            message.author.bot = True
        client = SimpleNamespace(complete_message=AsyncMock(return_value=(response, (0, 0, 0))))
        receipt, _ = await complete_with_image_tool(client, feature, [], message)
        assert "refusée" in receipt
        assert feature.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        message.channel.send.assert_not_called()
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


def test_chat_completions_tool_contract_and_normal_text():
    async def scenario():
        from llm_chatbot.image_tools import image_tool

        cfg = SimpleNamespace(presets=TEST_PRESETS)

        async def handler(request):
            payload = json.loads(request.content)
            assert payload["tool_choice"] == "auto" and payload["parallel_tool_calls"] is False
            assert payload["stream"] is False
            return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})

        client = ChatCompletionsClient("https://backend.invalid/v1", "fixture", "text-model", transport=httpx.MockTransport(handler))
        message, _ = await client.complete_message([], [image_tool(cfg)])
        assert message["content"] == "hello"
        await client.close()

    asyncio.run(scenario())


def test_tool_mode_requires_both_mode_and_configured_text_backend(monkeypatch, tmp_path):
    image_env(monkeypatch, tmp_path)
    monkeypatch.setenv("IMAGE_TOOLS_ENABLED", "true")
    with pytest.raises(ValueError, match="requires both"):
        build_bot(load_config(), DEFAULT_PERSONALITY)


def test_image_guidance_file_is_explicit_bounded_configuration(monkeypatch, tmp_path):
    image_env(monkeypatch, tmp_path)
    path = tmp_path / "guidance.txt"
    path.write_text("Describe lighting and composition.", encoding="utf-8")
    monkeypatch.setenv("IMAGE_PROMPT_GUIDANCE_FILE", str(path))
    assert ImageConfig.from_env().prompt_guidance == path.read_text()
    path.write_text("x" * 16001)
    with pytest.raises(ValueError, match="guidance"):
        ImageConfig.from_env()


def test_runtime_non_stream_conversational_image_tool_submits_without_cloud(monkeypatch, tmp_path):
    image_env(monkeypatch, tmp_path)
    for name, value in {
        "BOT_MODE": "both",
        "TEXT_API_BASE_URL": "https://backend.invalid/v1",
        "TEXT_API_KEY": "fixture",
        "TEXT_MODEL": "text-model",
        "TEXT_GUILD_IDS": "1",
        "TEXT_CHANNEL_IDS": "2",
        "IMAGE_TOOLS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    from llm_chatbot.personality import load_personality

    async def scenario():
        bot = build_bot(load_config(), load_personality("examples/local-bot.yml"), stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda _: True)
        bot.text_backend.complete_message = AsyncMock(return_value=(tool_response(), (5, 2, 0)))
        bot.text_backend.deltas = AsyncMock(side_effect=AssertionError("tools must not stream"))
        message = tool_message()
        message.content = "draw a lighthouse"
        message.author.display_name = "member"
        message.guild.name, message.guild.members, message.guild.emojis = "fixture", [], []
        await bot.on_message(message)
        assert bot.images.store.get("100")["state"] == "queued"
        bot.text_backend.complete_message.assert_awaited_once()
        bot.text_backend.deltas.assert_not_called()
        assert "Position 1/1" in message.channel.send.await_args.args[0]
        await bot.close()

    asyncio.run(scenario())


def test_optional_readiness_marker_tracks_connection_lifecycle(monkeypatch, tmp_path):
    image_env(monkeypatch, tmp_path)
    marker = tmp_path / "ready"
    marker.write_text("stale")
    monkeypatch.setenv("BOT_READY_FILE", str(marker))

    async def scenario():
        bot = build_bot(load_config(), DEFAULT_PERSONALITY)
        assert not marker.exists()
        await bot.on_ready()
        assert marker.exists() and marker.stat().st_mode & 0o777 == 0o600
        await bot.on_disconnect()
        assert not marker.exists()
        await bot.on_resumed()
        assert marker.exists()
        await bot.close()
        assert not marker.exists()

    asyncio.run(scenario())


def test_owner_listen_commands_override_persona_and_status_reports_effective_state(tmp_path):
    from llm_chatbot.listener import listening_enabled

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cfg = Config("", "", "text-model", None, "42", "!", 20, tmp_path / "context.json")
        store = MemoryStore(cfg.store_path)
        persona = replace(DEFAULT_PERSONALITY)
        persona.listen = replace(DEFAULT_PERSONALITY.listen, enabled=True)
        register_commands(bot, store, cfg, load_i18n("en"), persona, "!")
        ctx = SimpleNamespace(author=SimpleNamespace(id=42), guild=SimpleNamespace(id=1), send=AsyncMock())
        assert listening_enabled(persona, store.guild_settings(1))
        await bot.get_command("listen off").callback(ctx)
        assert not listening_enabled(persona, store.guild_settings(1))
        await bot.get_command("listen status").callback(ctx)
        assert "False" in ctx.send.await_args.args[0]
        reloaded = MemoryStore(cfg.store_path)
        assert reloaded.guild_settings(1)["listen_override"] is False
        await bot.get_command("listen on").callback(ctx)
        assert listening_enabled(persona, store.guild_settings(1))
        await bot.close()

    asyncio.run(scenario())


def test_configured_listening_judge_respects_persona_context_limit(monkeypatch, tmp_path):
    image_env(monkeypatch, tmp_path)
    for name, value in {
        "BOT_MODE": "both",
        "TEXT_API_BASE_URL": "https://backend.invalid/v1",
        "TEXT_API_KEY": "fixture",
        "TEXT_MODEL": "text-model",
        "TEXT_GUILD_IDS": "1",
        "TEXT_CHANNEL_IDS": "2",
    }.items():
        monkeypatch.setenv(name, value)
    import llm_chatbot.discord_bot as runtime

    cfg = load_config()
    store = MemoryStore(cfg.store_path)
    store.get(2).messages = [{"role": "user", "content": value} for value in ("old", "recent1", "recent2")]
    monkeypatch.setattr(runtime, "MemoryStore", lambda _: store)
    persona = replace(DEFAULT_PERSONALITY)
    persona.listen = replace(DEFAULT_PERSONALITY.listen, enabled=True, judge_max_context_messages=2)

    async def scenario():
        bot = build_bot(cfg, persona, stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda _: False)
        bot.text_backend.judge = AsyncMock(return_value=(False, "help", 0.2))
        message = tool_message()
        message.content, message.channel.name = "what do you think?", "fixture"
        await bot.on_message(message)
        msgs, _ = bot.text_backend.judge.await_args.args
        assert [m["content"] for m in msgs] == ["recent1", "recent2", "what do you think?"]
        await bot.close()

    asyncio.run(scenario())


class FixtureSSE(httpx.AsyncByteStream):
    def __init__(self, frames):
        self.frames = frames

    async def __aiter__(self):
        for frame in self.frames:
            yield frame.encode()


def sse_event(delta=None, finish=None, usage=None):
    choices = [] if usage is not None else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    return "data: " + json.dumps({"choices": choices, "usage": usage}) + "\n\n"


def tool_frames():
    arguments = json.dumps({"prompt": "A lighthouse at sunrise.", "size": "1024x1024"})
    return [
        sse_event({"content": "Je prépare l'image. "}),
        sse_event({"tool_calls": [{"index": 0, "type": "function", "function": {"name": "generate_", "arguments": ""}}]}),
        sse_event({"tool_calls": [{"index": 0, "function": {"name": "image", "arguments": arguments[:14]}}]}),
        sse_event({"tool_calls": [{"index": 0, "function": {"arguments": arguments[14:]}}]}),
        sse_event(finish="tool_calls"),
        sse_event(usage={"prompt_tokens": 10, "completion_tokens": 20}),
        "data: [DONE]\n\n",
    ]


@pytest.mark.parametrize("proxy_usage_choice", [False, True])
def test_streamed_image_tool_buffers_fragments_until_complete_and_tracks_usage(tmp_path, proxy_usage_choice):
    from llm_chatbot.image_commands import ImageCommands
    from llm_chatbot.image_tools import ImageToolStream

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, settings(tmp_path), "fr")
        calls = []

        def handler(request):
            payload = json.loads(request.content)
            assert payload["stream"] is True and payload["parallel_tool_calls"] is False
            assert payload["tools"][0]["function"]["name"] == "generate_image"
            calls.append(payload)
            frames = tool_frames()
            if proxy_usage_choice:
                frames[-2] = (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                        }
                    )
                    + "\n\n"
                )
            return httpx.Response(200, stream=FixtureSSE(frames))

        client = ChatCompletionsClient("https://backend.invalid/v1", "fixture", "text-model", transport=httpx.MockTransport(handler))
        stream = ImageToolStream(client, feature, [], tool_message())
        assert await stream.__anext__() == "Je prépare l'image. "
        assert feature.store.get("100") is None
        assert client.lock.locked()
        tail = [chunk async for chunk in stream]
        assert len(tail) == 1 and "Position 1/1" in tail[0]
        assert "generate_image" not in "".join(tail)
        assert json.loads(feature.store.get("100")["payload"])["prompt"] == "A lighthouse at sunrise."
        assert stream.usage == (10, 20, 0)
        assert not client.lock.locked() and len(calls) == 1
        # Replayed message uses the existing job and status message.
        stream = ImageToolStream(client, feature, [], tool_message())
        [chunk async for chunk in stream]
        assert feature.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        await client.close()
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["disconnect", "missing_finish", "length", "multiple", "index", "oversized", "json", "after_finish"])
def test_incomplete_or_invalid_tool_stream_never_admits_an_image(tmp_path, failure):
    from llm_chatbot.image_commands import ImageCommands
    from llm_chatbot.image_tools import ImageToolStream

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, settings(tmp_path), "fr")
        frames = tool_frames()
        if failure == "disconnect":
            frames = frames[:-1]
        elif failure == "missing_finish":
            frames.pop(4)
        elif failure == "length":
            frames[4] = sse_event(finish="length")
        elif failure == "multiple":
            frames[2] = sse_event({"tool_calls": [{"index": 0}, {"index": 1}]})
        elif failure == "index":
            frames[2] = sse_event({"tool_calls": [{"index": 1}]})
        elif failure == "oversized":
            frames[2] = sse_event({"tool_calls": [{"index": 0, "function": {"arguments": "x" * 16001}}]})
        elif failure == "json":
            frames[2] = "data: {broken\n\n"
        elif failure == "after_finish":
            frames.insert(5, sse_event({"content": "unexpected late data"}))
        count = []

        def handler(request):
            count.append(request)
            return httpx.Response(200, stream=FixtureSSE(frames))

        client = ChatCompletionsClient("https://backend.invalid/v1", "fixture", "text-model", transport=httpx.MockTransport(handler))
        stream = ImageToolStream(client, feature, [], tool_message())
        with pytest.raises((RuntimeError, ValueError)):
            [chunk async for chunk in stream]
        await stream.aclose()
        assert feature.store.get("100") is None and not client.lock.locked() and len(count) == 1
        await client.close()
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


def test_closing_tool_stream_after_first_text_releases_lock_without_admission(tmp_path):
    from llm_chatbot.image_commands import ImageCommands
    from llm_chatbot.image_tools import ImageToolStream

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, settings(tmp_path), "fr")
        client = ChatCompletionsClient(
            "https://backend.invalid/v1",
            "fixture",
            "text-model",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=FixtureSSE(tool_frames()))),
        )
        stream = ImageToolStream(client, feature, [], tool_message())
        await stream.__anext__()
        assert client.lock.locked()
        await stream.aclose()
        assert not client.lock.locked() and feature.store.get("100") is None
        await client.close()
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


def test_tool_stream_delivers_normal_text_progressively_without_admitting_image(tmp_path):
    from llm_chatbot.image_commands import ImageCommands
    from llm_chatbot.image_tools import ImageToolStream

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, settings(tmp_path), "fr")
        frames = [sse_event({"content": "bonjour "}), sse_event({"content": "à tous"}), sse_event(finish="stop"), "data: [DONE]\n\n"]
        client = ChatCompletionsClient(
            "https://backend.invalid/v1",
            "fixture",
            "text-model",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=FixtureSSE(frames))),
        )
        stream = ImageToolStream(client, feature, [], tool_message())
        assert await stream.__anext__() == "bonjour "
        assert [chunk async for chunk in stream] == ["à tous"]
        assert feature.store.get("100") is None
        await client.close()
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_runtime_streaming_image_tool_has_no_non_stream_fallback(monkeypatch, tmp_path, failure):
    image_env(monkeypatch, tmp_path)
    for name, value in {
        "BOT_MODE": "both",
        "TEXT_API_BASE_URL": "https://backend.invalid/v1",
        "TEXT_API_KEY": "fixture",
        "TEXT_MODEL": "text-model",
        "TEXT_GUILD_IDS": "1",
        "TEXT_CHANNEL_IDS": "2",
        "IMAGE_TOOLS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    import llm_chatbot.discord_bot as runtime
    from llm_chatbot.personality import load_personality

    async def scenario():
        bot = build_bot(load_config(), load_personality("examples/local-bot.yml"), stream=True)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda _: True)
        bot.text_backend.complete_message = AsyncMock(side_effect=AssertionError("no non-stream retry"))
        frames = tool_frames()[:-1] if failure else tool_frames()
        await bot.text_backend.http.aclose()
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, stream=FixtureSSE(frames))

        bot.text_backend.http = httpx.AsyncClient(base_url="https://backend.invalid/v1/", transport=httpx.MockTransport(handler))

        async def consume(channel, iterator, **kwargs):
            return "".join([chunk async for chunk in iterator])

        monkeypatch.setattr(runtime, "send_stream_as_messages", consume)
        message = tool_message()
        message.content = "draw a lighthouse"
        message.author.display_name = "member"
        message.guild.name, message.guild.members, message.guild.emojis = "fixture", [], []
        await bot.on_message(message)
        assert bool(bot.images.store.get("100")) is not failure
        assert len(calls) == 1 and not bot.text_backend.lock.locked()
        bot.text_backend.complete_message.assert_not_called()
        await bot.close()

    asyncio.run(scenario())
