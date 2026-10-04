"""Validated configuration for an image-generation API."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _ids(name: str) -> frozenset[int]:
    return frozenset(int(value.strip()) for value in os.getenv(name, "").split(",") if value.strip())


@dataclass(frozen=True)
class ImageConfig:
    base_url: str
    api_key: str
    guild_ids: frozenset[int]
    channel_ids: frozenset[int]
    role_ids: frozenset[int]
    state_dir: Path
    queue_limit: int = 5
    daily_limit: int = 10
    timeout: float = 1850
    ca_file: str | None = None
    retention_hours: int = 24
    sync_commands: bool = False
    presets: dict = field(default_factory=lambda: {"default": {"model": "default"}})
    default_preset: str = "default"
    include_prompt: bool = False
    prompt_guidance: str = ""

    def __post_init__(self):
        if not isinstance(self.prompt_guidance, str) or len(self.prompt_guidance) > 16000:
            raise ValueError("Image prompt guidance must be text up to 16000 characters")
        if not isinstance(self.presets, dict) or not 1 <= len(self.presets) <= 25 or self.default_preset not in self.presets:
            raise ValueError("IMAGE_PRESETS_JSON requires 1-25 presets including IMAGE_DEFAULT_PRESET")
        for name, options in self.presets.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", name) or not isinstance(options, dict):
                raise ValueError("Invalid image preset")
            if set(options) - {"model", "quality", "size_multiple", "min_size", "max_size", "max_pixels", "supports_seed"}:
                raise ValueError("Unknown image preset option")
            if not isinstance(options.get("model"), str) or not 1 <= len(options["model"]) <= 128:
                raise ValueError("Each image preset requires a model ID")
            if "quality" in options and (not isinstance(options["quality"], str) or not 1 <= len(options["quality"]) <= 64):
                raise ValueError("Invalid image preset quality")
            for key, default in (("size_multiple", 1), ("min_size", 64), ("max_size", 2048), ("max_pixels", 4194304)):
                value = options.get(key, default)
                if type(value) is not int or value < 1:
                    raise ValueError("Image dimension limits must be positive integers")
            if options.get("min_size", 64) > options.get("max_size", 2048) or options.get("max_size", 2048) > 8192:
                raise ValueError("Invalid image dimension limits")
            if type(options.get("supports_seed", False)) is not bool:
                raise ValueError("supports_seed must be a boolean")

    @classmethod
    def from_env(cls) -> ImageConfig:
        cfg = cls(
            base_url=os.getenv("IMAGE_API_BASE_URL", "").rstrip("/"),
            api_key=os.getenv("IMAGE_API_KEY", ""),
            guild_ids=_ids("IMAGE_GUILD_IDS"),
            channel_ids=_ids("IMAGE_CHANNEL_IDS"),
            role_ids=_ids("IMAGE_ROLE_IDS"),
            state_dir=Path(os.getenv("IMAGE_STATE_DIR", str(Path.home() / ".local/state/llm-chatbot-kit/images"))),
            queue_limit=int(os.getenv("IMAGE_QUEUE_LIMIT", "5")),
            daily_limit=int(os.getenv("IMAGE_DAILY_LIMIT", "10")),
            timeout=float(os.getenv("IMAGE_TIMEOUT_SECONDS", "1850")),
            ca_file=os.getenv("IMAGE_CA_FILE") or None,
            retention_hours=int(os.getenv("IMAGE_RETENTION_HOURS", "24")),
            sync_commands=os.getenv("IMAGE_SYNC_COMMANDS", "false").lower() == "true",
            presets=json.loads(os.getenv("IMAGE_PRESETS_JSON", '{"default":{"model":"default"}}')),
            default_preset=os.getenv("IMAGE_DEFAULT_PRESET", "default"),
            include_prompt=os.getenv("IMAGE_INCLUDE_PROMPT", "false").lower() == "true",
            prompt_guidance=(
                Path(os.environ["IMAGE_PROMPT_GUIDANCE_FILE"]).read_text(encoding="utf-8")
                if os.getenv("IMAGE_PROMPT_GUIDANCE_FILE")
                else ""
            ),
        )
        parts = urlsplit(cfg.base_url)
        if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("IMAGE_API_BASE_URL must be an HTTP(S) base URL without credentials/query/fragment")
        if not cfg.api_key or not cfg.guild_ids or not cfg.channel_ids:
            raise ValueError("IMAGE_API_KEY, IMAGE_GUILD_IDS and IMAGE_CHANNEL_IDS are required")
        if min(cfg.queue_limit, cfg.daily_limit) < 1 or cfg.retention_hours < 24 or not 30 <= cfg.timeout <= 3600:
            raise ValueError("Invalid image queue, quota, retention or timeout")
        if any(value <= 0 for value in cfg.guild_ids | cfg.channel_ids | cfg.role_ids):
            raise ValueError("Discord IDs must be positive")
        return cfg

    def permits(self, guild_id: int | None, channel_id: int | None, roles: set[int]) -> bool:
        return guild_id in self.guild_ids and channel_id in self.channel_ids and (not self.role_ids or bool(roles & self.role_ids))
