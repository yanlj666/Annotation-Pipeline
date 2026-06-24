# Usage Guide Has Moved

The current AP workflow is documented in:

- [QUICKSTART.md](QUICKSTART.md): shortest path for one real run.
- [GUIDE.md](GUIDE.md): complete feature guide, import contract, reliability reports, persistent VM service, troubleshooting, and maintenance notes.
- [ARCHITECTURE.md](ARCHITECTURE.md): implementation-level architecture notes.

This file is intentionally kept as a migration pointer so old links do not teach stale behavior.

Historical configuration names are no longer valid:

- Do not use `turn_mode`.
- Do not use `conversation_id` as the import contract.
- Do not use `turn_mode: single` or `turn_mode: conversation`.

Use `task_mode` with `fields.session_id`, `fields.exchange_id`, `fields.exchange_time`, and `fields.turns`.
