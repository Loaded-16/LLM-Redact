#!/bin/bash
# Removes Presidio Redactor from Claude Desktop's config (a backup is kept).
# Then you can delete this folder.
set -euo pipefail
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
[[ -f "$CONFIG" ]] || { echo "No Claude Desktop config found. Nothing to remove."; exit 0; }
cp "$CONFIG" "$CONFIG.backup-$(date +%Y%m%d-%H%M%S)"
cd "$(dirname "$0")"
PY=".venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
"$PY" - "$CONFIG" <<'PYEOF'
import json, sys
p = sys.argv[1]
with open(p) as f:
    data = json.load(f)
removed = data.get("mcpServers", {}).pop("presidio-redactor", None)
with open(p, "w") as f:
    json.dump(data, f, indent=2)
print("Removed presidio-redactor." if removed else "presidio-redactor was not in the config.")
PYEOF
echo "Quit and reopen Claude Desktop. You can now delete this folder."
