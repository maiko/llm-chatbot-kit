# Cost Tracking

The bot estimates costs using GPT-5 family pricing and tracks totals per bot.

What is tracked
- Daily and monthly USD totals (per bot)
- Breakdown by model tier and by feature (listen vs mentions/DMs)
- Optional budgets and owner-only controls

Pricing (per 1M tokens)
- GPT-5: input $1.25, cached input $0.125, output $10.00
- GPT-5 mini: input $0.25, cached input $0.025, output $2.00
- GPT-5 nano: input $0.05, cached input $0.005, output $0.40

Commands (owner-only)
- `~cost status`: show today and month totals for this bot
- `~cost limit daily <amount>` / `~cost limit monthly <amount>`: set budgets
- `~cost pause on|off`: pause/resume generation for this bot
- `~cost hardstop on|off`: hard stop behavior when limits are hit

Notes
- For streaming, the final usage is captured from the Responses API and recorded.
- If usage is unavailable, the tracker falls back gracefully.
- Set `DISCORD_OWNER_ID` to enable owner-only controls.
