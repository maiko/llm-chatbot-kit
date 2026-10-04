"""Async client for a configured image-generation API; never follows result URLs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import secrets
import ssl

import httpx

from .image_config import ImageConfig

logger = logging.getLogger(__name__)
PROGRESS_POLL_SECONDS = 5
PROGRESS_PHASES = {"preparing", "sampling", "decoding", "saving", "completed", "unavailable"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ImageError(Exception):
    """Safe error code; backend bodies and credentials are never exposed."""


class BackendBusy(ImageError):
    def __init__(self, retry_after: float = 30):
        super().__init__("backend_busy")
        self.retry_after = min(60, max(1, retry_after))


def validate_request(prompt: str, preset: str, size: str, seed: int | None, presets: dict | None = None) -> dict:
    if not prompt.strip() or len(prompt) > 4000:
        raise ImageError("invalid_prompt")
    presets = presets if presets is not None else {"default": {"model": "default"}}
    if preset not in presets:
        raise ImageError("invalid_preset")
    match = re.fullmatch(r"(\d{1,4})x(\d{1,4})", size)
    if not match:
        raise ImageError("invalid_size")
    width, height = map(int, match.groups())
    options = presets[preset]
    multiple = options.get("size_multiple", 1)
    if any(
        n < options.get("min_size", 64) or n > options.get("max_size", 2048) or n % multiple for n in (width, height)
    ) or width * height > options.get("max_pixels", 4194304):
        raise ImageError("invalid_size")
    if seed is not None and not 0 <= seed <= 2**63 - 1:
        raise ImageError("invalid_seed")
    payload = dict(model=options["model"], prompt=prompt.strip(), size=size, n=1, response_format="b64_json")
    if "quality" in options:
        payload["quality"] = options["quality"]
    if options.get("supports_seed", False):
        payload["seed"] = seed if seed is not None else secrets.randbelow(2**63)
    elif seed is not None:
        raise ImageError("seed_unsupported")
    return payload


class ImageClient:
    def __init__(self, cfg: ImageConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.progress_enabled = cfg.progress_enabled
        verify = ssl.create_default_context(cafile=cfg.ca_file) if cfg.ca_file else True
        self.http = httpx.AsyncClient(
            base_url=cfg.base_url + "/",
            headers={"Authorization": "Bearer " + cfg.api_key},
            verify=verify,
            timeout=httpx.Timeout(cfg.timeout, connect=10, write=30, pool=10),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2 if cfg.progress_enabled else 1),
        )

    async def generate(self, payload: dict, *, on_progress=None, source: bytes | None = None) -> bytes:
        request_id = secrets.token_hex(16) if self.progress_enabled and on_progress else None
        poll = asyncio.create_task(self._poll_progress(request_id, on_progress)) if request_id else None
        try:
            # A hard deadline also bounds a peer that keeps sending tiny chunks.
            return await asyncio.wait_for(self._generate(payload, request_id, source), timeout=self.http.timeout.read + 15)
        except (httpx.TimeoutException, httpx.TransportError, asyncio.TimeoutError) as exc:
            # POST may have reached the backend. Do not regenerate this job.
            raise ImageError("outcome_unknown") from exc
        finally:
            if poll:
                poll.cancel()
                try:
                    await poll
                except asyncio.CancelledError:
                    pass

    async def _poll_progress(self, request_id, callback):
        while True:
            await asyncio.sleep(PROGRESS_POLL_SECONDS)
            progress = {"phase": "unavailable", "value": None, "max": None}
            try:
                async with self.http.stream("GET", "images/progress/" + request_id, timeout=5) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 4096:
                            raise ValueError("progress_response_too_large")
                    data = json.loads(body)
                    if not isinstance(data, dict) or data.get("phase") not in PROGRESS_PHASES:
                        raise ValueError("invalid_progress_phase")
                    progress["phase"] = data["phase"]
                    if data["phase"] == "sampling" and (data.get("value") is not None or data.get("max") is not None):
                        value, maximum = data.get("value"), data.get("max")
                        if type(value) is not int or type(maximum) is not int or not 0 <= value <= maximum <= 100000 or maximum == 0:
                            raise ValueError("invalid_progress_steps")
                        progress.update(value=value, max=maximum)
            except (httpx.HTTPError, ValueError, TypeError):
                progress = {"phase": "unavailable", "value": None, "max": None}
            try:
                callback(progress)
            except Exception as exc:
                logger.warning("image_progress callback_failed=%s", type(exc).__name__)

    async def _generate(self, payload: dict, request_id=None, source=None) -> bytes:
        endpoint = "images/edits" if source is not None else "images/generations"
        body = (
            {"data": {key: str(value) for key, value in payload.items()}, "files": {"image": ("source.png", source, "image/png")}}
            if source is not None
            else {"json": payload}
        )
        async with self.http.stream(
            "POST", endpoint, **body, headers={"X-Image-Request-ID": request_id} if request_id else None
        ) as response:
            if response.status_code == 429:
                try:
                    delay = float(response.headers.get("Retry-After", "30"))
                except ValueError:
                    delay = 30
                raise BackendBusy(delay)
            if response.status_code >= 500:
                raise ImageError("outcome_unknown")
            if response.status_code != 200:
                raise ImageError("backend_rejected")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES * 4 // 3 + 4096:
                    raise ImageError("image_too_large")
            try:
                data = json.loads(body)["data"][0]["b64_json"]
                result = base64.b64decode(data, validate=True)
            except (ValueError, KeyError, IndexError, TypeError, binascii.Error) as exc:
                raise ImageError("invalid_response") from exc
            if not result.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ImageError("invalid_response")
            if len(result) > MAX_IMAGE_BYTES:
                raise ImageError("image_too_large")
            return result

    async def close(self) -> None:
        await self.http.aclose()
