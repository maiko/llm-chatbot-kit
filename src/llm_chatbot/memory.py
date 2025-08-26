"""In-memory state with JSON persistence for channels, guilds, and billing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .config import read_json, write_json
from .costs import Billing

Message = dict  # {"role": "user"|"assistant"|"system", "content": str}


@dataclass
class ChannelContext:
    """Conversation state for a specific Discord channel."""

    turns: int = 0
    messages: List[Message] = field(default_factory=list)


class MemoryStore:
    """Holds channel contexts, guild settings, and billing, persisted to JSON."""

    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, ChannelContext] = {}
        self._guild_settings: Dict[str, dict] = {}
        self._billing: Billing = Billing()
        self._load()

    def _load(self) -> None:
        raw = read_json(self.path)
        chats = raw.get("chats", {}) if isinstance(raw, dict) else raw
        for k, v in chats.items():
            self._data[k] = ChannelContext(turns=v.get("turns", 0), messages=v.get("messages", []))
        self._guild_settings = raw.get("guild_settings", {}) if isinstance(raw, dict) else {}
        b = raw.get("billing", {}) if isinstance(raw, dict) else {}
        if b:
            self._billing = Billing(
                daily_usd=b.get("daily_usd", 0.0),
                daily_key=b.get("daily_key", ""),
                monthly_usd=b.get("monthly_usd", 0.0),
                monthly_key=b.get("monthly_key", ""),
                by_model=b.get("by_model", {}),
                by_feature=b.get("by_feature", {}),
                budget_daily_usd=b.get("budget_daily_usd"),
                budget_monthly_usd=b.get("budget_monthly_usd"),
                thresholds=tuple(b.get("thresholds", (0.5, 0.8, 1.0))),
                hard_stop=bool(b.get("hard_stop", True)),
                last_daily_alert=float(b.get("last_daily_alert", 0.0)),
                last_monthly_alert=float(b.get("last_monthly_alert", 0.0)),
            )

    def save(self) -> None:
        """Persist the current state to disk atomically."""
        raw = {
            "chats": {k: {"turns": v.turns, "messages": v.messages} for k, v in self._data.items()},
            "guild_settings": self._guild_settings,
            "billing": {
                "daily_usd": self._billing.daily_usd,
                "daily_key": self._billing.daily_key,
                "monthly_usd": self._billing.monthly_usd,
                "monthly_key": self._billing.monthly_key,
                "by_model": self._billing.by_model,
                "by_feature": self._billing.by_feature,
                "budget_daily_usd": self._billing.budget_daily_usd,
                "budget_monthly_usd": self._billing.budget_monthly_usd,
                "thresholds": list(self._billing.thresholds),
                "hard_stop": self._billing.hard_stop,
                "last_daily_alert": self._billing.last_daily_alert,
                "last_monthly_alert": self._billing.last_monthly_alert,
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, raw)

    def get(self, channel_id: int) -> ChannelContext:
        """Return the channel context, creating a new one if needed."""
        key = str(channel_id)
        if key not in self._data:
            self._data[key] = ChannelContext()
        return self._data[key]

    def reset(self, channel_id: int) -> None:
        """Clear the context for a specific channel and persist."""
        key = str(channel_id)
        self._data.pop(key, None)
        self.save()

    def reset_all(self) -> None:
        """Clear all channel contexts and persist."""
        self._data.clear()
        self.save()

    # Guild settings
    def guild_settings(self, guild_id: int) -> dict:
        """Return mutable guild settings, creating defaults on first access."""
        key = str(guild_id)
        gs = self._guild_settings.get(key)
        if gs is None:
            gs = {
                "listen_enabled": False,
                "denied_channels": [],
                "allowed_channels": [],
                "last_channel_ts": {},
                "last_user_ts": {},
            }
            self._guild_settings[key] = gs
        return gs

    # Billing accessors
    @property
    def billing(self) -> Billing:
        return self._billing

    def set_budgets(self, daily_usd: float | None, monthly_usd: float | None) -> None:
        """Update budgets in the billing state and persist."""
        if daily_usd is not None:
            self._billing.budget_daily_usd = daily_usd
        if monthly_usd is not None:
            self._billing.budget_monthly_usd = monthly_usd
        self.save()

