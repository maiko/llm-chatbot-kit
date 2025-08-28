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
        self._billing_by_bot: Dict[str, Billing] = {}
        self._rate_windows_by_bot: Dict[str, Dict[str, Dict[str, list]]] = {}
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
        bb = raw.get("billing_by_bot", {}) if isinstance(raw, dict) else {}
        if isinstance(bb, dict):
            for bot_id, vb in bb.items():
                self._billing_by_bot[bot_id] = Billing(
                    daily_usd=vb.get("daily_usd", 0.0),
                    daily_key=vb.get("daily_key", ""),
                    monthly_usd=vb.get("monthly_usd", 0.0),
                    monthly_key=vb.get("monthly_key", ""),
                    by_model=vb.get("by_model", {}),
                    by_feature=vb.get("by_feature", {}),
                    budget_daily_usd=vb.get("budget_daily_usd"),
                    budget_monthly_usd=vb.get("budget_monthly_usd"),
                    thresholds=tuple(vb.get("thresholds", (0.5, 0.8, 1.0))),
                    hard_stop=bool(vb.get("hard_stop", True)),
                    last_daily_alert=float(vb.get("last_daily_alert", 0.0)),
                    last_monthly_alert=float(vb.get("last_monthly_alert", 0.0)),
                )
        self._rate_windows_by_bot = raw.get("rate_windows_by_bot", {}) if isinstance(raw, dict) else {}

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
            "billing_by_bot": {
                k: {
                    "daily_usd": v.daily_usd,
                    "daily_key": v.daily_key,
                    "monthly_usd": v.monthly_usd,
                    "monthly_key": v.monthly_key,
                    "by_model": v.by_model,
                    "by_feature": v.by_feature,
                    "budget_daily_usd": v.budget_daily_usd,
                    "budget_monthly_usd": v.budget_monthly_usd,
                    "thresholds": list(v.thresholds),
                    "hard_stop": v.hard_stop,
                    "last_daily_alert": v.last_daily_alert,
                    "last_monthly_alert": v.last_monthly_alert,
                }
                for k, v in self._billing_by_bot.items()
            },
            "rate_windows_by_bot": self._rate_windows_by_bot,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, raw)

    def billing_for(self, bot_id: str) -> Billing:
        key = str(bot_id)
        b = self._billing_by_bot.get(key)
        if b is None:
            base = self._billing
            b = Billing(
                daily_usd=base.daily_usd,
                daily_key=base.daily_key,
                monthly_usd=base.monthly_usd,
                monthly_key=base.monthly_key,
                by_model=dict(base.by_model),
                by_feature=dict(base.by_feature),
                budget_daily_usd=base.budget_daily_usd,
                budget_monthly_usd=base.budget_monthly_usd,
                thresholds=base.thresholds,
                hard_stop=base.hard_stop,
                last_daily_alert=base.last_daily_alert,
                last_monthly_alert=base.last_monthly_alert,
            )
            self._billing_by_bot[key] = b
        return b

    def rate_windows_for(self, bot_id: str) -> Dict[str, Dict[str, list]]:
        key = str(bot_id)
        return self._rate_windows_by_bot.setdefault(key, {})

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
