"""Discord runtime: events, streaming, listening, and command glue.

Keeps the event loop readable and delegates to helpers in `runtime_utils` and
feature modules. Supports mention/DM chat and optional passive listening.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import List

import discord
from discord.ext import commands

from .chat_context import clean_history, current_request_context, history_context, message_metadata, strip_metadata_headers
from .commands import register_commands
from .config import Config
from .costs import rollover_if_needed, usd_cost
from .i18n import load_i18n
from .listener import mark_intervened, should_intervene
from .memory import MemoryStore
from .openai_client import (
    _messages_to_responses_payload,
    chat_complete_with_usage,
    judge_intervention,
)
from .personality import Personality
from .rate_limit import MultiKeySlidingWindow
from .reply_queue import ReplyQueue, ReplyQueueFull
from .runtime_utils import (
    _build_env_context,
    _chunk_message,
    _effective_model_and_params,
    _effective_truncation,
    _maybe_alert_owner,
    render_custom_emojis,
    repair_truncated_mentions,
)
from .streaming import send_stream_as_messages, stream_deltas

logger = logging.getLogger(__name__)


# `_chunk_message` moved to `runtime_utils` for reuse and clarity.


def _conversation(
    messages: List[dict],
    system: str,
    developer: str | None,
    remaining: int,
    *,
    add_meta: bool = True,
) -> List[dict]:
    """Return a full conversation list including system/developer guidance.

    The bot merges persona system + developer guidance into a single system-like
    context by appending the developer prompt to system guidance as a dedicated
    "developer" item for the Responses API (performed downstream when building
    typed items).
    """
    convo = list(messages)
    sys = system
    if developer:
        sys = developer + "\n\n" + system
    if add_meta:
        sys += f"\n\n[meta] {remaining} message(s) remaining in this conversation."
    convo.append({"role": "system", "content": sys})
    return convo


def build_bot(cfg: Config, personality: Personality, *, stream: bool = True) -> commands.Bot:
    """Start the Discord bot event loop.

    Parameters
    ----------
    cfg: Config
        Environment-driven configuration for tokens, models, and limits.
    personality: Personality
        Persona configuration loaded from YAML.
    stream: bool
        Whether to use streaming responses by default (with a natural burst
        sender). Configured backend failures never trigger a second inference.
    """
    if cfg.image_tools_enabled and not (cfg.text_enabled and cfg.image_enabled and cfg.text_api_base_url):
        raise ValueError("IMAGE_TOOLS_ENABLED requires both mode and a Chat Completions backend")
    if cfg.ready_file:
        cfg.ready_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.ready_file.unlink(missing_ok=True)

    def set_ready(ready: bool) -> None:
        if cfg.ready_file:
            if ready:
                cfg.ready_file.touch(mode=0o600)
            else:
                cfg.ready_file.unlink(missing_ok=True)

    intents = discord.Intents.default()
    intents.message_content = cfg.text_enabled
    intents.members = cfg.text_enabled
    intents.presences = cfg.text_enabled

    effective_prefix = personality.command_prefix or cfg.command_prefix

    class KitBot(commands.Bot):
        save_task = None
        images = None
        text_backend = None
        replies = None

        async def setup_hook(self):
            if cfg.text_enabled:

                async def periodic_save():
                    while True:
                        await asyncio.sleep(300)
                        store.save()

                self.save_task = asyncio.create_task(periodic_save(), name="context-save")
            if self.images:
                await self.images.setup()

        async def close(self):
            set_ready(False)
            if self.replies:
                await self.replies.close()
            if self.save_task:
                self.save_task.cancel()
                try:
                    await self.save_task
                except asyncio.CancelledError:
                    pass
                store.save()
            if self.text_backend:
                await self.text_backend.close()
                self.text_backend = None
            if self.images:
                await self.images.close()
                self.images = None
            await super().close()

    if cfg.text_vision_enabled and not (cfg.text_enabled and cfg.text_api_base_url):
        raise ValueError("TEXT_VISION_ENABLED requires a configured Chat Completions backend")
    bot = KitBot(command_prefix=effective_prefix, intents=intents)
    if cfg.text_enabled and cfg.text_api_base_url:
        from .text_client import ChatCompletionsClient

        if not cfg.text_guild_ids or not cfg.text_channel_ids:
            raise ValueError("TEXT_GUILD_IDS and TEXT_CHANNEL_IDS are required for the configured text backend")
        if not cfg.text_api_key or not cfg.text_api_key.strip():
            raise ValueError("TEXT_API_KEY is required for the configured text backend")
        bot.text_backend = ChatCompletionsClient(
            cfg.text_api_base_url,
            cfg.text_api_key,
            cfg.openai_model,
            cfg.text_ca_file,
            max_tokens=cfg.text_max_tokens,
            tool_max_tokens=cfg.text_tool_max_tokens,
            timeout_seconds=cfg.text_timeout_seconds,
        )
    store = MemoryStore(cfg.store_path) if cfg.text_enabled else None
    if cfg.image_enabled:
        from .image_commands import ImageCommands
        from .image_config import ImageConfig

        bot.images = ImageCommands(bot, ImageConfig.from_env(), personality.language, cfg.owner_id)
        if bot.text_backend and cfg.serialize_backends:
            bot.images.worker.execution_lock = bot.text_backend.lock
    i18n = load_i18n(personality.language, overrides=personality.messages)

    # Compute effective judge model locally (avoid mutating personality at runtime)
    def effective_judge_model() -> str:
        try:
            jm = getattr(personality, "listen", None).judge_model  # type: ignore
            if isinstance(jm, str) and "mini" in jm:
                return "gpt-5-nano"
            return jm or "gpt-5-nano"
        except Exception:
            return "gpt-5-nano"

    def _strip_leading_self_mention(text: str) -> str:
        """Remove a leading mention of the bot itself from text (e.g., '<@id>' or '<@!id>')."""
        if not text or not bot.user:
            return text
        toks = [f"<@{bot.user.id}>", f"<@!{bot.user.id}>"]
        out = text
        changed = True
        while changed:
            changed = False
            s = out.lstrip()
            for t in toks:
                if s.startswith(t):
                    s = s[len(t) :].lstrip(" :,–-\u2013\u2014")
                    changed = True
            if changed:
                out = s
        return out

    @bot.event
    async def on_ready():
        set_ready(True)
        logger.info("Connected as %s", bot.user)
        if cfg.text_enabled and personality.env_include_emojis:
            for guild in bot.guilds:
                logger.info(
                    "custom_emoji_inventory guild=%s count=%s context_limit=%s", guild.id, len(guild.emojis), personality.env_emojis_limit
                )

    @bot.event
    async def on_disconnect():
        set_ready(False)

    @bot.event
    async def on_resumed():
        set_ready(True)

    @bot.event
    async def on_message(message: discord.Message):
        if not cfg.text_enabled or message.webhook_id:
            return

        if bot.text_backend:
            roles = {role.id for role in getattr(message.author, "roles", [])}
            if (
                not message.guild
                or message.guild.id not in cfg.text_guild_ids
                or message.channel.id not in cfg.text_channel_ids
                or (cfg.text_role_ids and not roles & cfg.text_role_ids)
            ):
                return

        is_dm = message.guild is None
        is_mentioned = bot.user and bot.user.mentioned_in(message)
        peer_message = bool(message.author.bot)
        mentioned_ids = {member.id for member in getattr(message, "mentions", [])}
        if peer_message and (
            not cfg.text_bot_chat_enabled
            or not message.guild
            or not bot.user
            or message.author.id == bot.user.id
            or message.author.id not in cfg.text_bot_chat_peer_ids
            or bot.user.id not in mentioned_ids
        ):
            return
        content = (message.content or "").strip()

        word_triggered = False
        try:
            if getattr(personality, "triggers", None) and personality.triggers.enabled:
                words = personality.triggers.words or []
                if words:
                    if personality.triggers.use_regex:
                        for pat in words:
                            try:
                                if re.search(pat, content, flags=re.IGNORECASE):
                                    word_triggered = True
                                    break
                            except Exception:
                                continue
                    else:
                        low = content.lower()
                        word_triggered = any((w or "").lower() in low for w in words if isinstance(w, str))
        except Exception:
            word_triggered = False

        logger.info("message guild=%s channel=%s author=%s", getattr(message.guild, "id", None), message.channel.id, message.author.id)

        if not peer_message:
            await bot.process_commands(message)

        # If this message targets our command prefix, don't treat it as chat input
        if content.startswith(effective_prefix):
            logger.debug("command-detected: prefix=%s", effective_prefix)
            return

        primary_trigger = False
        try:
            on_mention_enabled = getattr(personality, "triggers", None) is None or personality.triggers.on_mention
        except Exception:
            on_mention_enabled = True
        if is_dm or (is_mentioned and on_mention_enabled) or word_triggered:
            primary_trigger = True

        if peer_message and not primary_trigger:
            return
        if cfg.text_bot_chat_enabled and message.guild:
            settings = store.guild_settings(message.guild.id)
            budgets = settings.setdefault("bot_chat_replies", {})
            channel_key = str(message.channel.id)
            if peer_message:
                used = int(budgets.get(channel_key, 0))
                if used >= cfg.text_bot_chat_max_replies:
                    logger.info("bot_chat limit_reached channel=%s", message.channel.id)
                    return
                # Reserve synchronously before queue admission. Failures also consume
                # the budget, so restarts/errors cannot create an unbounded loop.
                budgets[channel_key] = used + 1
                store.save()
            elif mentioned_ids & (cfg.text_bot_chat_peer_ids | ({bot.user.id} if bot.user else set())):
                budgets[channel_key] = 0
                store.save()

        # Observing a permitted human or addressed trusted peer is independent of replying.
        # Record before busy/listening/quota gates, without consuming a turn.
        channel_id = message.channel.id
        ctx = store.get(channel_id)
        author_name = getattr(message.author, "display_name", str(message.author.id))
        ctx.messages.append(
            {**message_metadata(message), "role": "user", "content": f"{author_name}: {content}", "addressed": bool(primary_trigger)}
        )
        ctx.messages[:] = ctx.messages[-100:]
        store.save()
        # A later event may arrive while a judge or inference is awaited. Keep
        # this event's input snapshot ending at its own message.
        turn_history = list(ctx.messages)

        if primary_trigger:
            try:
                await bot.replies.submit(message, turn_history)
            except ReplyQueueFull:
                notice = (
                    "La file de réponses est pleine ; réessaie plus tard."
                    if (personality.language or "en").startswith("fr")
                    else "The reply queue is full; please retry later."
                )
                await message.channel.send(notice, allowed_mentions=discord.AllowedMentions.none())
            return
        if bot.replies.busy or (bot.text_backend and bot.text_backend.lock.locked()):
            return
        await respond(message, False, turn_history)

    async def text_access(message):
        if not bot.text_backend:
            return True
        if not message.guild or message.guild.id not in cfg.text_guild_ids or message.channel.id not in cfg.text_channel_ids:
            return False
        member = message.author
        if hasattr(message.guild, "get_member"):
            member = message.guild.get_member(message.author.id)
            if member is None:
                try:
                    member = await message.guild.fetch_member(message.author.id)
                except discord.HTTPException:
                    return False
        if cfg.text_role_ids and not {role.id for role in getattr(member, "roles", [])} & cfg.text_role_ids:
            return False
        if hasattr(message.channel, "permissions_for"):
            if not message.channel.permissions_for(member).view_channel:
                return False
            if getattr(message.guild, "me", None):
                perms = message.channel.permissions_for(message.guild.me)
                send = perms.send_messages_in_threads if isinstance(message.channel, discord.Thread) else perms.send_messages
                if not perms.view_channel or not send:
                    return False
        return True

    reply_lock = asyncio.Lock()

    async def respond(message, primary_trigger, turn_history):
        async with reply_lock:
            await perform_response(message, primary_trigger, turn_history)

    async def perform_response(message, primary_trigger, turn_history):
        if not await text_access(message):
            return
        is_dm = message.guild is None
        content = (message.content or "").strip()
        intervened = False
        channel_id = message.channel.id
        ctx = store.get(channel_id)
        # Earlier queued replies may have completed after this event arrived.
        # Insert those answers after their own user event, never later inputs.
        fresh = [m for m in ctx.messages if m.get("role") == "assistant" and m not in turn_history]
        enriched = []
        for item in turn_history:
            enriched.append(item)
            if item.get("message_id"):
                enriched.extend(m for m in fresh if m.get("in_reply_to") == item["message_id"])
        turn_history = enriched

        bot_id_str = str(getattr(bot.user, "id", "")) if bot.user else ""
        rl_caps: dict[str, list[tuple[int, int]]] = {}
        try:
            if getattr(personality, "rate_limit", None):
                if personality.rate_limit.channel:
                    rl_caps["channel"] = [(int(d.window), int(d.max)) for d in personality.rate_limit.channel]
                if personality.rate_limit.dm_user:
                    rl_caps["dm_user"] = [(int(d.window), int(d.max)) for d in personality.rate_limit.dm_user]
                if personality.rate_limit.trigger_user:
                    rl_caps["trigger_user"] = [(int(d.window), int(d.max)) for d in personality.rate_limit.trigger_user]
                if personality.rate_limit.global_:
                    rl_caps["global"] = [(int(d.window), int(d.max)) for d in personality.rate_limit.global_]
        except Exception:
            rl_caps = {}
        limiter = MultiKeySlidingWindow(rl_caps, store.rate_windows_for(bot_id_str)) if bot_id_str else None

        if not primary_trigger:
            # Consider spontaneous intervention in guild channels
            if message.guild:
                gs = store.guild_settings(message.guild.id)
                # Ignore common foreign bot prefixes to avoid butting in
                common_prefixes = ("!", "/", ".", ":", ";", ")", "(", ">", "<", "?", "#", "$")
                if content and content[0] in common_prefixes and not content.startswith(effective_prefix):
                    logger.debug("listen-skip: foreign prefix=%r", content[0])
                    return
                ok, intent = should_intervene(
                    personality,
                    gs,
                    message.channel.id,
                    getattr(message.channel, "name", None),
                    int(getattr(message.author, "id", 0) or 0),
                    getattr(message.author, "bot", False),
                    content,
                )
                if not ok:
                    logger.debug("listen-skip: heuristics not triggered")
                    return
                # Local listening uses the same configured model, never a provider escalation.
                if personality.listen.judge_enabled and bot.text_backend:
                    judge_limit = max(1, min(50, personality.listen.judge_max_context_messages))
                    judge_msgs = turn_history[:-1][-judge_limit:] + [{"role": "user", "content": content}]
                    accepted, j_intent, conf = await bot.text_backend.judge(judge_msgs, personality.listen.judge_threshold)
                    if not accepted:
                        return
                    intent = j_intent
                if personality.listen.judge_enabled and not bot.text_backend:
                    # Build context from actual channel history: last 10 messages with timestamps
                    judge_msgs: List[dict]
                    try:
                        hist_msgs = []
                        async for m in message.channel.history(limit=10, oldest_first=True):
                            role = "assistant" if (bot.user and m.author.id == bot.user.id) else "user"
                            ts = getattr(m, "created_at", None)
                            if ts is not None:
                                ts_s = ts.strftime("%Y-%m-%d %H:%M") + " UTC"
                            else:
                                ts_s = ""
                            author = getattr(m.author, "display_name", str(m.author))
                            txt = (m.content or "").strip()
                            hist_msgs.append({"role": role, "content": f"[{ts_s}] {author}: {txt}"})
                        judge_msgs = hist_msgs
                    except Exception:
                        # Fallback: use in-memory context (no timestamps)
                        hist = turn_history[:-1][-max(1, personality.listen.judge_max_context_messages) :]
                        judge_msgs = hist + [turn_history[-1]]
                    accepted, j_intent, conf = await asyncio.to_thread(
                        judge_intervention,
                        cfg.openai_api_key,
                        effective_judge_model(),
                        judge_msgs,
                        personality.listen.judge_threshold,
                    )
                    logger.info(
                        "listen-judge: model=%s accepted=%s conf=%.2f intent=%s",
                        personality.listen.judge_model,
                        accepted,
                        conf,
                        j_intent,
                    )
                    if not accepted and "nano" in effective_judge_model() and 0.4 <= conf < personality.listen.judge_threshold:
                        accepted, j_intent, conf = await asyncio.to_thread(
                            judge_intervention, cfg.openai_api_key, "gpt-5-mini", judge_msgs, personality.listen.judge_threshold
                        )
                        logger.info(
                            "listen-judge-escalate: model=%s accepted=%s conf=%.2f intent=%s",
                            "gpt-5-mini",
                            accepted,
                            conf,
                            j_intent,
                        )
                    if not accepted:
                        logger.debug("listen-skip: judge rejected")
                        return
                    intent = j_intent or intent

                # Budget hard stop (global) for interventions only
                b = store.billing
                if b.hard_stop and (
                    (b.budget_daily_usd and b.daily_usd >= b.budget_daily_usd)
                    or (b.budget_monthly_usd and b.monthly_usd >= b.budget_monthly_usd)
                ):
                    logger.info("listen-skip: budget hard stop active")
                    return
                # Persona-level budgets just for interventions
                if personality.listen.cost_daily_usd and store.billing.daily_usd >= personality.listen.cost_daily_usd:
                    logger.info("listen-skip: persona daily budget reached")
                    return
                if personality.listen.cost_monthly_usd and store.billing.monthly_usd >= personality.listen.cost_monthly_usd:
                    logger.info("listen-skip: persona monthly budget reached")
                    return
                intervened = True

        # Build optional environment context
        env_context = _build_env_context(message, personality, i18n)

        if not intervened and ctx.turns >= cfg.max_turns:
            await message.channel.send(i18n.t("limit_reached", max_turns=cfg.max_turns, prefix=effective_prefix))
            return

        remaining = max(0, cfg.max_turns - ctx.turns - 1)
        # Select truncation strategy (per-guild override if present) BEFORE building conversation
        effective_truncation = _effective_truncation(personality, store, message)
        # Append a dynamic reminder in the developer message to avoid self-mentions
        dev_base = (personality.developer_prompt or "") + current_request_context(message, personality.language)
        try:
            if bot.user and getattr(bot.user, "id", None):
                bot_id = bot.user.id
                if (personality.language or "").lower().startswith("fr"):
                    reminder = f"\n\nRappel: <@{bot_id}> est ton propre ID. Ne te mentionne pas dans tes réponses."
                else:
                    reminder = f"\n\nReminder: <@{bot_id}> is your own ID. Do not mention yourself in replies."
                dev_base = (dev_base or "") + reminder
        except Exception:
            pass

        # If intervening, add a light tone directive and respect joke bias
        if intervened:
            try:
                if intent != "joke" and "?" not in content and float(personality.listen.joke_bias) > 0:
                    import random as _r

                    if _r.random() < float(personality.listen.joke_bias):
                        intent = "joke"
            except Exception:
                pass
            if intent == "joke":
                dev_base += "\n\nTone: brief, witty if appropriate; keep it helpful and concise."
            elif intent == "snark":
                dev_base += "\n\nTone: light snark acceptable; stay friendly and concise."
            else:
                # help (default) intent
                dev_base += "\n\nTone: helpful, direct, and concise."

        # Decide whether to include meta based on truncation: hide when active (auto)
        truncation_active = effective_truncation == "auto"
        try:
            include_n = int(getattr(personality, "context", None).include_last_n) if getattr(personality, "context", None) else 10
        except Exception:
            include_n = 10
        HARD_CAP = 100
        include_n = max(1, min(include_n, HARD_CAP))
        include_non_addr = True
        try:
            if getattr(personality, "context", None) is not None:
                include_non_addr = bool(personality.context.include_non_addressed_messages)
        except Exception:
            include_non_addr = True

        if include_non_addr:
            history = turn_history[-include_n:]
        else:
            filtered = []
            for m in turn_history:
                r = m.get("role")
                if r == "assistant":
                    filtered.append(m)
                elif r == "user" and m.get("addressed"):
                    filtered.append(m)
            history = filtered[-include_n:]

        b_bot = store.billing_for(getattr(bot.user, "id", 0)) if bot.user else store.billing
        try:
            if getattr(personality, "billing", None) and bool(personality.billing.paused):
                logger.info("generation blocked: persona billing paused")
                return
            if getattr(personality, "billing", None):
                if personality.billing.hard_limit_daily_usd is not None and b_bot.daily_usd >= float(
                    personality.billing.hard_limit_daily_usd
                ):
                    logger.info("generation blocked: persona hard daily limit reached")
                    return
                if personality.billing.hard_limit_monthly_usd is not None and b_bot.monthly_usd >= float(
                    personality.billing.hard_limit_monthly_usd
                ):
                    logger.info("generation blocked: persona hard monthly limit reached")
                    return
        except Exception:
            pass

        # Charge one response event, never its individual streaming bursts.
        # Once admitted, finish delivery even if a quota window fills meanwhile.
        if limiter is not None:
            keys = {"global": "all", "channel": str(message.channel.id), "trigger_user": str(message.author.id)}
            if is_dm:
                keys["dm_user"] = str(message.author.id)
            delay = limiter.reserve(keys)
            while delay:
                if intervened:
                    return
                await asyncio.sleep(delay)
                if not await text_access(message):
                    return
                delay = limiter.reserve(keys)
            store.save()

        convo = _conversation(
            clean_history(history),
            personality.system_prompt + (env_context or ""),
            history_context(history) + dev_base,
            remaining,
            add_meta=not truncation_active,
        )

        source = None
        if (
            primary_trigger
            and not message.author.bot
            and bot.text_backend
            and (cfg.text_vision_enabled or (cfg.image_tools_enabled and bot.images and bot.images.cfg.edits_enabled))
        ):
            from .discord_media import resolve_source, with_image
            from .image_client import ImageError
            from .image_tools import tool_error

            try:
                source = await resolve_source(message)
                if source is not None and cfg.text_vision_enabled:
                    convo = with_image(convo, source, message.id)
                if not await text_access(message):
                    return
            except ImageError as exc:
                reply = (
                    tool_error(personality.language or "en", str(exc))
                    .replace("Génération non lancée", "Photo non traitée")
                    .replace("Generation not started", "Photo not processed")
                )
                await message.channel.send(reply, allowed_mentions=discord.AllowedMentions.none())
                return

        # Build Responses API typed input items (developer/user/assistant)
        input_items = _messages_to_responses_payload(convo) if not bot.text_backend else []

        # Stream (default) or non-stream path
        input_tokens = output_tokens = cached_tokens = 0
        use_image_tool = bool(cfg.image_tools_enabled and primary_trigger and not intervened and not message.author.bot)
        use_stream = stream
        # Select model and parameters (allow override for interventions)
        gen_model, reasoning, verbosity = _effective_model_and_params(cfg.openai_model, intervened, personality, cfg.openai_verbosity)
        if use_stream:
            try:
                if use_image_tool:
                    from .image_tools import ImageToolStream

                    deltas = ImageToolStream(bot.text_backend, bot.images, convo, message, source)
                elif bot.text_backend:
                    deltas = bot.text_backend.deltas(convo)
                else:
                    deltas = await stream_deltas(
                        cfg.openai_api_key,
                        gen_model,
                        input_items,
                        reasoning=reasoning,
                        verbosity=verbosity,
                        truncation=effective_truncation,
                    )
                logger.info("generate: streaming model=%s", gen_model)
                # Allow user mentions (to interact with others), block roles/everyone; strip only self-mention token
                no_pings = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)
                try:
                    final_text = await send_stream_as_messages(
                        message.channel,
                        deltas,
                        rate_hz=personality.stream_rate_hz,
                        min_first=personality.stream_min_first,
                        min_next=personality.stream_min_next,
                        strip_leading=[f"<@{bot.user.id}>", f"<@!{bot.user.id}>"] if bot.user else None,
                        allowed_mentions=no_pings,
                        max_total_chars=(personality.listen.response_max_chars if intervened else None),
                    )
                finally:
                    if hasattr(deltas, "aclose"):
                        await deltas.aclose()
                # Capture usage if available
                if getattr(deltas, "usage", None):
                    input_tokens, output_tokens, cached_tokens = deltas.usage  # type: ignore
            except Exception as e:
                logger.warning("generate: streaming failed backend=%s error=%s", "local" if bot.text_backend else "cloud", type(e).__name__)
                if bot.text_backend:
                    # No second generation after a configured backend streaming failure.
                    await message.channel.send(
                        i18n.t("text_response_truncated" if str(e) == "text_backend_response_truncated" else "generic_error"),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                use_stream = False

        if not use_stream:
            try:
                logger.info("generate: non-stream model=%s", gen_model)
                if use_image_tool:
                    from .image_tools import complete_with_image_tool

                    final_text, usage = await complete_with_image_tool(bot.text_backend, bot.images, convo, message, source)
                else:
                    final_text, usage = (
                        await bot.text_backend.complete(convo)
                        if bot.text_backend
                        else await asyncio.to_thread(
                            chat_complete_with_usage,
                            api_key=cfg.openai_api_key,
                            model=gen_model,
                            messages=convo,
                            reasoning=reasoning,
                            verbosity=verbosity,
                            truncation=effective_truncation,
                        )
                    )
                input_tokens, output_tokens, cached_tokens = usage
                # Sanitize leading self-mention; allow user mentions (block roles/everyone)
                final_text = render_custom_emojis(
                    _strip_leading_self_mention(repair_truncated_mentions(strip_metadata_headers(final_text), message)), message.guild
                )
                if intervened and personality.listen.response_max_chars:
                    final_text = final_text[: max(0, int(personality.listen.response_max_chars))]
                no_pings = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)
                for chunk in _chunk_message(final_text):
                    await message.channel.send(chunk, allowed_mentions=no_pings)
            except Exception as e2:
                logger.warning(
                    "generate: non-stream failed exception=%s truncated=%s", type(e2).__name__, str(e2) == "text_backend_response_truncated"
                )
                final_text = i18n.t("text_response_truncated" if str(e2) == "text_backend_response_truncated" else "generic_error")
                no_pings = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)
                await message.channel.send(final_text, allowed_mentions=no_pings)

        # Optional moderation (persona listen setting)
        if intervened and personality.listen.moderation_enabled and not bot.text_backend:
            from .openai_client import moderate_text

            allowed = await asyncio.to_thread(moderate_text, cfg.openai_api_key, personality.listen.moderation_model, final_text)
            if not allowed:
                # Skip sending content (already sent if streaming; in that case, this should be disabled or pre-moderated)
                # For simplicity, do nothing extra here.
                pass

        # Update memory after completion
        ctx.turns += 1
        # Persist the sanitized final text in memory for context dumps
        final_text = _strip_leading_self_mention(final_text)
        answer = {
            "role": "assistant",
            "content": final_text,
            "in_reply_to": str(message.id),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "author_id": str(bot.user.id) if bot.user else None,
            "author_name": str(getattr(bot.user, "display_name", "bot")),
            "author_kind": "bot",
        }
        index = next((i + 1 for i, item in enumerate(ctx.messages) if item.get("message_id") == str(message.id)), len(ctx.messages))
        ctx.messages.insert(index, answer)
        ctx.messages[:] = ctx.messages[-100:]
        # Mark intervention cooldown if applicable
        if intervened and message.guild:
            gs = store.guild_settings(message.guild.id)
            mark_intervened(gs, message.channel.id, int(getattr(message.author, "id", 0) or 0))

        # Cost tracking (per-bot) and alerts
        try:
            bcur = store.billing_for(getattr(bot.user, "id", 0)) if bot.user else store.billing
            rollover_if_needed(bcur)
            used_model = gen_model if "gen_model" in locals() else cfg.openai_model
            cost = 0.0 if bot.text_backend else usd_cost(used_model, input_tokens, output_tokens, cached_tokens)
            bcur.daily_usd += cost
            bcur.monthly_usd += cost
            tier = used_model
            bcur.by_model[tier] = bcur.by_model.get(tier, 0.0) + cost
            feat = "listen" if intervened else "mention_or_dm"
            bcur.by_feature[feat] = bcur.by_feature.get(feat, 0.0) + cost
            store.save()
            logger.info(
                "usage model=%s input=%d output=%d cached=%d cost=$%.4f feature=%s channel=%s guild=%s",
                used_model,
                input_tokens,
                output_tokens,
                cached_tokens,
                cost,
                feat,
                getattr(message.channel, "id", None),
                getattr(message.guild, "id", None),
            )
            await _maybe_alert_owner(bot, cfg, store, i18n)
        except Exception:
            pass

        store.save()

    if cfg.text_enabled:
        bot.replies = ReplyQueue(respond, lambda: bot.user, cfg.text_queue_limit)
        register_commands(bot, store, cfg, i18n, personality, effective_prefix)
    return bot


def run(cfg: Config, personality: Personality, *, stream: bool = True) -> None:
    if not cfg.discord_token:
        raise ValueError("DISCORD_TOKEN is required")
    if cfg.text_enabled:
        key_name = "TEXT_API_KEY" if cfg.text_api_base_url else "OPENAI_API_KEY"
        key = cfg.text_api_key if cfg.text_api_base_url else cfg.openai_api_key
        if not key or not key.strip():
            raise ValueError(f"{key_name} is required in chat/both mode")
    bot = build_bot(cfg, personality, stream=stream)
    bot.run(cfg.discord_token)
