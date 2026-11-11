#!/usr/bin/env bash
# Remove peek from your user folder. Python packages it may have installed are left alone.
set -euo pipefail
rm -f "${PEEK_BIN:-$HOME/.local/bin}/peek"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/peek"
echo "peek removed."
