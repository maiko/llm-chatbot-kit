import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from discord.ext import commands
from PIL import Image

from llm_chatbot.discord_media import normalize_image, read_attachment, resolve_source, with_image
from llm_chatbot.image_client import ImageClient, ImageError, validate_request
from llm_chatbot.image_commands import ImageCommands
from llm_chatbot.image_jobs import ImageWorker, JobStore
from llm_chatbot.image_tools import complete_with_image_tool, image_tool_receipt
from test_image_modes import interaction
from test_images import image_env, settings, tool_message


def photo():
    buffer = io.BytesIO()
    image = Image.new("RGB", (640, 512), "red")
    image.save(buffer, format="PNG")
    return normalize_image(buffer.getvalue(), 99)


def config(tmp_path, **kwargs):
    presets = {
        "default": {"model": "fixture-quality", "supports_seed": True, "supports_edits": True, "size_multiple": 32},
        "preview": {"model": "fixture-fast", "supports_seed": True, "supports_edits": True, "size_multiple": 32},
        "other": {"model": "unsupported"},
    }
    opts = dict(
        presets=presets,
        edits_enabled=True,
        mode_presets={"fast": "preview", "quality": "default"},
        default_mode="fast",
        quality_rerun_enabled=True,
    )
    opts.update(kwargs)
    return settings(tmp_path, **opts)


def test_photo_normalization_strips_metadata_and_bounds_size():
    image = Image.new("RGB", (1600, 1200), "blue")
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "PRIVATE_EXIF_MARKER"
    b = io.BytesIO()
    image.save(b, format="JPEG", exif=exif)
    source = normalize_image(b.getvalue())
    assert max(source.width, source.height) <= 1024 and source.height > source.width
    assert source.width % 32 == source.height % 32 == 0
    decoded = Image.open(io.BytesIO(source.data))
    assert not decoded.getexif() and "exif" not in decoded.info
    assert b"PRIVATE_EXIF_MARKER" not in source.data


@pytest.mark.parametrize("kind", ["empty", "corrupt", "large", "animated"])
def test_invalid_photo_is_rejected(kind):
    data = {"empty": b"", "corrupt": b"not an image", "large": b"x" * (8 * 1024 * 1024 + 1)}.get(kind)
    if kind == "animated":
        b = io.BytesIO()
        Image.new("RGB", (32, 32), "red").save(
            b, format="WEBP", save_all=True, append_images=[Image.new("RGB", (32, 32), "blue")], duration=100
        )
        data = b.getvalue()
    with pytest.raises(ImageError):
        normalize_image(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/attachments/x",
        "https://localhost/attachments/x",
        "https://cdn.discordapp.com.evil/attachments/x",
        "https://cdn.discordapp.com@localhost/attachments/x",
        "https://cdn.discordapp.com/other/x",
        "https://cdn.discordapp.com:invalid/attachments/x",
    ],
)
def test_attachment_only_fetches_discord_attachment_hosts(url):
    with pytest.raises(ImageError, match="invalid_source_image"):
        asyncio.run(read_attachment(SimpleNamespace(size=100, url=url)))


def test_reply_source_is_same_channel_readable_and_unambiguous(monkeypatch):
    import llm_chatbot.discord_media as media

    async def scenario():
        message = tool_message()
        message.attachments = []
        permissions = message.channel.permissions_for(message.author)
        permissions.read_message_history = True
        source_message = SimpleNamespace(
            id=99,
            channel=message.channel,
            guild=message.guild,
            attachments=[SimpleNamespace(content_type="image/png", filename="source.png")],
        )
        message.reference = SimpleNamespace(
            message_id=99, channel_id=message.channel.id, guild_id=message.guild.id, resolved=source_message
        )
        monkeypatch.setattr(media, "read_attachment", AsyncMock(return_value=photo()))
        assert (await resolve_source(message)).message_id == 99
        media.read_attachment.assert_awaited_once()
        message.reference.channel_id = 999
        with pytest.raises(ImageError, match="source_image_access_denied"):
            await resolve_source(message)
        message.reference.channel_id = message.channel.id
        permissions.read_message_history = False
        with pytest.raises(ImageError, match="source_image_access_denied"):
            await resolve_source(message)
        permissions.read_message_history = True
        source_message.attachments *= 2
        with pytest.raises(ImageError, match="source_image_ambiguous"):
            await resolve_source(message)

    asyncio.run(scenario())


