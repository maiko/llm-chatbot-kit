"""Heuristics for passive listening and cooldown bookkeeping."""

from __future__ import annotations

import time
from typing import Tuple

from .personality import Personality


def _now() -> float:
    """Return current UNIX timestamp in seconds as float."""
    return time.time()


def _matches_allow_deny(persona: Personality, guild_settings: dict, channel_id: int, channel_name: str | None) -> bool:
    """Check channel allow/deny lists from persona and guild settings."""
    allowed = set((persona.listen.allow_channels or []) + (guild_settings.get("allowed_channels") or []))
    denied = set((persona.listen.deny_channels or []) + (guild_settings.get("denied_channels") or []))
    sid = str(channel_id)
    name = (channel_name or "").lower()
    if allowed:
        if sid not in allowed and name not in allowed:
            return False
    if sid in denied or name in denied:
        return False
    return True


def should_intervene(
    persona: Personality,
    guild_settings: dict,
    channel_id: int,
    channel_name: str | None,
    author_id: int,
    author_is_bot: bool,
    content: str,
) -> Tuple[bool, str]:
    """Return (intervene, intent) where intent is 'help' | 'joke' | 'snark'.

    Applies allow/deny lists, cooldowns (channel and per-user), minimal length,
    and simple keyword/laughter cues.
    """
    if not (persona.listen.enabled or guild_settings.get("listen_enabled")):
        return False, "help"
    if author_is_bot:
        return False, "help"
    if not _matches_allow_deny(persona, guild_settings, channel_id, channel_name):
        return False, "help"
    txt = content.strip()
    if len(txt) < persona.listen.min_len:
        return False, "help"

    # Cooldown gate per-channel
    last_ts = float(guild_settings.get("last_channel_ts", {}).get(str(channel_id), 0.0) or 0.0)
    if _now() - last_ts < persona.listen.cooldown_channel_seconds:
        return False, "help"

    # Cooldown gate per-user
    try:
        user_map = guild_settings.get("last_user_ts") or {}
        u_last = float(user_map.get(str(author_id), 0.0) or 0.0)
        if _now() - u_last < persona.listen.cooldown_user_seconds:
            return False, "help"
    except Exception:
        pass

    # Heuristics
    triggers = persona.listen.trigger_keywords or []
    hit = any(k.lower() in txt.lower() for k in triggers)
    hit = hit or ("?" in txt)
    # simple laughter cues
    for cue in ("lol", "mdr", "😂", "🤣", "lmao"):
        if cue in txt.lower():
            hit = True
            break
    if not hit:
        return False, "help"

    # Minimal intent: question -> help, otherwise joke
    intent = "help" if "?" in txt else "joke"
    return True, intent


def mark_intervened(guild_settings: dict, channel_id: int, author_id: int | None = None) -> None:
    """Record the last intervention time for the channel (and optionally user)."""
    m = guild_settings.setdefault("last_channel_ts", {})
    m[str(channel_id)] = _now()
    if author_id is not None:
        um = guild_settings.setdefault("last_user_ts", {})
        um[str(author_id)] = _now()

