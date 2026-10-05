# Configuration

Image-only and combined local operation: see [Local Images and Text](Images.md). Administrative commands require a configured `DISCORD_OWNER_ID`; unset denies access.

Set environment variables before running the bot. A `.env.example` file shows the expected variables.

Core
- `DISCORD_TOKEN`: required
- `TEXT_MAX_TOKENS`: configured Chat Completions output budget (default 1024, range 1–16384).
- `TEXT_TOOL_MAX_TOKENS`: output budget when tools are offered, including arguments (default 4096, range 1–16384).
- `TEXT_TIMEOUT_SECONDS`: configured text backend read timeout (default 300, range 30–600); connect timeout remains 10 seconds. Larger budgets permit longer inference, so match the timeout to backend speed.
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

Known custom emoji shortcodes in non-streamed generated replies are resolved to the current
guild's actual static/animated Discord codes before sending. ID-bearing tokens are
repaired using a known guild ID first, then an exact unambiguous guild name if the
ID is invalid (for example, truncated). Unknown or ambiguous names stay unchanged;
code spans and valid Discord codes are preserved.


## Trusted bot conversations

Bot-authored messages remain ignored by default. To let selected bots converse,
configure each instance with:

- `TEXT_BOT_CHAT_ENABLED=true` (default false).
- `TEXT_BOT_CHAT_PEER_IDS`: comma-separated trusted bot user IDs, required when enabled.
- `TEXT_BOT_CHAT_MAX_REPLIES`: automatic reply events per bot/channel, default3,
  range1–10. The counter persists through restart; a human explicitly mentioning
  this bot or a trusted peer resets the counter for that channel.

Only a direct user mention of this bot from a listed peer is accepted in a guild.
Self messages, unlisted bots, webhooks and peer commands are ignored. Existing
text guild/channel/role permissions and queue limits still apply. Peer turns
are text only: they never fetch photos or execute image tools. Each bot has its
own budget, including unsuccessful reply events; no automatic timed reset.
Passive listening/word triggers do not activate peer conversations. Personas
and command prefixes need no changes. Set the option false to disable it.
