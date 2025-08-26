# Configuration

Set environment variables before running the bot. A `.env.example` file shows the expected variables.

Core
- `DISCORD_TOKEN`: required
- `OPENAI_API_KEY`: required
- `OPENAI_MODEL`: default `gpt-5-mini` (override with `--model`)
- `COMMAND_PREFIX`: default `~`
- `MAX_TURNS`: default `20`
- `DISCORD_OWNER_ID`: optional; required for `~reboot`

Storage
- Context is persisted as JSON at `~/.cache/llm-chatbot-kit/context.json` (or `XDG_CACHE_HOME`). Legacy path is auto-migrated on first run.
- To reset all memory, delete this file or use `~reboot` (owner only).

Discord setup
- Enable “Message Content Intent” in the Developer Portal.
- Invite your bot with appropriate permissions (send messages, read history).
