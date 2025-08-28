"""Discord runtime: events, streaming, listening, and command glue.

Keeps the event loop readable and delegates to helpers in `runtime_utils` and
feature modules. Supports mention/DM chat and optional passive listening.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import List

import discord
from discord.ext import commands

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
from .runtime_utils import (
    _build_env_context,
    _chunk_message,
    _effective_model_and_params,
    _effective_truncation,
    _maybe_alert_owner,
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


def run(cfg: Config, personality: Personality, *, stream: bool = True) -> None:
    """Start the Discord bot event loop.

    Parameters
    ----------
    cfg: Config
        Environment-driven configuration for tokens, models, and limits.
    personality: Personality
        Persona configuration loaded from YAML.
    stream: bool
        Whether to use streaming responses by default (with a natural burst
        sender). If streaming fails, gracefully falls back to non-streaming.
    """
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.presences = True

    effective_prefix = personality.command_prefix or cfg.command_prefix
    bot = commands.Bot(command_prefix=effective_prefix, intents=intents)
    store = MemoryStore(cfg.store_path)
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
        toks = [f"<@{bot.user.id}", f"<@!{bot.user.id}"]
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
        logger.info("Connected as %s", bot.user)

        async def periodic_save():
            while True:
                await asyncio.sleep(300)
                store.save()

        bot.loop.create_task(periodic_save())

    @bot.event
    async def on_message(message: discord.Message):
        if message.author == bot.user:
            return

        is_dm = message.guild is None
        is_mentioned = bot.user and bot.user.mentioned_in(message)
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

        logger.info(
            "on_message: mode=%s mention=%s guild=%s channel=%s author=%s content=%r",
            "DM" if is_dm else "GUILD",
            bool(is_mentioned),
            getattr(message.guild, "name", None),
            getattr(message.channel, "name", None),
            getattr(message.author, "display_name", None),
            content[:120],
        )

        await bot.process_commands(message)

        # If this message targets our command prefix, don't treat it as chat input
        if content.startswith(effective_prefix):
            logger.info("command-detected: prefix=%s content=%r", effective_prefix, content[:80])
            return
        intervened = False

        primary_trigger = False
        try:
            on_mention_enabled = getattr(personality, "triggers", None) is None or personality.triggers.on_mention
        except Exception:
            on_mention_enabled = True
        if is_dm or (is_mentioned and on_mention_enabled) or word_triggered:
            primary_trigger = True

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

        async def send_gate() -> bool:
            if limiter is None:
                return True
            ok = True
            try:
                ok = limiter.allow("global", "all") and ok
                ok = limiter.allow("channel", str(message.channel.id)) and ok
                if is_dm:
                    ok = limiter.allow("dm_user", str(getattr(message.author, "id", ""))) and ok
                ok = limiter.allow("trigger_user", str(getattr(message.author, "id", ""))) and ok
                if ok:
                    store.save()
            except Exception:
                return False
            return ok

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
                # Optional LLM judge step
                if personality.listen.judge_enabled:
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
                        pre_ctx = store.get(message.channel.id)
                        hist = pre_ctx.messages[-max(1, personality.listen.judge_max_context_messages) :]
                        judge_msgs = hist + [{"role": "user", "content": f"{message.author.display_name}: {content}"}]
                    accepted, j_intent, conf = judge_intervention(
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
                        accepted, j_intent, conf = judge_intervention(
                            cfg.openai_api_key, "gpt-5-mini", judge_msgs, personality.listen.judge_threshold
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

        channel_id = message.channel.id
        ctx = store.get(channel_id)

        # Build optional environment context
        env_context = _build_env_context(message, personality, i18n)

        user_msg = f"{message.author.display_name}: {content}"
        addressed_now = bool(primary_trigger)
        ctx.messages.append({"role": "user", "content": user_msg, "addressed": addressed_now})

        if not intervened and ctx.turns >= cfg.max_turns:
            await message.channel.send(i18n.t("limit_reached", max_turns=cfg.max_turns, prefix=effective_prefix))
            return

        remaining = max(0, cfg.max_turns - ctx.turns - 1)
        # Select truncation strategy (per-guild override if present) BEFORE building conversation
        effective_truncation = _effective_truncation(personality, store, message)
        # Append a dynamic reminder in the developer message to avoid self-mentions
        dev_base = personality.developer_prompt or ""
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
        HARD_CAP = 50
        include_n = max(1, min(include_n, HARD_CAP))
        include_non_addr = True
        try:
            if getattr(personality, "context", None) is not None:
                include_non_addr = bool(personality.context.include_non_addressed_messages)
        except Exception:
            include_non_addr = True

        if include_non_addr:
            history = ctx.messages[-include_n:]
        else:
            filtered = []
            for m in ctx.messages:
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

        convo = _conversation(
            history,
            personality.system_prompt + (env_context or ""),
            dev_base,
            remaining,
            add_meta=not truncation_active,
        )

        # Build Responses API typed input items (developer/user/assistant)
        input_items = _messages_to_responses_payload(convo)

        # Stream (default) or non-stream path
        input_tokens = output_tokens = cached_tokens = 0
        use_stream = stream
        # Select model and parameters (allow override for interventions)
        gen_model, reasoning, verbosity = _effective_model_and_params(cfg.openai_model, intervened, personality, cfg.openai_verbosity)
        if use_stream:
            try:
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
                final_text = await send_stream_as_messages(
                    message.channel,
                    deltas,
                    rate_hz=personality.stream_rate_hz,
                    min_first=personality.stream_min_first,
                    min_next=personality.stream_min_next,
                    strip_leading=[f"<@{bot.user.id}>", f"<@!{bot.user.id}>"] if bot.user else None,
                    allowed_mentions=no_pings,
                    max_total_chars=(personality.listen.response_max_chars if intervened else None),
                    send_gate=send_gate,
                )
                # Capture usage if available
                if getattr(deltas, "usage", None):
                    input_tokens, output_tokens, cached_tokens = deltas.usage  # type: ignore
            except Exception as e:
                logger.exception("generate: streaming failed; falling back. error=%s", e)
                use_stream = False

        if not use_stream:
            try:
                logger.info("generate: non-stream model=%s", gen_model)
                final_text, usage = chat_complete_with_usage(
                    api_key=cfg.openai_api_key,
                    model=gen_model,
                    messages=convo,
                    reasoning=reasoning,
                    verbosity=verbosity,
                    truncation=effective_truncation,
                )
                input_tokens, output_tokens, cached_tokens = usage
                # Sanitize leading self-mention; allow user mentions (block roles/everyone)
                final_text = _strip_leading_self_mention(final_text)
                if intervened and personality.listen.response_max_chars:
                    final_text = final_text[: max(0, int(personality.listen.response_max_chars))]
                no_pings = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)
                for chunk in _chunk_message(final_text):
                    try:
                        if limiter is not None and not await send_gate():
                            break
                    except Exception:
                        break
                    await message.channel.send(chunk, allowed_mentions=no_pings)
            except Exception as e2:
                logger.exception("generate: non-stream failed error=%s", e2)
                final_text = i18n.t("generic_error")
                no_pings = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)
                await message.channel.send(final_text, allowed_mentions=no_pings)

        # Optional moderation (persona listen setting)
        if intervened and personality.listen.moderation_enabled:
            from .openai_client import moderate_text

            allowed = moderate_text(cfg.openai_api_key, personality.listen.moderation_model, final_text)
            if not allowed:
                # Skip sending content (already sent if streaming; in that case, this should be disabled or pre-moderated)
                # For simplicity, do nothing extra here.
                pass

        # Update memory after completion
        ctx.turns += 1
        # Persist the sanitized final text in memory for context dumps
        final_text = _strip_leading_self_mention(final_text)
        ctx.messages.append({"role": "assistant", "content": final_text})
        # Mark intervention cooldown if applicable
        if intervened and message.guild:
            gs = store.guild_settings(message.guild.id)
            mark_intervened(gs, message.channel.id, int(getattr(message.author, "id", 0) or 0))

        # Cost tracking (per-bot) and alerts
        try:
            bcur = store.billing_for(getattr(bot.user, "id", 0)) if bot.user else store.billing
            rollover_if_needed(bcur)
            used_model = gen_model if "gen_model" in locals() else cfg.openai_model
            cost = usd_cost(used_model, input_tokens, output_tokens, cached_tokens)
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

    register_commands(bot, store, cfg, i18n, personality, effective_prefix)
    bot.run(cfg.discord_token)
