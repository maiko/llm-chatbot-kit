Logging

Phase 1
- Verbosity: -v/--verbose, -q/--quiet, --log-level
- Formats: --log-format text|json (default text)
- Secret redaction: always on
- Consistent module loggers

Phase 2
- Per-module levels (exact matching)
  - --log-levels module=LEVEL[,module=LEVEL…]
  - Env: LLM_LOG_LEVELS="llm_chatbot.openai_client=DEBUG,llm_chatbot.streaming=WARNING"
  - Matching is exact. No prefix/glob expansion.
- OpenAI tracing
  - --trace-openai off|meta|full (default meta; env LLM_TRACE_OPENAI)
  - meta logs model, path, latency_ms, usage (input/output/cached), request_id
  - full additionally logs inputs/outputs with redaction applied
  - Text format example:
    - trace-openai: path=responses.create model=gpt-5-mini latency_ms=123 input=42 output=17 cached=0 req_id=req_123
  - JSON format example (extra.trace):
    - {"trace":{"type":"openai","mode":"meta","path":"responses.create","phase":"chat","model":"gpt-5-mini","latency_ms":123,"request_id":"req_123","usage":{"input":42,"output":17,"cached":0}}}
# Logging

Phase 1 introduces safer, more ergonomic logging.

- Verbosity: -v/--verbose (stackable), -q/--quiet, --log-level LEVEL. Precedence: --log-level > flags > env.
- Format: --log-format text|json (default text). Env mirrors: LLM_LOG_FORMAT, LLM_LOG_LEVEL (CLI wins).
- JSON keys are stable: ts, level, logger, msg, extra.
- Redaction: values of OPENAI_API_KEY, DISCORD_TOKEN, and SLACK_* are redacted. Potential secret-like strings are masked. Request/response bodies are not logged by default.
- Usage summaries: one INFO line per assistant turn with fields:
  usage model=&lt;m&gt; input=&lt;it&gt; output=&lt;ot&gt; cached=&lt;cit&gt; cost=$&lt;c&gt; feature=&lt;listen|mention_or_dm&gt; channel=&lt;id&gt; guild=&lt;id&gt;.

Examples:
- llm-chatbot -v
- llm-chatbot --log-format=json -q
- LLM_LOG_LEVEL=DEBUG LLM_LOG_FORMAT=json llm-chatbot discord run
