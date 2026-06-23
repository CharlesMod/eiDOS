<#
.SYNOPSIS
  Resolve the path to nssm.exe robustly, so service-install scripts don't depend on nssm being on PATH.

.DESCRIPTION
  Tries, in order: nssm on PATH (the normal case after `winget install NSSM.NSSM`), then the known
  eiDOS bin location where this machine's fleet of nssm services already live. Throws a clear,
  actionable error if neither resolves. Dot-source this and call Get-NssmPath:

      . (Join-Path $PSScriptRoot 'find-nssm.ps1')
      $nssm = Get-NssmPath
      & $nssm install ...
#>
function Get-NssmPath {
    $onPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source
    if ($onPath) { return $onPath }
    $candidates = @(
        "C:\Users\cmod\llm\bin\nssm\nssm.exe",                       # this machine's shared nssm
        (Join-Path $env:USERPROFILE "llm\bin\nssm\nssm.exe")          # same, user-relative
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    throw ("nssm not found on PATH or known locations. Install it (winget install NSSM.NSSM) " +
           "or add its folder to PATH, then re-run.")
}
