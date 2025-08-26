# Cost Tracking

The bot estimates costs using GPT-5 family pricing and tracks totals globally.

What is tracked
- Daily and monthly USD totals
- Breakdown by model tier and by feature (listen vs mentions/DMs)
- Optional budgets and alerts (planned)

Pricing (per 1M tokens)
- GPT-5: input $1.25, cached input $0.125, output $10.00
- GPT-5 mini: input $0.25, cached input $0.025, output $2.00
- GPT-5 nano: input $0.05, cached input $0.005, output $0.40

Commands
- `~cost status`: shows today and this month totals.
- `~cost budget daily <amount>` / `~cost budget monthly <amount>`: set budgets
- `~cost hardstop on|off`: stop interventions when budgets are hit

Notes
- For streaming, the final usage is captured from the Responses API and recorded.
- If usage is unavailable, the tracker falls back gracefully.
- Budget thresholds can DM the owner (set `DISCORD_OWNER_ID`).
