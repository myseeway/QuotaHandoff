# Security & privacy

QuotaHandoff handles existing login credentials on the local Mac. It is an independent, experimental tool, not an authentication or account-sharing service.

## Data boundaries

- Credentials are stored in macOS Keychain using a separate helper. They are passed to the bundled OpenAI app-server in memory; raw tokens and raw RPC responses must not be logged.
- Switching replaces the desktop client's current login file. Do not use the same credential files concurrently from unrelated account-switching tools.
- The local state database includes profile display information, imported paths, task metadata, scheduling records and events. It is personal data even though it does not contain login tokens.
- No telemetry or project-operated server is included. Quota requests communicate with OpenAI through its local CLI.
- The demo app has a separate bundle identifier, no backend, and no credential helper. Its identities and tasks are fictional.

## Reporting

Do not post tokens, `auth.json`, Keychain exports, raw app-server traffic, personal screenshots or a copy of the state directory in public Issues. Report a reproducible problem using fabricated data. If a credential has been disclosed, revoke or replace it through the relevant service; deleting a Git commit alone does not revoke access.

For a sensitive vulnerability, use the repository's private vulnerability-reporting channel when available. If unavailable, first open an issue asking for a private contact without including exploit details or secrets.

## Before publishing changes

Run `python3 scripts/privacy_check.py --history`, review the staged diff, and inspect screenshots visually. The script checks common credential patterns, personal paths, email addresses and private file names; it cannot guarantee that all sensitive information is detected.

Only screenshot the dedicated demo app for documentation. Do not commit runtime state, logs, credentials, personal usage records, `.app` bundles or compiler caches. Distribute binaries only through a separately reviewed release process; local builds may contain machine-specific paths.

Keep the Keychain helper stable across UI updates. Never solve repeated prompts by broadening Keychain access to all applications, saving the Mac password, or disabling system protections. Updates to the helper require deliberate review and may require new user authorization.
