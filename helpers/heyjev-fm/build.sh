#!/bin/sh
# Build heyjev-fm optionally. Never fails the app build: exits 0 even on failure.
set -u
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$(cd "$SRC_DIR/../.." && pwd)/bin"
OUT="$OUT_DIR/heyjev-fm"
mkdir -p "$OUT_DIR"
if ! command -v swiftc >/dev/null 2>&1; then
  echo "heyjev-fm: swiftc not found, skipping" >&2
  exit 0
fi
if swiftc -O -target arm64-apple-macosx14.0 -o "$OUT" "$SRC_DIR/main.swift" 2>&2; then
  echo "heyjev-fm: built $OUT"
  exit 0
fi
echo "heyjev-fm: full build failed (likely macOS 26 SDK without PCC symbols), retrying without PCC" >&2
if swiftc -O -D NO_PCC -target arm64-apple-macosx14.0 -o "$OUT" "$SRC_DIR/main.swift" 2>&2; then
  echo "heyjev-fm: built $OUT (NO_PCC fallback)"
  exit 0
else
  echo "heyjev-fm: build failed or unsupported SDK, skipping" >&2
  exit 0
fi
