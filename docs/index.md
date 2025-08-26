# LLM Chatbot Kit

This site documents a modular, pipx-installable Discord-first LLM chatbot kit that uses OpenAI for chat and supports pluggable personalities. It emphasizes simplicity (KISS), separation of concerns, and safe defaults.

Quick links
- Installation: Installation.md
- Configuration: Configuration.md
- Personalities: Personality.md
- Streaming behavior: Streaming.md
- Commands: Commands.md
- Development: Development.md
- Security: Security.md
- Troubleshooting: Troubleshooting.md

Highlights
- Latest OpenAI SDK (Responses API preferred), default model `gpt-5-mini`
- Natural streaming that feels human (typing + bursts, no edits)
- Persona YAML with optional pacing controls
- Per-channel memory backed by a local JSON store

Quick start
1) Install dependencies and CLI
```
pipx install .
```

2) Export tokens
```
export DISCORD_TOKEN=...  
export OPENAI_API_KEY=...
```

3) Run with a personality (Discord)

```
llm-chatbot discord run --personality personalities/aelita.yml
```
