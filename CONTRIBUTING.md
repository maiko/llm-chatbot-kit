# Contributing

Thanks for your interest in improving this project! This guide explains how to set up your environment, follow the coding style, write tests, and submit high‑quality changes.

## Getting Started

- Python 3.9+ (3.11+ recommended)
- Create a virtualenv: `python -m venv .venv && source .venv/bin/activate`
- Install in editable mode: `pip install -e .`
- Optional tooling: `pip install ruff black pytest pre-commit`

Useful links
- Architecture: `docs/Architecture.md`
- Development: `docs/Development.md`
- Commands, Streaming, Listening: see `docs/`

## Project Structure (overview)

- `src/llm_chatbot/`: core package
  - `cli.py`: CLI entrypoint (`llm-chatbot`)
  - `discord_bot.py`: runtime (events, streaming, listening, cost tracking)
  - `commands.py`: all bot commands (context, listen, cost, emoji, truncation)
  - `runtime_utils.py`: small helpers (env context, truncation/model params, alerts, chunking)
  - `openai_client.py`: Responses API wrappers + payload/usage helpers
  - `streaming.py`: true streaming + human-like bursts
  - `config.py`: env config + JSON helpers
  - `memory.py`: channel contexts, guild settings, billing
  - `personality.py`: dataclasses + YAML loader
  - `listener.py`: listening heuristics + cooldown bookkeeping
  - `locales/`: packaged i18n strings
- `personalities/`: example personas (YAML)
- `docs/`: developer and user documentation
- `tests/`: unit tests (pytest)

## Coding Style

- Language: Python 3.9+
- Style: PEP 8, 4‑space indentation
- Naming: functions/vars `snake_case`, classes `CapWords`, constants `UPPER_SNAKE_CASE`
- Keep modules focused and readable (KISS). Prefer explicit code over cleverness.
- Add short, descriptive docstrings for modules and functions. Keep inline comments useful and sparing.
- Imports are explicit and localize heavy imports when possible (e.g., SDKs inside functions if rarely used).

Formatting & linting
- Ruff: `ruff src` (CI enforces)
- Black: `black src tests` (CI checks with `--check`)
 - Pre-commit hooks (optional):
   - Install: `pre-commit install`
   - Run on all files: `pre-commit run --all-files`

## Testing

- Framework: `pytest`
- Location: `tests/` with filenames `test_*.py`
- Scope: prioritize unit tests for helpers and logic (e.g., payload building, chunking, heuristics, config resolution)
- OpenAI/Discord calls: mock at module boundaries; do not hit network
- Run: `pytest -q`

Suggested targets
- `_messages_to_responses_payload`, `extract_usage` (OpenAI wrappers)
- `send_stream_as_messages` burst boundaries (line/sentence splitting)
- `_effective_model_and_params`, `_effective_truncation` (runtime utils)
- `should_intervene` and cooldown bookkeeping
- `_chunk_message` (Discord chunking)

## Security & Configuration

- Do not commit secrets. Use env vars: `DISCORD_TOKEN`, `OPENAI_API_KEY`, `DISCORD_OWNER_ID` (optional)
- See `.env.example` for expected variables
- Discord Developer Portal: enable Message Content Intent and, if needed, Presence Intent
- Storage: context JSON at `~/.cache/llm-chatbot-kit/context.json` by default (auto-migrates legacy path)

## Commits & Branching

- Keep commits small and focused; write clear messages
- Conventional Commits encouraged:
  - `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `ci:`, `perf:`
- Create a feature branch from the target base (e.g., `feat/listen-user-cooldown`)
- Rebase over merge when updating your branch (keeps history clean)

## Pull Requests

- Provide a concise description and motivation
- Link any related issues
- Include steps to validate (commands, screenshots/logs if UI/behavior changed)
- Ensure:
  - Tests pass locally (`pytest -q`)
  - Ruff and Black are clean (`ruff src` and `black --check src tests`)
  - Docs are updated when behavior/flags change (e.g., `docs/`, `.env.example`)

## CI/CD

- GitHub Actions run on pushes and PRs:
  - Lint (Ruff), format check (Black), tests (pytest) on Python 3.9–3.12
  - Docs and package build workflows upload artifacts
- Failing checks block merges; please fix lint/test before requesting review

## Releasing (maintainers)

- Package artifacts built by `.github/workflows/package.yml`
- Tag a release (`vX.Y.Z`) to trigger release workflow
- Optional PyPI publish if `PYPI_API_TOKEN` is configured

## Adding/Updating Personalities

- Place persona YAMLs under `personalities/`
- Required fields: `name`, `system_prompt`; optional `developer_prompt`, `language`
- Environment, streaming, and `listen` controls can be set in YAML
- Keep persona prompts concise and consistent with project tone

## Scope & Non-Goals

- Don’t introduce unrelated changes in a single PR
- Don’t commit tooling configs unless used by CI or the project build
- Avoid adding dependencies unless there’s clear value and consensus

Thanks again for contributing! If you’re unsure about anything, open a small draft PR early or ask a question in the issue/PR thread.
