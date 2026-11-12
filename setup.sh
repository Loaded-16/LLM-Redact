#!/bin/bash
# Presidio Redactor setup for macOS.
# Installs the local Python engine, checks everything works, then adds the
# server to Claude Desktop's config (with a backup). Run from this folder:
#   bash setup.sh          (asks before touching the Claude config)
#   bash setup.sh --yes    (no questions)
set -euo pipefail

cd "$(dirname "$0")"
DIR="$(pwd)"
YES=0
[[ "${1:-}" == "--yes" ]] && YES=1
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

say()  { printf "\n\033[1m%s\033[0m\n" "$*"; }
fail() { printf "\n\033[31m%s\033[0m\n" "$*"; exit 1; }

# ---- 1. Python 3.10 to 3.13 ----
say "1/5  Looking for Python 3.10 to 3.13"
PY=""
for c in python3.12 python3.13 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
    if [[ $v -ge 310 && $v -le 313 ]]; then PY="$(command -v "$c")"; break; fi
  fi
done
[[ -z "$PY" ]] && fail "Python 3.10 to 3.13 not found. Install it with:  brew install python@3.12   (or from python.org), then run this again."
echo "     using $PY ($("$PY" --version))"

# ---- 2. Node 20+ ----
say "2/5  Looking for Node.js 20 or newer"
NODE=""
for c in "$(command -v node 2>/dev/null || true)" /opt/homebrew/bin/node /usr/local/bin/node $(ls -d "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -r); do
  if [[ -n "$c" && -x "$c" ]]; then
    major=$("$c" -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
    if [[ $major -ge 20 ]]; then NODE="$c"; break; fi
  fi
done
[[ -z "$NODE" ]] && fail "Node.js 20+ not found. Install it with:  brew install node   (or from nodejs.org), then run this again."
echo "     using $NODE ($("$NODE" --version))"

# ---- 3. Python environment ----
say "3/5  Installing Presidio and the English language model (about 600 MB, a few minutes)"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
.venv/bin/python -c "import en_core_web_lg" 2>/dev/null || .venv/bin/python -m spacy download en_core_web_lg
echo "     done"

# ---- 4. Self-check ----
say "4/5  Testing the server"
"$NODE" scripts/selfcheck.mjs || fail "The self-check failed. Nothing was added to Claude. Send the message above to Claude for help."

# ---- 5. Claude Desktop config ----
say "5/5  Adding Presidio Redactor to Claude Desktop"
echo "     Config file: $CONFIG"
if [[ $YES -eq 0 ]]; then
  read -r -p "     Add it now? A backup of your current config is saved first. [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "     Skipped. You can run this script again later."; exit 0; }
fi
mkdir -p "$(dirname "$CONFIG")"
if [[ -f "$CONFIG" ]]; then
  BACKUP="$CONFIG.backup-$(date +%Y%m%d-%H%M%S)"
  cp "$CONFIG" "$BACKUP"
  echo "     backup saved: $BACKUP"
fi
.venv/bin/python - "$CONFIG" "$NODE" "$DIR" <<'PYEOF'
import json, os, sys
config_path, node, root = sys.argv[1:4]
data = {}
if os.path.exists(config_path) and os.path.getsize(config_path) > 0:
    with open(config_path) as f:
        data = json.load(f)
data.setdefault("mcpServers", {})["presidio-redactor"] = {
    "command": node,
    "args": [os.path.join(root, "dist", "server.js")],
    "env": {"REDACTOR_PYTHON": os.path.join(root, ".venv", "bin", "python")},
}
with open(config_path, "w") as f:
    json.dump(data, f, indent=2)
print("     added 'presidio-redactor' (your other servers were left as they were)")
PYEOF

say "All set. Quit Claude Desktop fully (Cmd+Q) and open it again."
echo "Then in a new chat, type:  open the redaction panel"
