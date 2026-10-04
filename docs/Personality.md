# Personality: Triggers and Context

## Triggers (mention and word triggers)
Add a dedicated triggers block to control when the bot generates (separate from listen mode):
```yaml
triggers:
  enabled: true
  on_mention: true
  words: ["assistant", "help"]   # case-insensitive substring by default
  use_regex: false               # when true, treat words entries as regex patterns
```
- DM always triggers.
- Mention triggers when `on_mention: true` (default).
- Word triggers fire when any pattern matches a message, even without a mention.

## Context (last-N messages)
Control how many recent messages are included in the model input:
```yaml
context:
  include_last_n: 12
  include_non_addressed_messages: true
```
- `include_last_n`: number of recent messages to include (hard-capped at 50).
- `include_non_addressed_messages`: when false, user messages are included only if they targeted the bot (mention or word trigger). Assistant messages are always included.
- Permitted human messages are recorded even when listening is off, the judge declines to answer, or the backend is busy. Recording does not trigger a reply or consume a conversation turn. Bot/webhook messages, prefix commands and messages outside configured access rules are excluded. Each channel retains up to 100 entries, and each reply uses its configured last-N input snapshot. This records events received while the bot runs; it does not fetch older Discord history. `reset` still clears the stored context.

# Personalities

Define personas in YAML files and pass them via `--personality path.yml`.

Schema
- `name`: string identifier
- `language`: optional; selects locale (`en`/`fr`) for built-in messages
- `developer_prompt`: optional; prepended to the system prompt
- `system_prompt`: required; the core behavior
- `command_prefix`: optional; overrides global `COMMAND_PREFIX`
- `streaming` (optional): pacing controls
  - `rate_hz`: messages per second (default 1.0)
  - `min_first`: minimum chars before first burst (default 80)
  - `min_next`: minimum chars before subsequent bursts (default 120)
- `environment` (optional): context templates injected per message
  - `guild_template`: text appended when chatting in a server
  - `dm_template`: text appended when chatting in DMs
  - Placeholders: `{guild_name}`, `{channel_name}`, `{member_names}`, `{user_display_name}`
 - `messages` (optional): override built-in i18n strings to match persona tone
   - Keys: `limit_reached`, `no_history`, `context_cleared`, `all_contexts_cleared`, `owner_only`, `generic_error`, `participants_visible`

Example
```yaml
name: example
language: en
developer_prompt: |
  Stay in character. Be concise, helpful, and safe. Keep replies under 1900 characters.
system_prompt: |
  You are a calm, friendly assistant. Provide clear, copy‑paste‑ready answers with brief
  explanations when useful. Ask a short clarifying question if requirements are ambiguous.
streaming:
  rate_hz: 1.0
  min_first: 80
  min_next: 120
command_prefix: "~"
environment:
  guild_template: |
    \nYou are speaking in a public Discord server. Visible members:\n{member_names}
  dm_template: |
    \nYou are in a direct message on Discord (DM).
messages:
  limit_reached: "Conversation limit of {max_turns} reached. Use '{prefix}reset' to continue."
  no_history: "No history for this channel."
  context_cleared: "Context cleared for this channel."
  all_contexts_cleared: "All contexts have been cleared."
  owner_only: "Owner-only action."
  generic_error: "Sorry, an error occurred while generating the reply."
  participants_visible: "Visible participants: {names}"
```

Guidelines
- Keep system prompts concise and specific; avoid revealing internal rules.
- Respect Discord constraints: keep messages short; the bot auto-chunks when needed.
- For code-heavy personas, increase `min_next` to avoid breaking blocks mid-stream.

## Custom server emojis

```yaml
environment:
  include_emojis: true
  emojis_limit: 200
```

The optional inventory supplies exact static `<:name:id>` and animated
`<a:name:id>` codes to each guild reply. Choose a limit covering the server's
inventory if the full list is needed; the prompt reports included/total counts.
The model receives names and codes, not the emoji image pixels or usage meanings.
Instructions require full codes without backticks when rendering an emote and
forbid invented IDs or visual descriptions. Use `<prefix>emoji list` without a
mention for the complete deterministic code listing, independently of the prompt
limit. Startup logs record inventory counts, not emoji names or conversation text.
