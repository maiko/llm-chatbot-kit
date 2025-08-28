"""Discord command registration to keep the main runtime tidy."""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

from .config import Config
from .costs import rollover_if_needed
from .memory import MemoryStore
from .personality import Personality
from .runtime_utils import _chunk_message


def register_commands(
    bot: commands.Bot,
    store: MemoryStore,
    cfg: Config,
    i18n: Any,
    personality: Personality,
    effective_prefix: str,
) -> None:
    """Register all text commands on the provided bot instance."""

    @bot.command(name="context")
    async def context_cmd(ctx_cmd: commands.Context):
        ctx = store.get(ctx_cmd.channel.id)
        if not ctx.messages:
            await ctx_cmd.send(i18n.t("no_history"))
            return
        text = "\n".join([f"{m['role']}: {m['content']}" for m in ctx.messages][-10:])
        for chunk in _chunk_message(text, limit=1970):
            await ctx_cmd.send(f"```\n{chunk}\n```")

    @bot.command(name="reset")
    async def reset_cmd(ctx_cmd: commands.Context):
        store.reset(ctx_cmd.channel.id)
        await ctx_cmd.send(i18n.t("context_cleared"))

    @bot.command(name="reboot")
    async def reboot_cmd(ctx_cmd: commands.Context):
        if cfg.owner_id and str(ctx_cmd.author.id) != str(cfg.owner_id):
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        store.reset_all()
        await ctx_cmd.send(i18n.t("all_contexts_cleared"))

    # Listen control commands
    @bot.group(name="listen", invoke_without_command=True)
    async def listen_group(ctx_cmd: commands.Context):
        await ctx_cmd.send(i18n.t("listen_usage", prefix=effective_prefix))

    @listen_group.command(name="on")
    async def listen_on(ctx_cmd: commands.Context):
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        gs = store.guild_settings(ctx_cmd.guild.id)
        gs["listen_enabled"] = True
        store.save()
        await ctx_cmd.send(i18n.t("listen_enabled_on"))

    @listen_group.command(name="off")
    async def listen_off(ctx_cmd: commands.Context):
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        gs = store.guild_settings(ctx_cmd.guild.id)
        gs["listen_enabled"] = False
        store.save()
        await ctx_cmd.send(i18n.t("listen_enabled_off"))

    @listen_group.command(name="status")
    async def listen_status(ctx_cmd: commands.Context):
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        gs = store.guild_settings(ctx_cmd.guild.id)
        denied = gs.get("denied_channels", [])
        reasoning = "effort=None" if (cfg.openai_model.startswith("gpt-5") or cfg.openai_model.startswith("o-")) else "disabled"
        await ctx_cmd.send(
            i18n.t(
                "listen_status",
                enabled=str(gs.get("listen_enabled", False)),
                denied=", ".join(map(str, denied)) or "-",
                model=cfg.openai_model,
                reasoning=reasoning,
            )
        )

    @listen_group.command(name="ban")
    async def listen_ban(ctx_cmd: commands.Context, channel: discord.TextChannel):
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        gs = store.guild_settings(ctx_cmd.guild.id)
        denied = set(gs.get("denied_channels", []))
        denied.add(str(channel.id))
        gs["denied_channels"] = list(denied)
        store.save()
        await ctx_cmd.send(i18n.t("listen_banned", channel=channel.mention))

    @listen_group.command(name="unban")
    async def listen_unban(ctx_cmd: commands.Context, channel: discord.TextChannel):
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        gs = store.guild_settings(ctx_cmd.guild.id)
        denied = set(gs.get("denied_channels", []))
        if str(channel.id) in denied:
            denied.remove(str(channel.id))
            gs["denied_channels"] = list(denied)
            store.save()
            await ctx_cmd.send(i18n.t("listen_unbanned", channel=channel.mention))
        else:
            await ctx_cmd.send(i18n.t("listen_not_banned", channel=channel.mention))

    # Emoji commands
    @bot.group(name="emoji", invoke_without_command=True)
    async def emoji_group(ctx_cmd: commands.Context):
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        await ctx_cmd.send(i18n.t("emoji_usage", prefix=effective_prefix))

    @emoji_group.command(name="list")
    async def emoji_list(ctx_cmd: commands.Context):
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        emjs = list(ctx_cmd.guild.emojis)
        if not emjs:
            await ctx_cmd.send(i18n.t("emoji_none"))
            return
        lines = [f":{e.name}: => {e.mention}" for e in emjs]
        joined = "\n".join(lines)
        for chunk in _chunk_message(joined, limit=1970):
            await ctx_cmd.send(f"```\n{chunk}\n```")

    # Costs commands
    @bot.group(name="cost", invoke_without_command=True)
    async def cost_group(ctx_cmd: commands.Context):
        await ctx_cmd.send(i18n.t("cost_usage", prefix=effective_prefix))

    @cost_group.command(name="status")
    async def cost_status(ctx_cmd: commands.Context):
        bot_user = getattr(ctx_cmd.bot, "user", None)
        b = store.billing_for(getattr(bot_user, "id", 0)) if bot_user else store.billing
        rollover_if_needed(b)
        reasoning = "effort=None" if (cfg.openai_model.startswith("gpt-5") or cfg.openai_model.startswith("o-")) else "disabled"
        await ctx_cmd.send(
            i18n.t(
                "cost_status",
                daily=f"${b.daily_usd:.2f}",
                monthly=f"${b.monthly_usd:.2f}",
                model=cfg.openai_model,
                reasoning=reasoning,
            )
        )

    @cost_group.command(name="limit")
    async def cost_limit(ctx_cmd: commands.Context, scope: str, amount: float):
        if cfg.owner_id and str(ctx_cmd.author.id) != str(cfg.owner_id):
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        bot_user = getattr(ctx_cmd.bot, "user", None)
        b = store.billing_for(getattr(bot_user, "id", 0)) if bot_user else store.billing
        if scope.lower() == "daily":
            b.budget_daily_usd = float(amount)
            store.save()
            await ctx_cmd.send(i18n.t("cost_budget_set", scope="daily", amount=f"${amount:.2f}"))
        elif scope.lower() == "monthly":
            b.budget_monthly_usd = float(amount)
            store.save()
            await ctx_cmd.send(i18n.t("cost_budget_set", scope="monthly", amount=f"${amount:.2f}"))
        else:
            await ctx_cmd.send(i18n.t("cost_usage", prefix=effective_prefix))

    @cost_group.command(name="hardstop")
    async def cost_hardstop(ctx_cmd: commands.Context, value: str):
        if cfg.owner_id and str(ctx_cmd.author.id) != str(cfg.owner_id):
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        v = value.lower() in ("on", "true", "1", "yes")
        bot_user = getattr(ctx_cmd.bot, "user", None)
        b = store.billing_for(getattr(bot_user, "id", 0)) if bot_user else store.billing
        b.hard_stop = v
        store.save()
        await ctx_cmd.send(i18n.t("cost_hardstop_set", value=str(v)))

    @cost_group.command(name="pause")
    async def cost_pause(ctx_cmd: commands.Context, value: str):
        if cfg.owner_id and str(ctx_cmd.author.id) != str(cfg.owner_id):
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        bot_user = getattr(ctx_cmd.bot, "user", None)
        b = store.billing_for(getattr(bot_user, "id", 0)) if bot_user else store.billing
        v = value.lower() in ("on", "true", "1", "yes")
        b.hard_stop = bool(v)
        store.save()
        await ctx_cmd.send(f"Pause set to {v}")

    # Truncation (per-guild) commands
    @bot.group(name="truncation", invoke_without_command=True)
    async def truncation_group(ctx_cmd: commands.Context):
        await ctx_cmd.send(i18n.t("truncation_usage", prefix=effective_prefix))

    @truncation_group.command(name="status")
    async def truncation_status(ctx_cmd: commands.Context):
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        gs = store.guild_settings(ctx_cmd.guild.id)
        current = gs.get("truncation", None)
        cur_val = current if current is not None else "(inherit)"
        await ctx_cmd.send(i18n.t("truncation_status", value=str(cur_val), default=str(personality.truncation or "disabled")))

    @truncation_group.command(name="set")
    async def truncation_set(ctx_cmd: commands.Context, value: str):
        if cfg.owner_id and str(ctx_cmd.author.id) != str(cfg.owner_id):
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        if not ctx_cmd.guild:
            await ctx_cmd.send(i18n.t("owner_only"))
            return
        v = value.lower()
        if v not in ("auto", "disabled"):
            await ctx_cmd.send(i18n.t("truncation_usage", prefix=effective_prefix))
            return
        gs = store.guild_settings(ctx_cmd.guild.id)
        gs["truncation"] = v
        store.save()
        await ctx_cmd.send(i18n.t("truncation_set", value=v))
