# Development

Repo layout
- `src/llm_chatbot/`: core package
  - `cli.py`: entrypoint and args
  - `discord_bot.py`: runtime, events, commands
  - `streaming.py`: streaming helpers
  - `openai_client.py`: OpenAI API wrappers
  - `config.py`: env/config + JSON store helpers
  - `memory.py`: per-channel context
  - `personality.py`: dataclass + YAML loader
- `personalities/`: example personas
- `docs/`: this documentation

Common tasks
- Create venv: `python -m venv .venv && source .venv/bin/activate`
- Install dev: `pip install -e .`
- Lint/format: `pip install ruff black && ruff src && black src`
- Run: `llm-chatbot discord run --personality personalities/aelita.yml`

Testing (suggested)
- Use `pytest`; mock OpenAI calls at the module boundary.
- Focus on chunking, streaming pacing, commands, and memory behavior.

CI/CD
- GitHub Actions run on push and PR:
  - Build docs and package (workflows under `.github/workflows/`)
  - Lint with Ruff, check formatting with Black, and run `pytest` on Python 3.9–3.12
  - Artifacts are uploaded for docs and distribution builds

Contributing
- Follow Conventional Commits.
- Keep changes small, focused, and covered by tests where appropriate.
