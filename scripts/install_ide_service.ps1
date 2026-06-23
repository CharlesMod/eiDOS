<#
.SYNOPSIS
  Register EidosCodeIDE (the pi mini-IDE) as a Windows nssm service.

.DESCRIPTION
  ide.py is a standalone service (own process, like voice.py) so an experimental IDE crash
  can't wound the dashboard watchdog. This registers it as "EidosCodeIDE" on config.ide_port
  (default 8100), separate from the dashboard (8099) and voice (8098).

  Manual launch (no service needed):
      $env:PYTHONUTF8=1; python ide.py --config config.toml

  A code change goes live by `Restart-Service EidosCodeIDE`. On boot it reaps any pi left
  detached by a prior crash and surfaces prior stints as resumable (cold). HouseAI-Llama must
  be running (the IDE's pi talks to house-ai via the :8088 monitor tap).

.PARAMETER NoStart
  Register but do not start. DEFAULT false -- the IDE is safe to start immediately (unlike the
  voice cutover), so this installs AND starts unless you pass -NoStart.
#>
param(
    [string]$RepoDir     = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)),
    [string]$Python      = "",
    [string]$ServiceName = "EidosCodeIDE",
    [switch]$NoStart     = $false
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'find-nssm.ps1')
$nssm = Get-NssmPath

if (-not $Python) {
    $candidates = @(
        (Join-Path $RepoDir ".venv\Scripts\python.exe"),
        "C:\Users\cmod\llm\Kairos\.venv\Scripts\python.exe"
    )
    $Python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Python) { throw "No python.exe found. Pass -Python <path>." }
}

$ide = Join-Path $RepoDir "ide.py"
if (-not (Test-Path $ide)) { throw "ide.py not found at $ide" }

Write-Host "Registering '$ServiceName'"
Write-Host "  python : $Python"
Write-Host "  script : $ide"
Write-Host "  workdir: $RepoDir"

# Idempotent: remove a stale registration first.
$existing = & $nssm status $ServiceName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  (service exists - removing and re-creating)"
    & $nssm stop $ServiceName 2>$null | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
}

& $nssm install $ServiceName $Python $ide "--config" "config.toml"
& $nssm set $ServiceName AppDirectory $RepoDir
# The service runs as LocalSystem (no user PATH / profile), so point pi at the
# user's config dir (where the house-tap provider extension lives) and home, and
# config.toml [delegate] pi_path gives the absolute launcher.
& $nssm set $ServiceName AppEnvironmentExtra "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8" `
    "PI_CODING_AGENT_DIR=C:\Users\cmod\.pi\agent" "USERPROFILE=C:\Users\cmod" `
    "HOMEDRIVE=C:" "HOMEPATH=\Users\cmod"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout (Join-Path $RepoDir "workspace\logs\ide.out.log")
& $nssm set $ServiceName AppStderr (Join-Path $RepoDir "workspace\logs\ide.err.log")
& $nssm set $ServiceName AppRotateFiles 1

if ($NoStart) {
    Write-Host "Registered (NOT started). Start with: Start-Service $ServiceName"
} else {
    Start-Service $ServiceName
    Write-Host "Registered and started. Open http://127.0.0.1:8100 (config [ide] port)."
}
