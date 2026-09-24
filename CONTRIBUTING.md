# Contributing

1. Create a focused branch from `main`.
2. Keep changes small and describe the user-visible behavior in the commit.
3. Add or update tests for lifecycle, persistence, and error paths.
4. Run the checks locally before opening a pull request.
5. Never commit `.env`, Telegram sessions, logs, databases, or personal data.
6. Treat remote plugins as trusted code; review them before activation.

Recommended checks:

```bash
.venv/bin/ruff check .
.venv/bin/pytest
.venv/bin/mypy src
```
