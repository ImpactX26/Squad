Turn a support agent's ticket search into filters. Use only these values, and leave out anything they didn't ask for:

- status: new, in_progress, awaiting_customer, awaiting_payment, scheduled, resolved, closed
- open_only: true when they want open, active, pending or unresolved tickets
- priority: low, medium, high, urgent
- issue_type: battery, charging, display, keyboard, audio, overheating, boot, os, driver, performance, connectivity, other
- category: hardware, software
- source_channel: discord, telegram, email, web (website chat is web, mail is email)
- created_within_days: how many days back, when they say today (1), yesterday (2), this week (7), this month (30)
- min_duplicate_count: 1 when they want tickets the customer chased, followed up on, or that got escalated
- text: the words left that describe the problem or the customer, for matching by meaning; empty when nothing is left