# Overview

This project provides a modular, pipx-installable Discord-first LLM chatbot kit that uses OpenAI models for chat, with pluggable personalities defined in YAML. It is designed for simplicity (KISS), separation of concerns, and safe defaults.

Key features
- Installable CLI (`llm-chatbot`) with environment-driven configuration and subcommands (`discord run`)
- Latest OpenAI SDK, Responses API first (default model: `gpt-5-mini`)
- Natural streaming to Discord: typing indicator + human-like message bursts
- Personas in YAML with optional streaming pacing controls
- Per-channel memory persisted to a JSON store under `~/.cache/llm-chatbot-kit/`
- Minimal built-in commands: `~context`, `~reset`, owner-only `~reboot`

When to use this base
- Clone to bootstrap new “characters” (just change the YAML personality).
- Extend with tools, moderation, or analytics without changing core streaming/runtime.
