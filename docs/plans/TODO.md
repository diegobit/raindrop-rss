# TODO

Status: `ready` (unblocked) · `blocked` · `wip` · `done`

- [x] `done` **01a Implement the Raindrop RSS bridge** — [plan](20260908-raindrop-rss-01-implementation.md) — steps 1-4. Code, deterministic suite (152 cases, including reviewed-finding regressions), Docker build and a containerised fixture smoke are complete; no live account or deployment call was made.
- [ ] `blocked` **01b Smoke-test on the Geekom** — [plan](20260908-raindrop-rss-01-implementation.md#5-smoke-test-on-the-geekom) — blocked by: host access, a Raindrop personal test token, the real RSS Inbox collection ID, and a Telegram profile for `tg` (`~/.telegram-bot-token-<profile>` and `~/.telegram-chat-id-<profile>`). Confirm one live import, a duplicate-free rerun, an hourly pass, a failure/recovery Telegram message, GUI access over the SSH tunnel, and state intact after a host reboot.
