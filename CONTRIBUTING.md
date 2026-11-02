# Contributing

Thank you for helping improve QuotaHandoff. Small, reviewable changes with clear behavior and synthetic reproduction data are welcome.

## Development

1. Install Apple Command Line Tools on macOS 13 or later.
2. Run `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -q`.
3. Build with `bash scripts/build.sh`.
4. For UI work, build `bash scripts/build-demo.sh` and open `dist/QuotaHandoff Demo.app` in Finder.

The Python backend has no third-party runtime dependencies. Tests use temporary stores and mocked credentials; do not add tests that query live accounts, redeem cards, stop real chats, or write to the user's login directory.

## UI and screenshots

Use the demo app to check first launch, closing/reopening the popover, all three monitoring modes, card disclosure, and settings scrolling. Accessibility-tree checks alone do not prove that layout is correct; inspect actual screenshots. Capture only the demo app, with its visible “演示数据” label. Keep the originals in `docs/images/`, without personal desktop windows or metadata. No generative reconstruction of the interface is needed.

## Compatibility

The current public name is QuotaHandoff. The `local.CodexQuota` bundle identifier, `CodexQuota` state directory and Keychain service are intentionally retained for upgrades from earlier local versions. Do not rename these casually or rebuild the credential helper for a UI-only change.

Task control uses desktop IPC and must fail closed when identity, ownership or turn state is uncertain. Preserve the distinction between manual login switching and automatic task recovery. Never weaken idempotency or cancellation checks to make a test pass.

## Before a pull request

- Explain the concrete behavior change and relevant verification.
- Add tests for meaningful changes to scheduling, credentials or task control.
- Stage only intended public files, then run `python3 scripts/privacy_check.py --history`.
- Review the staged diff and all image contents yourself.
- Keep examples fictional: use `example.com` or `.test` addresses and temporary directories.
- Avoid personal commit email addresses when privacy matters; use your GitHub-provided noreply address.

Contributions are provided under the project's [MIT License](LICENSE).
