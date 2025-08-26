# Architecture

This page is a quick tour for developers who want to dive in and extend the bot.

## High-Level Flow

- `cli.py` parses flags, loads `Config` (env) and `Personality` (YAML), then calls `discord_bot.run`.
- `discord_bot.py` wires Discord events and commands to generation:
  - Builds conversation context + environment snippets
  - Uses OpenAI Responses API (via `openai_client.py`) to generate text
  - Streams bursts to Discord using `streaming.py`
  - Tracks costs via `costs.py` and persists state with `memory.py`

## Modules

- `openai_client.py`: Responses-first helpers
  - `_messages_to_responses_payload(messages)`: builds typed items (developer/user/assistant)
  - `_build_responses_kwargs(...)`: consistent kwargs for GPT‑5 (reasoning + text.verbosity)
  - `chat_complete_with_usage(...)`: returns text and usage; Chat Completions is a fallback when needed
- `streaming.py`: human-paced streaming
  - `stream_deltas(...)`: returns an async iterator of text chunks, immediately (true streaming)
  - `send_stream_as_messages(...)`: first burst ASAP, then ~2 lines per burst, with typing indicator
- `runtime_utils.py`: keeps `discord_bot.py` lean
  - `_build_env_context(...)`, `_effective_truncation(...)`, `_effective_model_and_params(...)`, `_maybe_alert_owner(...)`
  - `_chunk_message(...)`: Discord-safe chunking helper
- `listener.py`: passive listening
  - Heuristics gate (allow/deny, cooldowns, triggers), optional judge step
  - `mark_intervened(...)` updates cooldowns after an intervention
- `memory.py`: JSON-backed store
  - Per-channel `ChannelContext`, per-guild settings, and global `Billing`
- `costs.py`: token pricing and budgeting
  - `usd_cost(...)`, daily/monthly rollover, alert thresholds
- `personality.py`: dataclasses + YAML loader
  - Persona drives prompts, environment, streaming pacing, and listen settings

## Key Design Choices

- Responses API first: typed items and GPT‑5 `reasoning`/`text.verbosity` when supported.
- Safe mentions: user mentions allowed; roles/everyone blocked; strips leading self-mention.
- Streaming UX: avoid edits; burst on boundaries; keep typing indicator; fallback gracefully.
- Separation of concerns: helpers moved to `runtime_utils.py` to keep event loop readable.

## Extending

- Tools and function-calling: add new helpers in `openai_client.py` and wire them into `discord_bot.py`.
- More commands: define new `@bot.command` functions in `discord_bot.py` (or split into a `commands.py`).
- Tests: add unit tests under `tests/` — mock OpenAI and Discord objects where needed.

