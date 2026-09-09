#!/usr/bin/env bash
# Build the fenris-engine sidecar (PyInstaller onedir; onefile has the
# documented uvicorn Ctrl+C bug and /tmp exec issues).
# Output: desktop/binaries/fenris-engine{,-<target-triple>} + _internal/
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET_TRIPLE="${1:-$(rustc -vV | awk '/^host:/ {print $2}')}"
OUT="desktop/binaries"

"$(command -v python3 || true)" -m PyInstaller \
  --name fenris-engine \
  --noconfirm --clean --onedir \
  --collect-all uvicorn \
  --copy-metadata uvicorn \
  --collect-all sqlite_vec \
  --hidden-import fenris.api \
  --paths src \
  --distpath "$OUT" \
  --workpath build/pyinstaller \
  --specpath build/pyinstaller \
  src/fenris/__main__.py 2>/dev/null || \
"$(dirname "$0")/../.venv/bin/python" -m PyInstaller \
  --name fenris-engine \
  --noconfirm --clean --onedir \
  --collect-all uvicorn \
  --copy-metadata uvicorn \
  --collect-all sqlite_vec \
  --hidden-import fenris.api \
  --paths src \
  --distpath "$OUT" \
  --workpath build/pyinstaller \
  --specpath build/pyinstaller \
  src/fenris/__main__.py

# Tauri's externalBin expects target-triple-suffixed names
cp -r "$OUT/fenris-engine" "$OUT/fenris-engine-$TARGET_TRIPLE" 2>/dev/null || true
echo "sidecar built at $OUT/fenris-engine-$TARGET_TRIPLE"
