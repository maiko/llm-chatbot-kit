"""Small runtime helpers to keep the main bot loop tidy."""

from __future__ import annotations

import logging
from typing import Any, Tuple

# Optional dependency: discord.py. Guard import for test environments.
try:  # pragma: no cover - exercised implicitly by imports
    import discord  # type: ignore
except Exception:  # pragma: no cover - fallback when discord isn't installed

    class _StubStatus:
        offline = "offline"

    class discord:  # type: ignore
        Status = _StubStatus


from .config import Config
from .memory import MemoryStore
from .personality import Personality

logger = logging.getLogger("llm-chatbot-kit")


def _chunk_message(text: str, limit: int = 1990) -> list[str]:
    """Split text into chunks compatible with Discord's message length limit.

    The default limit accounts for a small margin under Discord's ~2000 char cap.
    """
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _build_env_context(message: discord.Message, personality: Personality, i18n: Any) -> str:
    """Build optional environment context string for guild or DM based on persona settings."""
    env_context = ""
    if message.guild:
        members = [m for m in message.guild.members if message.channel.permissions_for(m).read_messages]
        member_lines = "\n".join([f"- {m.display_name} (ID: {m.id})" for m in members[:50]])
        if personality.env_guild_template:
            env_context = personality.env_guild_template.format(
                guild_name=message.guild.name,
                channel_name=getattr(message.channel, "name", str(message.channel.id)),
                member_names=member_lines,
                user_display_name=message.author.display_name,
            )
        else:
            names = ", ".join(sorted({f"{m.display_name} (ID: {m.id})" for m in members})[:50])
            env_context = "\n" + i18n.t("participants_visible", names=names)
        if personality.env_include_emojis:
            try:
                emjs = list(message.guild.emojis)[: max(0, personality.env_emojis_limit)]
                if emjs:
                    lines = "\n".join([f"- :{e.name}: => {e.mention}" for e in emjs])
                    env_context += f"\nEmojis personnalisés disponibles (limité à {personality.env_emojis_limit}):\n{lines}"
            except Exception:
                pass
        if personality.env_include_online_members:
            try:
                online = [m.display_name for m in members if getattr(m, "status", None) and m.status != discord.Status.offline]
                online = sorted(set(online))[: max(0, personality.env_online_limit)]
                if online:
                    env_context += "\n" + i18n.t("online_members", names=", ".join(online))
            except Exception:
                pass
    else:
        if personality.env_dm_template:
            env_context = personality.env_dm_template.format(
                guild_name="",
                channel_name="DM",
                member_names="",
                user_display_name=message.author.display_name,
            )
        else:
            env_context = ""
    return env_context


def _effective_truncation(personality: Personality, store: MemoryStore, message: discord.Message) -> str | None:
    trunc = personality.truncation
    if message.guild:
        try:
            gv = store.guild_settings(message.guild.id).get("truncation")
            if isinstance(gv, str) and gv in ("auto", "disabled"):
                trunc = gv
        except Exception:
            pass
    return trunc


def _effective_model_and_params(
    model_default: str, intervened: bool, personality: Personality, verbosity_default: str | None
) -> Tuple[str, dict | None, str | None]:
    """Return (model, reasoning, verbosity) consistent with GPT-5 rules."""
    model = (personality.listen.generation_model_override or model_default) if intervened else model_default
    is_gpt5 = model.startswith("gpt-5")
    is_chat_latest = model == "gpt-5-chat-latest"
    reasoning = {"effort": "minimal"} if (is_gpt5 and not is_chat_latest) else None
    verbosity = verbosity_default if (is_gpt5 and not is_chat_latest) else None
    return model, reasoning, verbosity


async def _maybe_alert_owner(bot: Any, cfg: Config, store: MemoryStore, i18n: Any) -> None:
    try:
        owner_id = cfg.owner_id
        if not owner_id:
            return
        user = await bot.fetch_user(int(owner_id))
        if not user:
            return
        thresholds = store.billing.thresholds or (0.5, 0.8, 1.0)
        if store.billing.budget_daily_usd:
            ratio = store.billing.daily_usd / store.billing.budget_daily_usd
            for t in thresholds:
                if ratio >= t and store.billing.last_daily_alert < float(t):
                    await user.send(i18n.t("cost_alert_daily", ratio=int(t * 100), spent=f"${store.billing.daily_usd:.2f}"))
                    store.billing.last_daily_alert = float(t)
        if store.billing.budget_monthly_usd:
            ratio = store.billing.monthly_usd / store.billing.budget_monthly_usd
            for t in thresholds:
                if ratio >= t and store.billing.last_monthly_alert < float(t):
                    await user.send(i18n.t("cost_alert_monthly", ratio=int(t * 100), spent=f"${store.billing.monthly_usd:.2f}"))
                    store.billing.last_monthly_alert = float(t)
    except Exception:
        pass
