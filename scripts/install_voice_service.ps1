<#
.SYNOPSIS
  Register the eiDOS voice service (GLaDOS TTS + GPU speech-gate) as a Windows nssm service.

.DESCRIPTION
  Phase 8.3 split the voice pipeline out of dashboard.py into its own process (voice.py) so a
  native TTS/ffmpeg crash can't take the watchdog down. This registers it as "EidosVoice"
  on config.voice_port (default 8098), separate from the dashboard (8099).

  Manual launch (no service needed):
      $env:PYTHONUTF8=1; python voice.py --config config.toml

  CUTOVER NOTE -- do NOT start this while the live v1 dashboard still owns voice. Two voice services
  would both shell out to Chatterbox (8004) and fight the GPU. Register now (-NoStart, the default),
  then start it as part of the v2 -> live cutover, AFTER stopping v1's voice path. This script does
  NOT auto-start the service; flip it on with `Start-Service EidosVoice` when ready.

.PARAMETER RepoDir
  The eiDOS repo the service runs from (default: this script's parent's parent).

.PARAMETER Python
  The python.exe to use (default: the repo's venv, falling back to the shared Kairos venv).

.PARAMETER ServiceName
  nssm service name (default: EidosVoice).

.PARAMETER NoStart
  Register but do not start (DEFAULT true -- see cutover note). Pass -NoStart:$false to start now.
#>
param(
    [string]$RepoDir     = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)),
    [string]$Python      = "",
    [string]$ServiceName = "EidosVoice",
    [switch]$NoStart     = $true
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

$voice = Join-Path $RepoDir "voice.py"
if (-not (Test-Path $voice)) { throw "voice.py not found at $voice" }

Write-Host "Registering '$ServiceName'"
Write-Host "  python : $Python"
Write-Host "  script : $voice"
Write-Host "  workdir: $RepoDir"

# Remove a stale registration so this is idempotent.
$existing = & $nssm status $ServiceName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  (service exists -- removing and re-creating)"
    & $nssm stop $ServiceName 2>$null | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
}

& $nssm install $ServiceName $Python $voice "--config" "config.toml"
& $nssm set $ServiceName AppDirectory $RepoDir
& $nssm set $ServiceName AppEnvironmentExtra "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout (Join-Path $RepoDir "workspace\logs\voice.out.log")
& $nssm set $ServiceName AppStderr (Join-Path $RepoDir "workspace\logs\voice.err.log")
& $nssm set $ServiceName AppRotateFiles 1

if ($NoStart) {
    Write-Host "Registered (NOT started). Start with: Start-Service $ServiceName   (after the cutover note above)"
} else {
    Start-Service $ServiceName
    Write-Host "Registered and started."
}
