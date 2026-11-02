# Architecture

QuotaHandoff is a native macOS menu-bar frontend with a local Python controller. It uses no third-party runtime packages.

```text
SwiftUI / AppKit panel
        │ JSON lines over local stdio
        ▼
Python controller ─── SQLite preferences, events, recovery markers
        ├── per-account polling + failure backoff
        ├── policy decisions + cancellation + redemption ledger
        ├── isolated OpenAI app-server for quota queries
        ├── local desktop IPC for task ownership / interruption / continuation
        └── independent Keychain helper for credentials
```

## Files

- `native/main.swift`: panel layout, menu-bar lifecycle, native confirmations, backend connection.
- `native/DemoState.swift`: fictional state used by the independent screenshot/demo build.
- `native/Keychain.swift`: stable credential helper; changes affect macOS authorization identity.
- `backend/service.py`: serialized scheduler, account enrollment, state persistence and recovery orchestration.
- `backend/adapters.py`: login parsing, Keychain IPC, app-server, desktop IPC and normal app termination.
- `backend/policy.py`: quota normalization and account/card selection.
- `backend/polling.py`: independent account polling intervals, backoff and refresh coalescing.

## Automatic handoff

The controller confirms the current login and fresh quota data before selecting a target. It captures the original chat and turn identifiers, interrupts the matching turns, and confirms they stopped before requesting a normal desktop exit. After login replacement and reopening, it resumes only eligible original tasks. New turns, manual stops, uncertain ownership, pending input and unsupported continuous goals prevent unsafe recovery.

Redemption uses a pinned account, credit identifier and idempotency key. An unknown response cannot start a different redemption. A confirmed response still requires a subsequent quota check.

## Manual switching

Manual switching has a separate path. It does not enforce quota thresholds, redeem credits or resume tasks. It enters observation mode, cancels the old automatic recovery plan, stops work where possible, requests normal desktop exit, changes login and reopens the desktop app. A native quit confirmation can still require user intervention.

## Credential enrollment

The active login is observed in the desktop client's standard login file. Additional backups are watched only after explicit import. Stored imported paths from older versions seed the new `backup_paths` list, including during credential-helper migration; no personal filenames are hard-coded in the public source.

## Demo isolation

`scripts/build-demo.sh` compiles with `DEMO`, a separate bundle identifier and no backend/helper payload. The model uses synthetic state; login import, account switching, login-item registration, Keychain access and desktop termination paths are guarded. Mode cycling and card disclosure only change in-memory UI state. The real build also supports `--demo` for local development.
