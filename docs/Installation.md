# Installation

Prerequisites
- Python 3.9+ (recommended 3.11+)
- A Discord application with a bot token and Message Content Intent enabled
- An OpenAI API key

Install with pipx (recommended)
- pipx install .   # from the repo root
- Run: llm-chatbot discord run --personality personalities/aelita.yml

Install with pip (editable dev)
- python -m venv .venv && source .venv/bin/activate
- pip install -e .
- llm-chatbot discord run --personality personalities/aelita.yml

Environment variables
- DISCORD_TOKEN: your Discord bot token (required)
- OPENAI_API_KEY: your OpenAI API key (required)
- OPENAI_MODEL: defaults to gpt-5-mini; override if needed or pass --model
- DISCORD_OWNER_ID: optional; allows ~reboot in servers where you’re the owner
- COMMAND_PREFIX: default ~
- MAX_TURNS: default 20
Docker
- See the “Run in Docker” section in the README for the official image and examples.
