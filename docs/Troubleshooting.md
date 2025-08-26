# Troubleshooting

Bot won’t start
- Ensure `DISCORD_TOKEN` and `OPENAI_API_KEY` are set.
- Check that the bot has Message Content Intent enabled.

Bot is silent in a server
- Mention the bot or DM it; by default it only replies in DMs or when mentioned.
- Verify it has permission to read/send in the channel.

Rate limited (429)
- The streamer includes a basic backoff. Reduce `streaming.rate_hz` if hitting limits.

Long responses are cut
- Discord caps messages at ~2000 characters; the bot auto-chunks.
- If streaming, increase `streaming.min_next` for fewer, larger bursts.

OpenAI errors
- Verify model name (default `gpt-5`) and API key validity.
- Temporary outages will fall back to non-streaming if possible.

Where is memory stored?
- `~/.cache/llm-chatbot-kit/context.json` (or `XDG_CACHE_HOME`). Use `~reset`/`~reboot` to clear.
