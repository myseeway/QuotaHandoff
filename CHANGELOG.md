# Changelog

## 1.10.2 — 2026-11-14

- Refresh quota after a renewed login, clearing the previous credentials' failure backoff while preserving request spacing and the selected monitoring mode.
- Discard quota responses from older credentials after a new login is detected.
- Show the retry time when an account is waiting after a failed query, including after restarting the widget.

## 1.10.1 — 2026-11-02

- Split the demo fixture into explicitly typed values so older Swift compilers can build it without a type-checking timeout.

## 1.10.0 — 2026-11-02

First public source release under the name **QuotaHandoff · 额度接力**.

- Added an illustrated README, MIT license, contribution and security guidance.
- Added an isolated native demo app for screenshots and UI verification.
- Replaced installation-specific backup filenames with explicitly imported paths.
- Preserved account storage, Keychain helper identity and the existing 30/10/60-second polling policy.
- Added publication privacy checks and automated tests/build validation.

This public repository starts from a reviewed source snapshot. Private development notes, account records, earlier local Git history and build artifacts are not part of the public release.

Automatic scheduling remains experimental. Full real-world reset-credit redemption is not yet verified end to end.
