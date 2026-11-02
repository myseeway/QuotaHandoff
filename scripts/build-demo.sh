#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/dist/QuotaHandoff Demo.app"
mkdir -p "$APP/Contents/MacOS" "$ROOT/.build-cache"
# A separate identity and no backend/keychain helper: safe to run beside the real app.
xcrun swiftc -O -D DEMO -swift-version 5 -module-cache-path "$ROOT/.build-cache" \
  -framework AppKit -framework SwiftUI -framework Security -framework ServiceManagement \
  "$ROOT/native/main.swift" "$ROOT/native/DemoState.swift" -o "$APP/Contents/MacOS/QuotaHandoffDemo"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.QuotaHandoff.Demo</string>
<key>CFBundleName</key><string>QuotaHandoff Demo</string>
<key>CFBundleExecutable</key><string>QuotaHandoffDemo</string>
<key>CFBundleShortVersionString</key><string>1.10.0</string>
<key>CFBundleVersion</key><string>14</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
/usr/bin/codesign --force --sign - --identifier local.QuotaHandoff.Demo "$APP"
printf '%s\n' "$APP"
