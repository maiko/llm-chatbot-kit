# Repository Guidelines

## Project Structure & Modules
- `src/llm_chatbot/`: bot package (installable via pipx)
  - `cli.py`: CLI (`llm-chatbot`) and arg parsing (subcommands; `discord run`)
  - `discord_bot.py`: runtime (events, commands, streaming bursts, listening, truncation)
  - `openai_client.py`: unified Responses API helpers and payload builders
    - `_messages_to_responses_payload(messages)` → typed items: developer/user/assistant, input_text/output_text
    - `_build_responses_kwargs(model, items, reasoning, verbosity, truncation)` → consistent kwargs for Responses
    - `chat_complete[_with_usage]` and `judge_intervention` both use the same path
  - `streaming.py`: human-like streaming sender; uses Responses.stream and respects text.verbosity, reasoning, truncation
  - `config.py`: env‑driven settings and JSON store helpers
  - `memory.py`: per‑channel context + per‑guild settings + billing persistence
  - `personality.py`: personality dataclass + YAML loader (prefix, env templates, listening, truncation)
  - `locales/`: packaged i18n strings (en, fr)
- `personalities/`: sample personas (`marv.yml`, `shaark.yml`)

## Build, Test, and Dev Commands
- Create venv: `python -m venv .venv && source .venv/bin/activate`
- Install locally: `pip install -e .` (or `pipx install .`)
- Run: `llm-chatbot discord run --personality personalities/marv.yml`
- Lint (optional): `pip install ruff black && ruff src && black src`

## Coding Style & Naming Conventions
- Python 3.9+, PEP 8, 4‑space indentation
- Names: functions/vars `snake_case`, classes `CapWords`, constants `UPPER_SNAKE_CASE`
- Keep modules focused (KISS, SoC). Prefer explicit over clever.

## Testing Guidelines
- Framework: pytest (suggested). Place tests in `tests/` as `test_*.py`.
- Mock OpenAI calls; unit test message chunking, commands, and memory.
- Run: `pip install pytest && pytest -q`

## Commit & Pull Request Guidelines
- Commits: concise, imperative, one logical change; Conventional Commits encouraged (`feat:`, `fix:`, `chore:`)
- PRs: clear description, linked issues, steps to validate; include screenshots/logs if UI/behavior changes

## Security & Configuration Tips
- Never commit secrets. Use env vars: `DISCORD_TOKEN`, `OPENAI_API_KEY`, `DISCORD_OWNER_ID` (optional)
- Discord: enable `MESSAGE CONTENT INTENT` (required) and `PRESENCE INTENT` (for online members) in Developer Portal
- Context store: defaults to `~/.cache/llm-chatbot-kit/context.json` (auto-migrates legacy); rotate/clean as needed

## Agent‑Specific Instructions
- Personalities (YAML): define `system_prompt`, `developer_prompt`, and:
  - `command_prefix`, `language`
  - `environment`: `include_emojis`, `emojis_limit`, `include_online_members`, `online_limit`
  - `listen`: `enabled`, `judge_enabled`, `judge_model` (nano), thresholds, cooldowns, etc.
  - `truncation`: `auto` to enable API-side context auto-truncation (hides meta)
  - Optional `streaming` pacing knobs
- Generation & judge models:
  - Generation default: `gpt-5-mini`
  - Judge/classifier default: `gpt-5-nano` (listening)
- GPT‑5 parameters (all except `gpt-5-chat-latest`):
  - `reasoning: { effort: "minimal" }`
  - `text: { format: { type: "text" }, verbosity: <low|medium|high> }` (default low via `OPENAI_VERBOSITY`)
- Conversation building:
  - We merge system + developer into a single developer item for the Responses API
  - If truncation is `auto`, we omit the `[meta] N message(s) remaining …` line
  - Guild env includes a “Membres visibles” list with “DisplayName (ID: …)” and an optional “Membres en ligne” list by name only
- Anti self‑mention:
  - Appends a dynamic developer reminder: “<@BOT_ID> is your own ID, do not mention yourself” (localized)
  - We also disallow role/everyone mentions; user mentions are allowed
- Listening mode details:
  - Heuristics gate + judge (nano) with minimal reasoning and low verbosity
  - Judge context: last 10 channel messages with timestamps and roles
- Commands (prefix per persona):
  - Memory: `<prefix>context`, `<prefix>reset`, `<prefix>reboot`
  - Listening: `<prefix>listen on|off|status|ban|unban`
  - Costs: `<prefix>cost status|budget daily|budget monthly|hardstop on|off`
  - Emojis: `<prefix>emoji list`
  - Truncation: `<prefix>truncation status|set <auto|disabled>`