def test_multimodal_event_never_mutates_persisted_history():
    history = [
        {"role": "user", "content": "earlier", "message_id": "98"},
        {"role": "user", "content": "describe this", "message_id": "100"},
    ]
    enriched = with_image(history, photo(), 100)
    assert isinstance(history[-1]["content"], str)
    assert enriched[0]["content"] == "earlier"
    assert enriched[1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert enriched[1]["content"][1]["text"] == "describe this"


def test_edit_tool_admits_one_source_and_quality_reuses_original(tmp_path):
    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, config(tmp_path), "fr")
        source = photo()
        message = tool_message()
        response = {
            "tool_calls": [
                {
                    "type": "function",
                    "function": {"name": "edit_image", "arguments": json.dumps({"prompt": "Change only the background to blue."})},
                }
            ]
        }
        client = SimpleNamespace(complete_message=AsyncMock(return_value=(response, (1, 2, 0))))
        receipt, _ = await complete_with_image_tool(client, feature, [], message, source)
        assert receipt == ""
        job = feature.store.get("100")
        options = json.loads(job["options"])
        assert options["operation"] == "edit" and options["size"] == source.size
        assert feature.store.source_artifact("100").read_bytes() == source.data
        assert "source" not in json.loads(job["payload"])
        assert feature.store.source_artifact("100").stat().st_mode & 0o777 == 0o600
        feature.store.update("100", "sent", message_id="900")
        it = interaction()
        await feature.rerun_quality(it, "100")
        rerun = feature.store.get("101")
        rerun_options = json.loads(rerun["options"])
        assert rerun_options["operation"] == "edit"
        assert feature.store.source_artifact("101").read_bytes() == source.data
        assert rerun_options["seed"] == options["seed"] and rerun_options["prompt"] == options["prompt"]
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing", "unsupported", "extra", "multiple"])
def test_edit_tool_invalid_calls_never_admit(tmp_path, failure):
    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, config(tmp_path), "fr")
        args = {"prompt": "change color"}
        source = photo()
        if failure == "missing":
            source = None
        if failure == "unsupported":
            args["preset"] = "other"
        if failure == "extra":
            args["image_url"] = "https://localhost/private"
        response = {"tool_calls": [{"type": "function", "function": {"name": "edit_image", "arguments": json.dumps(args)}}]}
        if failure == "multiple":
            response["tool_calls"] *= 2
        assert await image_tool_receipt(feature, response, tool_message(), source)
        assert feature.store.db.execute("select count(*) from jobs").fetchone()[0] == 0
        assert not list(tmp_path.glob("*.source.png"))
        await feature.close()
        await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["cancelled", "failed", "unknown", "sent"])
def test_source_lifetime_follows_job_and_quality_retention(tmp_path, state):
    cfg = config(tmp_path)
    store = JobStore(cfg)
    source = photo()
    payload = validate_request("change color", "preview", source.size, 1, cfg.presets)
    store.admit(100, 10, 1, 2, payload, "fr", 10000, preset="preview", source=source.data)
    store.update("100", state)
    assert store.source_artifact("100").exists() == (state == "sent")
    store.close()
    store = JobStore(cfg)
    assert store.source_artifact("100").exists() == (state == "sent")
    if state == "sent":
        store.db.execute("update jobs set updated=0")
        store.db.commit()
        store.cleanup()
        assert not store.source_artifact("100").exists()
    store.close()


def test_worker_calls_multipart_edit_client_and_progress(tmp_path):
    async def scenario():
        cfg = config(tmp_path, progress_enabled=True)
        store = JobStore(cfg)
        source = photo()
        payload = validate_request("change color", "preview", source.size, 1, cfg.presets)
        job = store.admit(100, 10, 1, 2, payload, "fr", 10000, preset="preview", source=source.data)

        async def handler(request):
            assert request.url.path == "/v1/images/edits"
            assert request.headers["content-type"].startswith("multipart/form-data")
            body = await request.aread()
            assert source.data in body and b'filename="source.png"' in body
            assert request.headers["X-Image-Request-ID"]
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(source.data).decode()}]})

        client = ImageClient(cfg, transport=httpx.MockTransport(handler))
        deliver = AsyncMock(return_value="900")
        worker = ImageWorker(store, client, deliver)
        await worker.process(job)
        assert store.get("100")["state"] == "sent"
        deliver.assert_awaited_once()
        await client.close()
        store.close()

    asyncio.run(scenario())


