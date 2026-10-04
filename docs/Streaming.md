# Streaming

Streaming is enabled by default; omit `--no-stream` or explicitly use `--stream`.
The sender keeps Discord's typing indicator active and sends text in small message
bursts, with sentence/paragraph boundaries and pacing. It does not edit one message
for every token. Every message stays within Discord's length limit.

Both the legacy Responses adapter and configured Chat Completions adapter support
text streaming. With `IMAGE_TOOLS_ENABLED=true`, ordinary text still streams;
structured image tool arguments are accumulated privately until the stream ends
with a valid finish reason and `[DONE]`. Only then can one validated image request
enter the normal durable queue. Incomplete/truncated/oversized streams never
submit an image. A configured backend streaming failure has no fallback or second
inference; text already sent may remain visible with an error message.

Persona pacing:

```yaml
streaming:
  rate_hz: 0.8
  min_first: 60
  min_next: 180
```

`rate_hz` controls message pacing. `min_first` and `min_next` are character
thresholds; complete-line boundaries and an early-first-flush timeout can also
trigger a send. Increase `min_next` to reduce bursts for long or code-heavy replies.
Persona rate limits admit one response per incoming event before generation,
across all configured dimensions atomically. Once admitted, the whole reply is
delivered, including length-limit splits and the final tail; streaming bursts do
not consume additional quota. A refused addressed event receives a retry notice;
passive interventions are skipped. Discord transport limits and streaming pacing
still apply. Delivery failures are explicit and do not save unsent text as a
completed reply. Early exits close the backend iterator
and release its connection/lock, without executing an incomplete image tool.

Use `--no-stream` to wait for the complete reply. See [Images](Images.md) for queue,
permission, prompt-publication and recovery behavior.
