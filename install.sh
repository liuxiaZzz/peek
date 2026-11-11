#!/usr/bin/env bash
# Install peek into your user folder (no sudo):
#   curl -fsSL https://raw.githubusercontent.com/liuxiaZzz/peek/main/install.sh | bash
#   or: git clone https://github.com/liuxiaZzz/peek && peek/install.sh
set -euo pipefail
RAW=https://raw.githubusercontent.com/liuxiaZzz/peek/main
BIN="${PEEK_BIN:-$HOME/.local/bin}"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}/peek"
here=""
[[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]] && here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$BIN" "$SHARE"
if [[ -n $here && -f "$here/peek" && -f "$here/peek_server.py" ]]; then
  cp "$here/peek" "$BIN/peek"
  cp "$here/peek_server.py" "$SHARE/peek_server.py"
else
  curl -fsSL "$RAW/peek" -o "$BIN/peek"
  curl -fsSL "$RAW/peek_server.py" -o "$SHARE/peek_server.py"
fi
chmod +x "$BIN/peek"

printf '\033[38;2;228;139;180m✓\033[0m peek installed: %s\n' "$BIN/peek"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  $BIN is not on your PATH yet. Add this line to ~/.zshrc or ~/.bashrc:"
     echo "    export PATH=\"$BIN:\$PATH\"" ;;
esac
echo "  Try:  peek --help"
echo "  Then: peek yourserver:/path/to/slides"
