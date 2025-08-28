<p align="center">
  <img src="docs/assets/logo.svg" alt="LLM Chatbot Kit" width="560" />
</p>

<p align="center">
  <b>Developer‑first kit for LLM chatbots</b><br/>
  Streaming • Memory • Personas (Discord‑first)
</p>

# LLM Chatbot Kit

![Build Docs](https://github.com/maiko/llm-chatbot-kit/actions/workflows/docs.yml/badge.svg?branch=main)
![Build Package](https://github.com/maiko/llm-chatbot-kit/actions/workflows/package.yml/badge.svg?branch=main)
![Python](https://img.shields.io/badge/Python-3.9%2B-blue)

Install (pipx recommended)

- pipx: `pipx install .` (from repo root) or `pipx install git+https://github.com/maiko/llm-chatbot-kit@main`
- Run (Discord): `llm-chatbot discord run --personality personalities/aelita.yml`
  - Default behavior streams replies as natural message bursts (no edits), with typing indicator.
  - Disable streaming: `llm-chatbot discord run --no-stream` (or `--stream=false`).

Demo personalities (Code Lyoko)

- Aelita (ally): `llm-chatbot discord run --personality personalities/aelita.yml`
- XANA (antagonist): `llm-chatbot discord run --personality personalities/xana.yml`

Configuration (env)

- `DISCORD_TOKEN`: Discord bot token
- `OPENAI_API_KEY`: OpenAI key
- `OPENAI_MODEL` (optional): defaults to `gpt-5-mini`.
- `OPENAI_VERBOSITY` (optional): `low` (default), `medium`, or `high` (GPT‑5 only; not for `gpt-5-chat-latest`).
- `DISCORD_OWNER_ID` (optional): User ID for `reboot`
- `COMMAND_PREFIX` (default `~`), `MAX_TURNS` (default `20`)
- `CONTEXT_STORE_PATH` (optional): override default JSON context path.

Run multiple personas

- Use one process per bot (each with its own `DISCORD_TOKEN`).
- Aelita: `llm-chatbot discord run --personality personalities/aelita.yml` (prefix `!`)
- XANA: `llm-chatbot discord run --personality personalities/xana.yml` (prefix `^`)

Models & generation

- Generation model: GPT‑5 mini by default (`OPENAI_MODEL=gpt-5-mini`).
- Judge/classifier (listening): GPT‑5 nano.
- GPT‑5 features used (except `gpt-5-chat-latest`):
  - `reasoning: { effort: "minimal" }` for faster time-to-first-token.
### Triggers and Context (new)

In addition to DMs and mentions, you can enable explicit word triggers and control the context window:

```yaml
triggers:
  enabled: true
  on_mention: true
  words: ["assistant", "help"]
  use_regex: false

context:
  include_last_n: 12
  include_non_addressed_messages: true
```

- Word triggers match case-insensitive substrings by default; set `use_regex: true` to use regex patterns.
- Context includes the last N messages (cap 50). If `include_non_addressed_messages: false`, only user messages that targeted the bot (mention/word trigger) are included; assistant messages are always included.

  - `text: { format: { type: "text" }, verbosity: <OPENAI_VERBOSITY> }` (default `low`).
- Requests use the Responses API with typed items (developer/user/assistant). Chat Completions is kept only as a fallback.

Create a new personality

- Duplicate `personalities/aelita.yml`, adjust `name`, `developer_prompt`, `system_prompt`.
- Optional pacing in YAML:
  ```yaml
  streaming:
    rate_hz: 1.0
    min_first: 80
    min_next: 120
  ```
- Optional environment & listening:
  ```yaml
  command_prefix: "!"
  environment:
    include_emojis: true
    emojis_limit: 50
    include_online_members: true
    online_limit: 50
  listen:
    enabled: false
    judge_enabled: true
    judge_model: gpt-5-nano
  truncation: auto  # hide meta and let API auto-truncate context
  ```
- Run with `--personality path/to/your.yml`.

Run in Docker

Official image: `ghcr.io/maiko/llm-chatbot-kit:latest`

Docker run

```bash
docker run --rm \
  -e DISCORD_TOKEN=... \
  -e OPENAI_API_KEY=... \
  -e OPENAI_MODEL=gpt-5-mini \
  -v $(pwd)/data:/data \
  ghcr.io/maiko/llm-chatbot-kit:latest \
  llm-chatbot discord run --personality personalities/aelita.yml
```

Compose example

See `examples/docker-compose.yml` and `examples/.env.example`:
```yaml
services:
  bot:
    image: ghcr.io/maiko/llm-chatbot-kit:latest
    restart: unless-stopped
    env_file:
      - .env
    environment:
      OPENAI_MODEL: gpt-5-mini
    volumes:
      - ./data:/data
    command: ["llm-chatbot", "discord", "run", "--personality", "personalities/aelita.yml"]
```

Notes

- Runs as non‑root (uid 1000). Data and cache are stored under `/data` (mapped via `XDG_CACHE_HOME=/data`), so bind-mount `./data:/data` to persist context.
- Required env: `DISCORD_TOKEN`, `OPENAI_API_KEY`. Optionals: `OPENAI_MODEL`, `OPENAI_VERBOSITY`, `DISCORD_OWNER_ID`, `COMMAND_PREFIX`, `MAX_TURNS`, `PERSONALITY_FILE`.
- To use a custom personality, mount it and set `PERSONALITY_FILE` (e.g., mount to `/config/custom.yml` and run with `--personality /config/custom.yml`).
Notes

- Messages are stored in a JSON file under `~/.cache/llm-chatbot-kit/context.json` (or `CONTEXT_STORE_PATH`). On first run, the kit migrates an existing `~/.cache/discord-llm-bot/context.json` automatically.
- Keep replies under Discord’s 2000-char limit; bot auto-chunks.
- Streaming is default; on failure it falls back to non-streaming.
- Enable intents in the Developer Portal: Message Content (required) and Presence (for online members).
- Mentions: The bot allows user mentions but blocks roles/everyone. It adds a reminder to the developer prompt not to mention itself and strips leading self-mentions.
- Environment context in guilds includes a “Membres visibles” list with member names and IDs for correct mentions, and an optional online members list by name only.

Commands (per persona prefix)

- Memory: `<prefix>context`, `<prefix>reset`, `<prefix>reboot` (owner)
- Listening: `<prefix>listen on|off|status|ban|unban`
- Costs: `<prefix>cost status|limit daily <amt>|limit monthly <amt>|pause on|off|hardstop on|off` (owner-only)
- Emojis: `<prefix>emoji list`
- Truncation: `<prefix>truncation status|set <auto|disabled>`

Anti-spam rate limiting

- Outbound send limits are enforced to prevent loops and spam. Dimensions:
  - channel: per guild channel
  - dm_user: per DM peer
  - trigger_user: user who triggered in a public channel
  - global: bot-wide
- Configure in personality YAML with safe defaults:
  ```yaml
  rate_limit:
    channel: [{ window: 10, max: 3 }, { window: 60, max: 10 }]
    dm_user: [{ window: 10, max: 2 }, { window: 60, max: 6 }]
    trigger_user: [{ window: 30, max: 3 }]
    global: [{ window: 60, max: 20 }]
  ```
- The limiter gates actual sends, including streaming bursts; when exceeded, sending silently stops to break loops.

Per-bot cost tracking

- Costs are isolated per running bot (keyed by Discord bot user ID).
- Owner-only cost controls:
  - `~cost status`
  - `~cost limit daily <amount>`, `~cost limit monthly <amount>`
  - `~cost pause on|off` and `~cost hardstop on|off`

Planned

- Multi-platform supervisor: run the same persona across Discord and other platforms concurrently via `llm-chatbot multi run --platforms discord,slack`. See issue #3.
- Slack integration: first-class adapter and `slack run` subcommand. See issue #2.
- Logging improvements: verbosity, JSON logs, redaction, usage summaries, per-module levels, OpenAI tracing. See issue #4.
- Docker packaging: official image and compose example. See issue #5.

Documentation

- Overview: `docs/Overview.md`
- Installation: `docs/Installation.md`
- Configuration: `docs/Configuration.md`
- Personalities: `docs/Personality.md`
- Streaming details: `docs/Streaming.md`
- Listening (Experimental): `docs/Listening.md`
- Costs: `docs/Costs.md`
- Commands: `docs/Commands.md`
- Packaging (CI builds): `docs/Packaging.md`
- Development: `docs/Development.md`
- Security: `docs/Security.md`
- Troubleshooting: `docs/Troubleshooting.md`

Quick tips

- Set `DISCORD_OWNER_ID` to receive DM alerts on spend thresholds and to unlock owner-only commands.
- If running via pipx, use `pipx upgrade llm-chatbot-kit` (or reinstall) after pulling updates.
