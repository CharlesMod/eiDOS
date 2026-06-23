# eiDOS one-step installer for Windows (PowerShell).
#
#   git clone https://github.com/CharlesMod/eiDOS.git; cd eiDOS; ./install.ps1
#
# Creates a virtualenv, installs dependencies, writes a machine-local config (config.local.toml) with
# safe defaults, and launches the dashboard at http://localhost:8099. It does NOT install an LLM server
# — bring your own OpenAI-compatible server (Ollama / LM Studio / llama.cpp) and set it in Settings (gear).
#
# If you get an execution-policy error, run once:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#
# Flags: -WithEmbeddings  -LlmUrl <url>  -Model <name>  -NoLaunch
param(
  [switch]$WithEmbeddings,
  [switch]$NoLaunch,
  [string]$LlmUrl = "",
  [string]$Model = ""
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --- Python (need 3.9+, prefer 3.11+) ---
$py = $null
foreach ($c in @("python", "python3", "py")) {
  if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) { Write-Error "Python 3.9+ not found. Install from https://www.python.org/downloads/ (check 'Add to PATH')."; exit 1 }
$pyv = & $py -c "import sys;print('%d.%d'%sys.version_info[:2])"
Write-Host "-> Python $pyv ($py)"
& $py -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Error "need Python >= 3.9 (found $pyv)"; exit 1 }

# --- venv + deps ---
if (-not (Test-Path ".venv")) { Write-Host "-> creating .venv"; & $py -m venv .venv }
$vpy = ".venv\Scripts\python.exe"
Write-Host "-> installing dependencies"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r requirements.txt
if ($WithEmbeddings) {
  Write-Host "-> installing embedding deps + model"
  & $vpy -m pip install --quiet onnxruntime tokenizers
  & $vpy setup_embedding.py
}

# --- machine-local config overlay (safe defaults; never overwrites an existing one) ---
if (-not (Test-Path "config.local.toml")) {
  Write-Host "-> writing config.local.toml from template"
  Copy-Item "config.template.toml" "config.local.toml"
} else {
  Write-Host "-> keeping your existing config.local.toml"
}

# --- optional pre-seeds via the TOML writer ---
$seed = @"
import sys, config
url, model, emb = sys.argv[1], sys.argv[2], sys.argv[3] == '1'
ch = {}
if url:   ch.setdefault('llm', {})['url'] = url
if model: ch.setdefault('llm', {})['model'] = model
if emb:   ch['knowledge'] = {'embedding_enabled': True}
if ch:
    config.save_overrides(ch, path='config.toml'); print('-> seeded:', ch)
"@
$embFlag = if ($WithEmbeddings) { "1" } else { "0" }
& $vpy -c $seed $LlmUrl $Model $embFlag

Write-Host ""
Write-Host "eiDOS installed." -ForegroundColor Green
Write-Host "  Next: start an OpenAI-compatible LLM server, then set its URL + model in Settings (gear)."
Write-Host "  Dashboard: http://localhost:8099"
Write-Host ""
if (-not $NoLaunch) {
  Write-Host "-> starting the dashboard (Ctrl-C to stop)..."
  & $vpy dashboard.py --config config.toml
} else {
  Write-Host "  Start it yourself with:  .venv\Scripts\python.exe dashboard.py --config config.toml"
}
