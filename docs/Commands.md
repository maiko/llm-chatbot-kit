# Commands

Prefix: configurable via `COMMAND_PREFIX` (default `~`).

Available commands
- `~context`: prints the last messages for this channel (trimmed)
- `~reset`: clears the memory for this channel
- `~reboot`: clears all memory (owner-only; requires `DISCORD_OWNER_ID`)

Mentions and DMs
- The bot replies in DMs and when mentioned in guild channels.

Message limits
- Discord limits messages to ~2000 characters; the bot auto-chunks.