def test_runtime_vision_forwards_current_photo_without_persisting_pixels(monkeypatch, tmp_path):
    import llm_chatbot.discord_media as media
    from llm_chatbot.config import load_config
    from llm_chatbot.discord_bot import build_bot
    from llm_chatbot.personality import load_personality

    image_env(monkeypatch, tmp_path)
    for key, value in {
        "BOT_MODE": "both",
        "TEXT_API_BASE_URL": "https://fixture.invalid/v1",
        "TEXT_API_KEY": "fixture",
        "TEXT_MODEL": "fixture-text",
        "TEXT_GUILD_IDS": "1",
        "TEXT_CHANNEL_IDS": "2",
        "TEXT_VISION_ENABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)

    async def scenario():
        bot = build_bot(load_config(), load_personality("examples/local-bot.yml"), stream=False)
        bot.process_commands = AsyncMock()
        bot._connection.user = SimpleNamespace(id=555, mentioned_in=lambda _: True)
        bot.text_backend.complete = AsyncMock(return_value=("A red rectangle.", (1, 2, 0)))
        monkeypatch.setattr(media, "resolve_source", AsyncMock(return_value=photo()))
        message = tool_message()
        message.content = "What color is this?"
        message.author.display_name = "member"
        message.guild.name = "fixture"
        message.guild.members = []
        message.guild.emojis = []
        await bot.on_message(message)
        convo = bot.text_backend.complete.await_args.args[0]
        content = next(m["content"] for m in convo if m.get("message_id") == "100")
        assert isinstance(content, list) and content[0]["type"] == "image_url"
        assert "data:image" not in load_config().store_path.read_text()
        await bot.close()

    asyncio.run(scenario())


def test_edit_configuration_requires_capable_preset(tmp_path):
    with pytest.raises(ValueError, match="edit-capable preset"):
        config(
            tmp_path,
            presets={"default": {"model": "text-only", "supports_seed": True}},
            mode_presets={"fast": "default", "quality": "default"},
        )


@pytest.mark.parametrize("status,oversized", [(302, False), (200, True), (200, False)])
def test_cdn_download_has_no_redirect_and_bounds_stream(monkeypatch, status, oversized):
    import llm_chatbot.discord_media as media

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Response(Context):
        async def iter_chunked(self, size):
            assert size == 65536
            if oversized:
                for _ in range(129):
                    yield b"x" * 65536
            else:
                yield photo().data

    response = Response()
    response.status = status
    response.content = response

    class Session(Context):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["timeout"].total == 20

        def get(self, url, **kwargs):
            assert kwargs == {"allow_redirects": False}
            return response

    monkeypatch.setattr(media.aiohttp, "ClientSession", Session)
    attachment = SimpleNamespace(size=1000, url="https://cdn.discordapp.com/attachments/1/2/source.png?signature=private")
    if status == 302 or oversized:
        with pytest.raises(ImageError, match="source_image_unavailable" if status == 302 else "source_image_too_large"):
            asyncio.run(read_attachment(attachment))
    else:
        assert asyncio.run(read_attachment(attachment)).data == photo().data


def test_source_file_removed_when_admission_transaction_fails(tmp_path):
    import sqlite3

    cfg = config(tmp_path)
    store = JobStore(cfg)
    store.db.execute("CREATE TRIGGER fail_admit BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    source = photo()
    with pytest.raises(sqlite3.IntegrityError):
        store.admit(100, 10, 1, 2, validate_request("change hair", "preview", source.size, 1, cfg.presets), "fr", 10000, source=source.data)
    assert not list(tmp_path.glob("*.source*")) and store.get("100") is None
    store.close()


def test_slash_edit_attachment_admission_and_feature_opt_in(monkeypatch, tmp_path):
    import llm_chatbot.discord_media as media

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, config(tmp_path), "fr")
        command = bot.tree.get_command("image-edit", guild=discord.Object(1))
        assert command is not None
        monkeypatch.setattr(media, "read_attachment", AsyncMock(return_value=photo()))
        it = interaction()
        await command.callback(it, SimpleNamespace(), "Change the hairstyle", "fast")
        job = feature.store.get(str(it.id))
        assert json.loads(job["options"])["operation"] == "edit"
        assert feature.store.source_artifact(str(it.id)).read_bytes() == photo().data
        await feature.close()
        await bot.close()
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        feature = ImageCommands(bot, config(tmp_path / "disabled", edits_enabled=False), "fr")
        assert bot.tree.get_command("image-edit", guild=discord.Object(1)) is None
        await feature.close()
        await bot.close()

    asyncio.run(scenario())
