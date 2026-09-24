# Security policy

Do not report vulnerabilities through public issues containing secrets or
session data.

Telegram session files and API credentials are equivalent to account access.
Keep them outside Git, use restrictive filesystem permissions, and rotate the
account session if a credential is exposed.

Only install plugins from repositories you trust. The Python plugin runtime is
not a security sandbox: a loaded plugin can access the Telegram client and its
runtime data.
