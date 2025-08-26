# Streaming

The bot streams replies by default to feel more human. It maintains the typing indicator and sends natural “bursts” rather than editing messages.

How it works
- Uses OpenAI Responses streaming to receive text deltas
- Buffers tokens and splits on sentence/paragraph boundaries
- Sends the first burst ASAP, then roughly every 2 lines, with ~1 msg/sec pacing and slight jitter
- Respects Discord’s 2000 character limit per message
- Falls back to non-streaming if an error occurs

Tuning
- Persona YAML `streaming:` controls the pacing:
  - `rate_hz`: messages/s
  - `min_first`: characters before the first send
  - `min_next`: characters before subsequent sends

Disabling streaming
- CLI: `--no-stream` (or `--stream=false` on Python 3.9+)

Notes
- Code blocks: for heavy code output, consider raising `min_next`.
- Rate limits: basic 429 backoff is built in.
