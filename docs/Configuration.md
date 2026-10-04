# Configuration

Image-only and combined local operation: see [Local Images and Text](Images.md). Administrative commands require a configured `DISCORD_OWNER_ID`; unset denies access.

Set environment variables before running the bot. A `.env.example` file shows the expected variables.

Core
- `DISCORD_TOKEN`: required
- `TEXT_QUEUE_LIMIT`: pending addressed replies (default 20, range 1–100), plus one active reply. 📝 indicates queued/active requests; see [Images](Images.md#addressed-reply-queue).
- `OPENAI_API_KEY`: required for the legacy cloud chat backend; configured text exclusively uses `TEXT_API_KEY`, image-only needs neither. Keys never fall back across backends. A configured text endpoint requires its own non-empty key even if an OpenAI key is present.
- `OPENAI_MODEL`: default `gpt-5-mini` (override with `--model`)
- `COMMAND_PREFIX`: default `~`
- `MAX_TURNS`: default `20`
- `DISCORD_OWNER_ID`: required for administrative commands (reboot, listening changes, cost controls, truncation changes); unset denies access

Storage
- Context is persisted as JSON at `~/.cache/llm-chatbot-kit/context.json` (or `XDG_CACHE_HOME`). Legacy path is auto-migrated on first run.
- To reset all memory, delete this file or use `~reboot` (owner only).

Discord setup
- Enable “Message Content Intent” in the Developer Portal.
- Invite your bot with appropriate permissions (send messages, read history).
