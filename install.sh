#!/usr/bin/env bash
# eiDOS one-step installer for macOS / Linux / Raspberry Pi.
#
#   git clone https://github.com/CharlesMod/eiDOS.git && cd eiDOS && bash install.sh
#
# Creates a virtualenv, installs dependencies, writes a machine-local config (config.local.toml) with
# safe defaults, and launches the dashboard at http://localhost:8099. It does NOT install an LLM server
# — bring your own OpenAI-compatible server (Ollama / LM Studio / llama.cpp) and set it in Settings ⚙.
#
# Flags:
#   --with-model        download the house-mind GGUF + primary embedder (~12.8 GB) for self-hosting
#                       on a GPU box (gemma-4-12b-it Q8_0 + nomic-embed, via download_model.py)
#   --with-embeddings   install onnxruntime + fetch the ONNX FALLBACK embedder (CPU/no-GPU semantic memory)
#   --models-dir DIR    where --with-model writes (default: ./models); point llama.cpp -m here
#   --llm-url URL       pre-seed the LLM endpoint    (e.g. http://127.0.0.1:11434/v1)
#   --model NAME        pre-seed the model name
#   --no-launch         set up only; don't start the dashboard
set -euo pipefail
cd "$(dirname "$0")"

WITH_EMBED=0; WITH_MODEL=0; MODELS_DIR="models"; LAUNCH=1; LLM_URL=""; MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-embeddings) WITH_EMBED=1 ;;
    --with-model) WITH_MODEL=1 ;;
    --models-dir) MODELS_DIR="${2:-models}"; shift ;;
    --no-launch) LAUNCH=0 ;;
    --llm-url) LLM_URL="${2:-}"; shift ;;
    --model) MODEL="${2:-}"; shift ;;
    *) echo "unknown flag: $1"; exit 2 ;;
  esac
  shift
done

# --- Python (need 3.9+, prefer 3.11+ for stdlib tomllib) ---
PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "✗ Python 3.9+ not found. Install it (macOS: brew install python; Debian/Pi: sudo apt install python3 python3-venv)"; exit 1; }
PYV=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
echo "→ Python $PYV ($PY)"
"$PY" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)' \
  || { echo "✗ need Python ≥ 3.9 (found $PYV)"; exit 1; }

# --- venv + deps ---
# Naive-machine gotcha: Debian/Ubuntu ship `python3` WITHOUT the venv/pip modules (they're separate
# apt packages). Detect that up front and fix it (auto via apt when we can, else a precise instruction)
# so a fresh box doesn't dump a confusing traceback at venv-creation time.
if ! "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
  echo "→ Python venv/pip support is missing (common on a fresh Debian/Ubuntu)."
  PYVER=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  if command -v apt-get >/dev/null 2>&1; then
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    echo "  installing python3-venv + python3-pip via apt ($SUDO)…"
    $SUDO apt-get update -qq && \
      $SUDO apt-get install -y -qq python3-venv python3-pip "python${PYVER}-venv" >/dev/null 2>&1 || true
  fi
  if ! "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
    echo "✗ Couldn't enable venv automatically. Install your distro's Python venv+pip packages, e.g.:"
    echo "    sudo apt-get install -y python3-venv python3-pip   # Debian/Ubuntu/Pi"
    echo "    (macOS: Python from python.org or Homebrew already includes venv)"
    exit 1
  fi
fi
if [ ! -d .venv ]; then echo "→ creating .venv"; "$PY" -m venv .venv; fi
VPY=".venv/bin/python"
echo "→ installing dependencies"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r requirements.txt
if [ "$WITH_EMBED" = "1" ]; then
  echo "→ installing embedding deps + ONNX fallback model"
  "$VPY" -m pip install --quiet onnxruntime tokenizers
  "$VPY" setup_embedding.py || echo "  (embedding model fetch failed — semantic memory stays off)"
fi
if [ "$WITH_MODEL" = "1" ]; then
  echo "→ downloading the house-mind + primary embedder into $MODELS_DIR (resumable; ~12.8 GB)"
  "$VPY" download_model.py all --models-dir "$MODELS_DIR" \
    || echo "  (model download failed — rerun 'python3 download_model.py all' to resume from the .part)"
fi

# --- machine-local config overlay (safe defaults; never overwrites an existing one) ---
if [ ! -f config.local.toml ]; then
  echo "→ writing config.local.toml from template"
  cp config.template.toml config.local.toml
else
  echo "→ keeping your existing config.local.toml"
fi

# --- optional pre-seeds (use the writer so the overlay stays valid TOML) ---
"$VPY" - "$LLM_URL" "$MODEL" "$WITH_EMBED" <<'PY'
import sys, config
url, model, emb = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
ch = {}
if url:   ch.setdefault("llm", {})["url"] = url
if model: ch.setdefault("llm", {})["model"] = model
if emb:   ch["knowledge"] = {"embedding_enabled": True}
if ch:
    config.save_overrides(ch, path="config.toml")
    print("→ seeded:", ch)
PY

echo ""
echo "✓ eiDOS installed."
echo "  Next: make sure an OpenAI-compatible LLM server is running, then set its URL + model in Settings ⚙."
echo "  Edit config.local.toml or use the dashboard. Dashboard: http://localhost:8099"
echo ""
if [ "$LAUNCH" = "1" ]; then
  echo "→ starting the dashboard (Ctrl-C to stop)…"
  exec "$VPY" dashboard.py --config config.toml
else
  echo "  Start it yourself with:  .venv/bin/python dashboard.py --config config.toml"
fi
