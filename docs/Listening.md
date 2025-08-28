# Listening vs Triggers

"Listening" is the optional, heuristic/judge-driven intervention path. It remains unchanged.

"Triggers" are an explicit, config-driven generation path:
- DM and/or mention (configurable via `triggers.on_mention`)
- Word triggers (substring or regex) in any message (`triggers.enabled`, `triggers.words`, `triggers.use_regex`)

If no trigger fires, listen heuristics may still intervene when enabled. These features are independent.

# Listening (Experimental)

Enable the bot to passively "listen" in servers and occasionally join conversations when it adds value or a good joke.

Behavior
- Heuristics and optional judge decide if the bot should intervene.
- Cooldowns and channel allow/deny guards avoid spam.
- Replies stream as natural bursts; tone follows the persona.

Persona YAML (`listen`)
- `enabled`: true|false
- `allow_channels`, `deny_channels`: lists of IDs or names
- `cooldown_channel_seconds`, `cooldown_user_seconds`
- `min_len`, `trigger_keywords`
- `judge_enabled`, `judge_model` (default `gpt-5-nano`), `judge_threshold`, `judge_max_context_messages`
- `generation_model_override` (else default model), `response_max_chars`, `joke_bias`
- `cost_daily_usd`, `cost_monthly_usd` (optional persona-level hints)
- `moderation_enabled` (default false), `moderation_model` (default `omni-moderation-latest`)

Commands (prefix respects persona)
- `~listen on` / `~listen off`: toggle listening for this guild (persisted)
- `~listen status`: show current state and banned channels
- `~listen ban #channel` / `~listen unban #channel`: update bans (persisted)

Notes
- The judge uses a small GPT-5 model by default to keep costs low.
- Moderation is optional and off by default.

