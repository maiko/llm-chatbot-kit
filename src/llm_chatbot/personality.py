"""Persona schema and YAML loader.

Defines the `Personality` and `ListenConfig` dataclasses and provides a loader
that reads YAML files into those structures. Personalities drive prompts,
environment context, streaming pacing, and listening behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class ListenConfig:
    """Configuration for passive listening and interventions in guilds."""

    enabled: bool = False
    # Channels by ID or name (strings accepted). If allow list non-empty, only these are considered.
    allow_channels: Optional[list] = None
    deny_channels: Optional[list] = None
    # Cooldowns in seconds
    cooldown_channel_seconds: int = 180
    cooldown_user_seconds: int = 30
    # Heuristics
    min_len: int = 12
    trigger_keywords: Optional[list] = None
    # Judge step
    judge_enabled: bool = True
    judge_model: str = "gpt-5-nano"
    judge_threshold: float = 0.6
    judge_max_context_messages: int = 5
    # Generation overrides for interventions
    generation_model_override: Optional[str] = None
    response_max_chars: int = 600
    joke_bias: float = 0.5
    # Cost budgets (global will still apply)
    cost_daily_usd: Optional[float] = None
    cost_monthly_usd: Optional[float] = None
    # Moderation (optional and off by default)
    moderation_enabled: bool = False
    moderation_model: str = "omni-moderation-latest"


@dataclass
class Personality:
    """Persona that guides behavior, prompts, environment, and streaming."""

    name: str
    system_prompt: str
    developer_prompt: str | None = None
    language: str | None = None
    # Responses API truncation strategy (e.g., "auto" or "disabled")
    truncation: str | None = "auto"
    # Streaming pacing (optional): messages per second and min chars per burst
    stream_rate_hz: float | None = None
    stream_min_first: int | None = None
    stream_min_next: int | None = None
    # Command prefix override (per-persona)
    command_prefix: str | None = None
    # Optional environment context templates
    env_guild_template: str | None = None
    env_dm_template: str | None = None
    # Optional: include custom server emojis in env context
    env_include_emojis: bool = False
    env_emojis_limit: int = 20
    # Optional message overrides (i18n keys)
    messages: Optional[Dict[str, str]] = None
    listen: ListenConfig = field(default_factory=ListenConfig)
    # Optional: include online members list
    env_include_online_members: bool = False
    env_online_limit: int = 50


DEFAULT_PERSONALITY = Personality(
    name="base",
    system_prompt=(
        "You are a helpful, direct assistant embedded in a Discord bot. "
        "Write concise replies, avoid sensitive data, and keep messages under 1900 characters."
    ),
    developer_prompt=("Stay within Discord’s content and rate limits. If unsure, ask for clarification."),
    language=None,
)


def load_personality(path: str | Path) -> Personality:
    """Load a persona YAML file into a `Personality` instance."""
    import yaml  # lazy import to avoid requiring PyYAML at import time

    p = Path(path)
    data = yaml.safe_load(p.read_text())
    streaming = data.get("streaming", {}) or {}
    environment = data.get("environment", {}) or {}
    # listen config
    _listen = data.get("listen", {}) or {}
    listen = ListenConfig(
        enabled=bool(_listen.get("enabled", False)),
        allow_channels=_listen.get("allow_channels"),
        deny_channels=_listen.get("deny_channels"),
        cooldown_channel_seconds=int(_listen.get("cooldown_channel_seconds", 180)),
        cooldown_user_seconds=int(_listen.get("cooldown_user_seconds", 30)),
        min_len=int(_listen.get("min_len", 12)),
        trigger_keywords=_listen.get("trigger_keywords"),
        judge_enabled=bool(_listen.get("judge_enabled", True)),
        judge_model=_listen.get("judge_model", "gpt-5-nano"),
        judge_threshold=float(_listen.get("judge_threshold", 0.6)),
        judge_max_context_messages=int(_listen.get("judge_max_context_messages", 5)),
        generation_model_override=_listen.get("generation_model_override"),
        response_max_chars=int(_listen.get("response_max_chars", 600)),
        joke_bias=float(_listen.get("joke_bias", 0.5)),
        cost_daily_usd=(_listen.get("cost_daily_usd")),
        cost_monthly_usd=(_listen.get("cost_monthly_usd")),
        moderation_enabled=bool(_listen.get("moderation_enabled", False)),
        moderation_model=_listen.get("moderation_model", "omni-moderation-latest"),
    )

    return Personality(
        name=data.get("name", p.stem),
        system_prompt=data["system_prompt"],
        developer_prompt=data.get("developer_prompt"),
        language=data.get("language"),
        truncation=data.get("truncation", "auto"),
        stream_rate_hz=streaming.get("rate_hz"),
        stream_min_first=streaming.get("min_first"),
        stream_min_next=streaming.get("min_next"),
        command_prefix=data.get("command_prefix"),
        env_guild_template=environment.get("guild_template"),
        env_dm_template=environment.get("dm_template"),
        env_include_emojis=bool(environment.get("include_emojis", False)),
        env_emojis_limit=int(environment.get("emojis_limit", 20)),
        messages=data.get("messages"),
        listen=listen,
        env_include_online_members=bool(environment.get("include_online_members", False)),
        env_online_limit=int(environment.get("online_limit", 50)),
    )
