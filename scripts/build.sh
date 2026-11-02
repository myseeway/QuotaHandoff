#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/dist/QuotaHandoff.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/backend" "$ROOT/.build-cache"
HELPER="$APP/Contents/MacOS/CodexQuotaKeychain"
SOURCE_HASH="$(shasum -a 256 "$ROOT/native/Keychain.swift" | awk '{print $1}')"
INSTALLED="$HOME/Applications/QuotaHandoff.app/Contents"
if [ ! -f "$INSTALLED/MacOS/CodexQuotaKeychain" ]; then
  INSTALLED="$HOME/Applications/Codex 额度.app/Contents"
fi
if [ -f "$INSTALLED/MacOS/CodexQuotaKeychain" ] && [ -f "$INSTALLED/Resources/keychain-helper.sha256" ] && \
   [ "$(cat "$INSTALLED/Resources/keychain-helper.sha256")" = "$SOURCE_HASH" ] && \
   /usr/bin/codesign --verify --strict "$INSTALLED/MacOS/CodexQuotaKeychain" 2>/dev/null; then
  cp "$INSTALLED/MacOS/CodexQuotaKeychain" "$HELPER"
elif [ -f "$HELPER" ] && [ -f "$APP/Contents/Resources/keychain-helper.sha256" ] && \
     [ "$(cat "$APP/Contents/Resources/keychain-helper.sha256")" = "$SOURCE_HASH" ] && \
     /usr/bin/codesign --verify --strict "$HELPER" 2>/dev/null; then
  : # Reuse the exact signed helper. UI releases must not change its keychain identity.
else
  xcrun swiftc -O -swift-version 5 -module-cache-path "$ROOT/.build-cache" \
    -framework Foundation -framework Security -framework LocalAuthentication \
    "$ROOT/native/Keychain.swift" -o "$HELPER"
  /usr/bin/codesign --force --sign - --identifier local.CodexQuota.KeychainHelper "$HELPER"
fi
printf '%s\n' "$SOURCE_HASH" > "$APP/Contents/Resources/keychain-helper.sha256"
xcrun swiftc -O -swift-version 5 -module-cache-path "$ROOT/.build-cache" \
  -framework AppKit -framework SwiftUI -framework Security -framework ServiceManagement \
  "$ROOT/native/main.swift" "$ROOT/native/DemoState.swift" -o "$APP/Contents/MacOS/CodexQuota"
cp "$ROOT"/backend/*.py "$APP/Contents/Resources/backend/"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.CodexQuota</string>
<key>CFBundleName</key><string>QuotaHandoff</string>
<key>CFBundleDisplayName</key><string>QuotaHandoff</string>
<key>CFBundleExecutable</key><string>CodexQuota</string>
<key>CFBundleShortVersionString</key><string>1.10.1</string>
<key>CFBundleVersion</key><string>15</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
<key>NSAppleEventsUsageDescription</key><string>在你开启自动调度后，正常退出并重新打开 ChatGPT 以切换账号。</string>
</dict></plist>
PLIST
/usr/bin/codesign --force --sign - --identifier local.CodexQuota "$APP"
printf '%s\n' "$APP"
