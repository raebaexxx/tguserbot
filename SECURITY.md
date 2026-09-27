# Security policy

Do not report vulnerabilities through public issues containing secrets or
session data.

Telegram session files and API credentials are equivalent to account access.
Keep them outside Git, use restrictive filesystem permissions, and rotate the
account session if a credential is exposed. The userbot refuses to start when
more than one process holds the same session, and warns at startup if `.env` is
readable by other users.

## Trust boundary

A loaded plugin is trusted code, not a sandbox. An installed plugin runs in this
process with access to the Telegram client, the session file, and the runtime
data. Review a commit before installing it, and allow-list only repositories you
control.

What the plugin sandbox does and does not cover:

- **Isolated:** each plugin's data lives in its own SQLite file, so a plugin
  cannot read or destroy the core bookkeeping tables or another plugin's
  tables, and cannot stall the manager's storage lock. `ATTACH`, `PRAGMA`,
  `VACUUM`, and stacked SQL statements are refused — twice over. The statement
  text is checked for the forbidden keywords, and the connection carries a SQLite
  authoriser that denies those operations on the *parsed* statement, so a payload
  hidden behind a comment is refused by the engine rather than by the text.
  Loading a shared library into the process is disabled on the same connection.
- **Not isolated:** the Telegram client, the session file, the process
  environment, the filesystem outside the data directory, and anything reachable
  over the network. A malicious plugin can do all of that.

`TGUSERBOT_GIT_ALLOWED_REPOS` is the only gate on remote sources. Comparison
ignores case and an optional `.git` suffix; entries are canonicalised before
being compared, so an allow-list entry that looks different from what you type
is still the same repository. `git` runs with its global and system
configuration disabled and only HTTPS and SSH transports permitted, so a local
`url.*.insteadOf` rule cannot turn an allow-listed URL into something git will
execute.

The `/ub` prefix is owner-only. The logged-in account is always an owner, so
anyone who can use that account can install plugins. Treat the session file as
the credential it is.
