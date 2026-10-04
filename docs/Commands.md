# Commands

Image-only and combined local operation: see [Local Images and Text](Images.md). Administrative commands require a configured `DISCORD_OWNER_ID`; unset denies access.

Prefix: configurable via `COMMAND_PREFIX` (default `~`).

Available commands
- `~context`: prints the last messages for this channel (trimmed)
- `~reset`: clears the memory for this channel
- `~reboot`: clears all memory (owner-only; requires `DISCORD_OWNER_ID`)
- `~listen on|off|status|ban|unban`
- `~emoji list`
- `~truncation status|set <auto|disabled>`
- Cost (owner-only):
  - `~cost status`
  - `~cost limit daily <amount>` / `~cost limit monthly <amount>`
  - `~cost pause on|off`
  - `~cost hardstop on|off`

Listening `on`/`off` commands are owner-only and persist an explicit guild override
of the persona's `listen.enabled` default. `listen status` reports the effective
setting. The override survives bot restarts; a later `listen on` enables it again.

Mentions and DMs
- The bot replies in DMs and when mentioned in guild channels.

Message limits
- Discord limits messages to ~2000 characters; the bot auto-chunks.

Anti-spam rate limiting
- Reply admission is limited per channel, DM peer, triggering user, and globally. Limits are configurable under `rate_limit` in the personality YAML. One incoming event consumes one reservation across all dimensions; an admitted reply is always allowed to finish, regardless of its number of streaming bursts or Discord length-limit chunks. Image generation retains its separate queue quotas.
