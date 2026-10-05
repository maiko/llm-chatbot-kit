"""Bounded Discord attachment ingestion; no arbitrary user/model URL fetching."""

from __future__ import annotations

import asyncio
import base64
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError

from .image_client import ImageError

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_PIXELS = 16 * 1024 * 1024
MAX_NORMALIZED_BYTES = 4 * 1024 * 1024
FORMATS = {"PNG", "JPEG", "WEBP"}
_NORMALIZERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="source-image")
_NORMALIZATION_SLOTS = WeakKeyDictionary()


async def normalize_image_async(data: bytes, message_id: int = 0) -> SourceImage:
    loop = asyncio.get_running_loop()
    slots = _NORMALIZATION_SLOTS.setdefault(loop, asyncio.Semaphore(2))
    await slots.acquire()
    try:
        future = loop.run_in_executor(_NORMALIZERS, normalize_image, data, message_id)
    except BaseException:
        slots.release()
        raise
    # Cancellation must not free capacity while Pillow is still working.
    future.add_done_callback(lambda _: slots.release())
    return await asyncio.shield(future)


@dataclass(frozen=True)
class SourceImage:
    data: bytes
    width: int
    height: int
    message_id: int

    @property
    def size(self):
        return f"{self.width}x{self.height}"

    @property
    def data_url(self):
        return "data:image/png;base64," + base64.b64encode(self.data).decode("ascii")


def normalize_image(data: bytes, message_id: int = 0) -> SourceImage:
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ImageError("source_image_too_large")
    try:
        with Image.open(io.BytesIO(data), formats=list(FORMATS)) as image:
            if image.width * image.height > MAX_SOURCE_PIXELS or max(image.size) > 8192:
                raise ImageError("source_image_too_large")
            if getattr(image, "n_frames", 1) != 1:
                raise ImageError("source_image_animated")
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            width, height = [max(32, round(n / 32) * 32) for n in image.size]
            image = image.resize((width, height), Image.Resampling.LANCZOS)
            # Fresh pixels omit EXIF, filenames and other embedded metadata.
            clean = Image.frombytes("RGB", image.size, image.tobytes())
            out = io.BytesIO()
            clean.save(out, format="PNG")
            result = out.getvalue()
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageError("invalid_source_image") from exc
    if len(result) > MAX_NORMALIZED_BYTES:
        raise ImageError("source_image_too_large")
    return SourceImage(result, width, height, message_id)


def image_attachments(message):
    return [
        a
        for a in getattr(message, "attachments", [])
        if (getattr(a, "content_type", "") or "").startswith("image/")
        or PurePosixPath(getattr(a, "filename", "")).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    ]


async def read_attachment(attachment, message_id=0) -> SourceImage:
    if not 0 < attachment.size <= MAX_SOURCE_BYTES:
        raise ImageError("source_image_too_large")
    try:
        url = urlsplit(attachment.url)
        port = url.port
    except ValueError as exc:
        raise ImageError("invalid_source_image") from exc
    if (
        url.scheme != "https"
        or url.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
        or url.username
        or url.password
        or port not in {None, 443}
        or not url.path.startswith(("/attachments/", "/ephemeral-attachments/"))
    ):
        raise ImageError("invalid_source_image")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
            async with session.get(attachment.url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ImageError("source_image_unavailable")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_SOURCE_BYTES:
                        raise ImageError("source_image_too_large")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise ImageError("source_image_unavailable") from exc
    return await normalize_image_async(bytes(data), message_id)


async def resolve_source(message) -> SourceImage | None:
    source = message
    attachments = image_attachments(source)
    reference = getattr(message, "reference", None)
    if not attachments and reference and reference.message_id:
        source = getattr(reference, "resolved", None)
        resolved = source is not None and hasattr(source, "attachments")
        # A known text reference must never become a photo permission error.
        if resolved and not image_attachments(source):
            return None
        same_channel = reference.channel_id == message.channel.id and reference.guild_id == getattr(message.guild, "id", None)
        permissions = message.channel.permissions_for(message.author)
        bot_permissions = message.channel.permissions_for(message.guild.me)
        readable = all(
            (permissions.view_channel, permissions.read_message_history, bot_permissions.view_channel, bot_permissions.read_message_history)
        )
        if not resolved:
            # Do not fetch inaccessible/cross-channel history just to find a photo.
            if not same_channel or not readable:
                return None
            try:
                source = await message.channel.fetch_message(reference.message_id)
            except Exception:
                return None
        attachments = image_attachments(source)
        if not attachments:
            return None
        if (
            not same_channel
            or not readable
            or source.id != reference.message_id
            or source.channel.id != message.channel.id
            or getattr(source.guild, "id", None) != getattr(message.guild, "id", None)
        ):
            raise ImageError("source_image_access_denied")
    if len(attachments) > 1:
        raise ImageError("source_image_ambiguous")
    result = await read_attachment(attachments[0], source.id) if attachments else None
    if result is not None and source is not message:
        permissions = message.channel.permissions_for(message.author)
        bot_permissions = message.channel.permissions_for(message.guild.me)
        if not all(
            (permissions.view_channel, permissions.read_message_history, bot_permissions.view_channel, bot_permissions.read_message_history)
        ):
            raise ImageError("source_image_access_denied")
    return result


def with_image(conversation, source, message_id):
    """Only enrich this event; do not persist image bytes or URLs in chat history."""
    result = [dict(m) for m in conversation]
    target = next((m for m in reversed(result) if m.get("message_id") == str(message_id) and m.get("role") == "user"), None)
    if target is None:
        raise ImageError("source_image_unavailable")
    target["content"] = [
        {"type": "image_url", "image_url": {"url": source.data_url}},
        {"type": "text", "text": target["content"]},
    ]
    return result
