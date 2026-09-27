# Contributing

1. Create a focused branch from `main`.
2. Keep changes small and describe the user-visible behavior in the commit.
3. Add or update tests for lifecycle, persistence, and error paths. A fix
   without a regression test is not finished.
4. Run the checks locally before opening a pull request.
5. Never commit `.env`, Telegram sessions, logs, databases, or personal data.
6. Treat remote plugins as trusted code; review them before activation.

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src plugins tests
.venv/bin/pytest
```

`mypy` covers `src`, `plugins`, and `tests`; the last two were previously
unchecked and hid real errors. `pytest` enforces an 85% coverage floor.

Shell scripts have their own checks:

```bash
bash -n userbotctl deploy/install.sh
shellcheck -S style userbotctl deploy/install.sh
```

Tests marked `git` need a `git` executable and are skipped without one.

## Writing a test

- Never load the repository's real `plugins/` directory. Build a plugin tree
  under `tmp_path` with the helpers in `tests/conftest.py`, or use the
  `real_plugin_dir` fixture, which copies the shipped plugins into a temporary
  tree.
- Use `pytest-asyncio` (`asyncio_mode = "auto"`); do not call `asyncio.run`.
- Mock Telethon. Nothing in the suite may touch the network.
- If a fix addresses a defect, name the test after the defect and say what went
  wrong in its docstring, so the reason survives the next refactor.

## Plugin API compatibility

Everything exported from `userbot.plugin_api` (and re-exported from the
`userbot` package root) is covered by semantic versioning. Deeper module paths
are internal. A change to the public surface needs a `CHANGELOG.md` entry and,
if it is breaking, a major version bump.
