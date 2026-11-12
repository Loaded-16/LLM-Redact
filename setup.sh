#!/bin/bash
# Presidio Redactor setup for macOS.
# Installs the local Python engine, checks everything works, then adds the
# server to Claude Desktop's config (with a backup). Run from this folder:
#   bash setup.sh            asks before touching the Claude config
#   bash setup.sh --yes      no questions
# Optional add-ons (remembered for later runs):
#   --ocr      read scanned PDFs (about 80 MB)
#   --gliner   GLiNER AI model for names instead of spaCy's (about 1.5 GB with PyTorch)
#   --arabic   Arabic-script PII model (about 3 GB with PyTorch)
#   --basic    switch all add-ons off again
set -euo pipefail

cd "$(dirname "$0")"
DIR="$(pwd)"
YES=0
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

# Add-ons chosen now or on an earlier run.
FEATURES_FILE="$DIR/.features"
OCR=0; GLINER=0; ARABIC=0
if [[ -f "$FEATURES_FILE" ]]; then
  grep -qx ocr "$FEATURES_FILE" && OCR=1 || true
  grep -qx gliner "$FEATURES_FILE" && GLINER=1 || true
  grep -qx arabic "$FEATURES_FILE" && ARABIC=1 || true
fi
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --ocr) OCR=1 ;;
    --gliner) GLINER=1 ;;
    --arabic) ARABIC=1 ;;
    --basic) OCR=0; GLINER=0; ARABIC=0 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done
{ [[ $OCR -eq 1 ]] && echo ocr; [[ $GLINER -eq 1 ]] && echo gliner; [[ $ARABIC -eq 1 ]] && echo arabic; true; } > "$FEATURES_FILE"

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

if [[ $OCR -eq 1 ]]; then
  say "     Add-on: OCR for scanned PDFs"
  .venv/bin/python -m pip install --quiet -r requirements-ocr.txt
  .venv/bin/python -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR()" >/dev/null || fail "OCR add-on failed to load."
  echo "     done"
fi
if [[ $GLINER -eq 1 || $ARABIC -eq 1 ]]; then
  say "     Installing PyTorch for the AI add-ons (large download, can take 10 minutes)"
  .venv/bin/python -m pip install --quiet -r requirements-arabic.txt
fi
if [[ $GLINER -eq 1 ]]; then
  say "     Add-on: GLiNER name model"
  .venv/bin/python -m pip install --quiet -r requirements-gliner.txt
  HF_HUB_OFFLINE=0 .venv/bin/python -c "from gliner import GLiNER; GLiNER.from_pretrained('knowledgator/gliner-pii-base-v1.0')" >/dev/null \
    || fail "Could not download the GLiNER model. Check your internet connection and run setup again."
  echo "     done"
fi
if [[ $ARABIC -eq 1 ]]; then
  say "     Add-on: Arabic model (about 2 GB download)"
  HF_HUB_OFFLINE=0 .venv/bin/python -c "
from transformers import pipeline
pipeline('token-classification', model='OpenMed/OpenMed-PII-Arabic-BigMed-Large-560M-v1', aggregation_strategy='simple', device=-1)" >/dev/null \
    || fail "Could not download the Arabic model. Check your internet connection and run setup again."
  echo "     done"
fi

# ---- 4. Self-check ----
say "4/5  Testing the server"
REDACTOR_NER=$([[ $GLINER -eq 1 ]] && echo gliner || echo spacy) REDACTOR_ARABIC=$ARABIC \
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
.venv/bin/python - "$CONFIG" "$NODE" "$DIR" "$GLINER" "$ARABIC" <<'PYEOF'
import json, os, sys
config_path, node, root, gliner, arabic = sys.argv[1:6]
data = {}
if os.path.exists(config_path) and os.path.getsize(config_path) > 0:
    with open(config_path) as f:
        data = json.load(f)
data.setdefault("mcpServers", {})["presidio-redactor"] = {
    "command": node,
    "args": [os.path.join(root, "dist", "server.js")],
    "env": {
        "REDACTOR_PYTHON": os.path.join(root, ".venv", "bin", "python"),
        "REDACTOR_NER": "gliner" if gliner == "1" else "spacy",
        "REDACTOR_ARABIC": arabic,
    },
}
with open(config_path, "w") as f:
    json.dump(data, f, indent=2)
print("     added 'presidio-redactor' (your other servers were left as they were)")
PYEOF

say "All set. Quit Claude Desktop fully (Cmd+Q) and open it again."
echo "Then in a new chat, type:  open the redaction panel"
