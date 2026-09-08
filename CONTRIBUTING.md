# Contributing

Thanks for your interest in improving this project! Contributions are welcome — bug reports, feature ideas, and pull requests alike.

## Reporting bugs

Open an [Issue](../../issues) and include:

1. What you expected vs. what happened
2. The relevant log line or verification output (`❌` / heartbeat lines are designed to pinpoint failures)
3. Your setup: OS, Python version, delivery channel (`whatsapp` / `telegram` / `webhook` / `none`)

**Never paste real credentials** (passwords, app passwords, tokens) into issues or logs. `config.yaml` is gitignored for a reason — keep it that way.

## Pull requests

1. Fork → create a branch → make your change
2. Test it end-to-end: run the pipeline with `delivery.channel: none` and confirm the verification output is clean
3. Keep the core design principle intact: **scripts first, Agent as fallback** — deterministic keyword hits should stay zero-LLM
4. Personal rules belong in `user_requirements.md` (runtime), not in scripts

## Scope notes

- The WhatsApp channel depends on the host agent's delivery ability; Telegram/webhook channels are fully standalone. PRs adding new channels go in `sender.py`.
- Course-scraper changes: Moodle web UI varies by version/theme — if your institution renders differently, include the failing selector/URL pattern in the PR description.
