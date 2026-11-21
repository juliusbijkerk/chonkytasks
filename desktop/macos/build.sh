#!/usr/bin/env bash
# Build the local chonkytasks macOS app.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/chonkytasks.app"
DIST_APP="$ROOT/dist/chonkytasks.app"
RELEASE_DIR="${CHONKYTASKS_RELEASE_DIR:-}"
FFMPEG_DIR="${CHONKYTASKS_FFMPEG_DIR:-}"
if [[ -n "$RELEASE_DIR" ]]; then
  APP="$RELEASE_DIR/ChonkyTasks.app"
  if [[ -e "$APP" || -L "$APP" ]]; then
    echo "Choose an empty release destination or move the existing ChonkyTasks.app first." >&2
    exit 1
  fi
  if [[ -z "$FFMPEG_DIR" || ! -x "$FFMPEG_DIR/ffmpeg" || ! -x "$FFMPEG_DIR/ffprobe" ]]; then
    echo "Set CHONKYTASKS_FFMPEG_DIR to a directory with both ffmpeg and ffprobe." >&2
    exit 1
  fi
fi
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
if [[ "$SIGNING_IDENTITY" == '-' && -z "$RELEASE_DIR" ]]; then
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
  -framework ScreenCaptureKit -framework AVFoundation \
  "$SRC"/*.swift -o "$BUNDLE/Contents/MacOS/chonkytasks"

echo "==> Assembling bundle"
cp -X "$ROOT/macos/Info.plist" "$BUNDLE/Contents/Info.plist"
if [[ -n "$RELEASE_DIR" ]]; then
  /usr/libexec/PlistBuddy -c 'Set :CFBundleIdentifier com.mistralhackathon.chonkytasks.release' "$BUNDLE/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName ChonkyTasks' "$BUNDLE/Contents/Info.plist"
fi
printf 'APPL????' > "$BUNDLE/Contents/PkgInfo"
printf '%s\n' "$ROOT" > "$BUNDLE/Contents/Resources/backend-root.txt"

echo "==> Building Mail and Calendar helper"
HELPER="$BUNDLE/Contents/Library/LoginItems/chonkytasks Automations.app"
mkdir -p "$HELPER/Contents/MacOS" "$HELPER/Contents/Resources"
swiftc -O -swift-version 5 -sdk "$SDK" -target "$(uname -m)-apple-macosx13.0" \
  -framework AppKit -framework Carbon -framework EventKit -framework Security \
  "$ROOT/macos/AutomationHelper/main.swift" -o "$HELPER/Contents/MacOS/AutomationHelper"
cp -X "$ROOT/macos/AutomationHelper/Info.plist" "$HELPER/Contents/Info.plist"
if [[ -n "$RELEASE_DIR" ]]; then
  /usr/libexec/PlistBuddy -c 'Set :CFBundleIdentifier com.mistralhackathon.chonkytasks.release.automations' "$HELPER/Contents/Info.plist"
fi
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

if [[ -n "$FFMPEG_DIR" ]]; then
  echo "==> Bundling FFmpeg"
  for tool in ffmpeg ffprobe; do
    cp -X "$FFMPEG_DIR/$tool" "$BUNDLE/Contents/Helpers/$tool"
    codesign --force --timestamp=none --sign "$SIGNING_IDENTITY" "$BUNDLE/Contents/Helpers/$tool"
  done
fi

echo "==> Building icon"
"$PYTHON" "$ROOT/macos/make_icon.py" "$STAGING/chonkytasks.iconset"
iconutil -c icns "$STAGING/chonkytasks.iconset" -o "$BUNDLE/Contents/Resources/chonkytasks.icns"

echo "==> Signing and verifying"
codesign --force --timestamp=none --sign "$SIGNING_IDENTITY" "$BUNDLE"
codesign --verify --deep --strict "$BUNDLE"
plutil -lint "$BUNDLE/Contents/Info.plist"

# Keep release builds and the installed development app independent.
if [[ -n "$RELEASE_DIR" ]]; then
  mkdir -p "$RELEASE_DIR"
  ditto --norsrc --noextattr "$BUNDLE" "$APP"
else
  mkdir -p "$HOME/Applications" "$ROOT/dist"
  rm -rf "$APP"
  ditto --norsrc --noextattr "$BUNDLE" "$APP"
  rm -rf "$DIST_APP"
  ln -s "$APP" "$DIST_APP"
fi
codesign --verify --deep --strict "$APP"

echo "==> Built $APP"
echo "    Run it with: open \"$APP\""
echo "    Backend root recorded: $ROOT (override with CHONKYTASKS_HOME)"
