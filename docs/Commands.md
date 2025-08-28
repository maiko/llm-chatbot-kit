# Commands

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

Mentions and DMs
- The bot replies in DMs and when mentioned in guild channels.

Message limits
- Discord limits messages to ~2000 characters; the bot auto-chunks.

Anti-spam rate limiting
- Outbound send rate limiting is enforced per channel, DM peer, triggering user, and globally. Limits are configurable in the personality YAML under `rate_limit` and have safe defaults.

