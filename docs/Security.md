# Security

Secrets
- Never commit secrets. Use environment variables (`DISCORD_TOKEN`, `OPENAI_API_KEY`).
- Rotate tokens if they leak. Revoke and reissue in Discord/OpenAI portals.

Discord permissions
- Grant only what is necessary (send messages, read history). Avoid admin scopes.
- Enable Message Content Intent only if required.

Operational hygiene
- Pin dependencies when packaging releases.
- Monitor for abuse; add moderation layers if running publicly.

Data retention
- Per-channel memory is stored locally under `~/.cache/llm-chatbot-kit/`.
- Decide on retention periods; `~reset`/`~reboot` clear memory.
