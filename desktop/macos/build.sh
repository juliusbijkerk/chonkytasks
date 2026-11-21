#!/usr/bin/env bash
# Build the local chonkytasks macOS app.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/chonkytasks.app"
DIST_APP="$ROOT/dist/chonkytasks.app"
SRC="$ROOT/macos/AIDiscovery"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing project environment. Follow the Python setup in README.md first." >&2
  exit 1
fi
"$PYTHON" -c 'import uvicorn, fastapi, PIL, numpy, httpx, dotenv, multipart' || {
  echo "Install the backend dependencies: .venv/bin/python -m pip install -r requirements.lock.txt" >&2
  exit 1
}

# Ad-hoc signatures identify one exact build, causing macOS to prompt again
# after every native change. Reuse a certificate-backed identity instead.
SIGNING_IDENTITY="${CHONKYTASKS_SIGNING_IDENTITY:-}"
if [[ -z "$SIGNING_IDENTITY" ]]; then
  SIGNING_IDENTITY="$("$PYTHON" "$ROOT/macos/signing_identity.py")"
fi
if [[ "$SIGNING_IDENTITY" == '-' ]]; then
  echo 'Ad-hoc signing cannot preserve macOS permissions. Use a persistent signing certificate.' >&2
  exit 1
fi

# Build outside synced Documents folders: Finder metadata can prevent signing.
STAGING="$(mktemp -d /private/tmp/chonkytasks-build.XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT
BUNDLE="$STAGING/chonkytasks.app"

echo "==> Compiling Swift sources"
SDK="$(xcrun --show-sdk-path)"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
swiftc -O -swift-version 5 -sdk "$SDK" -target "$(uname -m)-apple-macosx13.0" \
  -framework AppKit -framework WebKit -framework Foundation -framework UniformTypeIdentifiers \
  "$SRC"/*.swift -o "$BUNDLE/Contents/MacOS/chonkytasks"

echo "==> Assembling bundle"
cp -X "$ROOT/macos/Info.plist" "$BUNDLE/Contents/Info.plist"
printf 'APPL????' > "$BUNDLE/Contents/PkgInfo"
printf '%s\n' "$ROOT" > "$BUNDLE/Contents/Resources/backend-root.txt"

echo "==> Building Mail and Calendar helper"
HELPER="$BUNDLE/Contents/Library/LoginItems/chonkytasks Automations.app"
mkdir -p "$HELPER/Contents/MacOS" "$HELPER/Contents/Resources"
swiftc -O -swift-version 5 -sdk "$SDK" -target "$(uname -m)-apple-macosx13.0" \
  -framework AppKit -framework Carbon -framework EventKit -framework Security \
  "$ROOT/macos/AutomationHelper/main.swift" -o "$HELPER/Contents/MacOS/AutomationHelper"
cp -X "$ROOT/macos/AutomationHelper/Info.plist" "$HELPER/Contents/Info.plist"
osacompile -o "$HELPER/Contents/Resources/Mail.scpt" "$ROOT/macos/AutomationHelper/Mail.applescript"
codesign --force --timestamp=none --sign "$SIGNING_IDENTITY" "$HELPER"

echo "==> Building local visual redaction helper"
mkdir -p "$BUNDLE/Contents/Helpers"
swiftc -O -swift-version 5 -sdk "$SDK" -target "$(uname -m)-apple-macosx13.0" \
  "$ROOT/macos/VisualReview/"*.swift -o "$BUNDLE/Contents/Helpers/RedactEvidence"
codesign --force --timestamp=none --sign "$SIGNING_IDENTITY" "$BUNDLE/Contents/Helpers/RedactEvidence"
swiftc -O -swift-version 5 -sdk "$SDK" -target "$(uname -m)-apple-macosx13.0" \
  "$ROOT/macos/WorkflowEvidence/"*.swift -o "$BUNDLE/Contents/Helpers/FindWorkflowEvidence"
codesign --force --timestamp=none --sign "$SIGNING_IDENTITY" "$BUNDLE/Contents/Helpers/FindWorkflowEvidence"

echo "==> Building icon"
"$PYTHON" "$ROOT/macos/make_icon.py" "$STAGING/chonkytasks.iconset"
iconutil -c icns "$STAGING/chonkytasks.iconset" -o "$BUNDLE/Contents/Resources/chonkytasks.icns"

echo "==> Signing and verifying"
codesign --force --timestamp=none --sign "$SIGNING_IDENTITY" "$BUNDLE"
codesign --verify --deep --strict "$BUNDLE"
plutil -lint "$BUNDLE/Contents/Info.plist"

# Do not discard the last build until compilation and signing have succeeded.
mkdir -p "$HOME/Applications" "$ROOT/dist"
rm -rf "$APP"
ditto --norsrc --noextattr "$BUNDLE" "$APP"
codesign --verify --deep --strict "$APP"

# A synced Documents folder can add Finder metadata again immediately after a
# successful build. Keep the signed bundle in Applications and link to it here.
rm -rf "$DIST_APP"
ln -s "$APP" "$DIST_APP"

echo "==> Built $APP"
echo "    Run it with: open \"$APP\""
echo "    Backend root recorded: $ROOT (override with CHONKYTASKS_HOME)"
