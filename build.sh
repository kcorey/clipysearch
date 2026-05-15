#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="ClipySearch"
BUILD_DIR="build"
APP_DIR="$BUILD_DIR/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

ARCH=$(uname -m)
TARGET="${ARCH}-apple-macos12"

echo "Building for $TARGET..."
rm -rf "$APP_DIR"
mkdir -p "$MACOS" "$RESOURCES"

swiftc -O \
    -target "$TARGET" \
    -framework Cocoa \
    -o "$MACOS/$APP_NAME" \
    Sources/*.swift

cp Info.plist "$CONTENTS/Info.plist"

# Touch bundle so LaunchServices re-reads it
touch "$APP_DIR"

echo
echo "Built: $APP_DIR"
echo
echo "Run:        open $APP_DIR"
echo "Install:    cp -R $APP_DIR /Applications/"
