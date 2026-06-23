"""Platform shell layer — run the model's tool commands in the native shell of whatever OS eiDOS is on.

eiDOS started life Windows-only: every `bash` tool call was routed through PowerShell (with a
flag-translation linter that rewrites the model's Unix-isms into PowerShell). That's correct on
Windows, but on macOS / Linux / Raspberry Pi the model's commands are ALREADY native — they must run
verbatim through `bash`/`sh`, and the PowerShell translation must never touch them.

This module is the single seam that decides, per platform:
  - Windows + creature-in-WSL  -> the existing WSL invocation (real Linux inside Windows),
  - Windows otherwise          -> the existing PowerShell list-form routing,
  - POSIX (mac/Linux/Pi)        -> `bash -lc <cmd>` (or `sh -c` if bash is absent), list form so we
                                   choose the shell and can attach the exit-code sidecar symmetrically.

It also owns the three exit-code "epilogues" (record the real exit code to a sidecar file so async jobs
report a truthful exit code after the Popen handle is gone), factored here so every Popen site shares
identical logic. The Windows/WSL routing functions still live in tools.py (they're PowerShell-specific);
build_shell_command imports them lazily to avoid a tools<->platform_shell import cycle.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def posix_shell(config=None) -> list:
    """The POSIX shell argv prefix: prefer bash with a login shell (`-lc`) so the user's PATH/aliases
    load (a friend's `ollama`, `node`, etc. on PATH just work), falling back to `/bin/sh -c` if bash is
    absent (minimal containers). `posix_shell_login=false` drops `-lc`->`-c` if a slow rc file bites."""
    login = True if config is None else bool(getattr(config, "posix_shell_login", True))
    bash = shutil.which("bash") or ("/bin/bash" if Path("/bin/bash").exists() else None)
    if bash:
        return [bash, "-lc"] if login else [bash, "-c"]
    return [shutil.which("sh") or "/bin/sh", "-c"]


def _posix_epilogue(popen_arg: list, exit_path: str) -> list:
    """bash/sh: append `; record $? to the sidecar; exit $?` so an async POSIX job's real exit code is
    readable after the handle is gone (parity with the WSL/PowerShell sidecars)."""
    ep = str(exit_path).replace("'", "'\\''")
    tail = popen_arg[-1] + "\n__ec=$?; printf '%s' \"$__ec\" > '" + ep + "'; exit $__ec"
    return popen_arg[:-1] + [tail]


def _ps_epilogue(popen_arg: list, exit_path: str) -> list:
    """PowerShell list-form: record the real exit code to the sidecar and re-raise it via `exit` so
    proc.returncode stays truthful. (Moved verbatim from tools.py.)"""
    ep = str(exit_path).replace("'", "''")
    tail = (popen_arg[-1]
            + "\n$__eidos_ec = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 }"
            + "\n[System.IO.File]::WriteAllText('" + ep + "', [string]$__eidos_ec)"
            + "\nexit $__eidos_ec")
    return popen_arg[:-1] + [tail]


def build_shell_command(cmd: str, config):
    """Decide how to run `cmd` on this platform.

    Returns (popen_arg, use_shell, epilogue) where:
      - popen_arg : the argv list (or raw string when use_shell is True),
      - use_shell : whether to pass shell=True to Popen,
      - epilogue  : a callable(popen_arg, exit_path) -> popen_arg that appends the exit-code sidecar,
                    or None when no sidecar applies (e.g. an operator `cmd.exe` / `powershell -File`).
    The caller owns the actual Popen + sidecar wiring; this just picks the shell."""
    import tools as _t  # lazy: avoids the tools <-> platform_shell import cycle

    # Windows, creature running its bash inside WSL2 — real Linux, no PowerShell in the path.
    if _t._creature_uses_wsl(config):
        arg = _t._wsl_popen(cmd, config)

        def _wsl_ep(popen_arg, exit_path):
            wep = _t._win_to_wsl_path(str(exit_path))
            tail = popen_arg[-1] + "\n__ec=$?; printf '%s' \"$__ec\" > '" + wep + "'; exit $__ec"
            return popen_arg[:-1] + [tail]

        return arg, False, _wsl_ep

    # Windows, house-AI — PowerShell IS the shell (with the Unix->PS flag translation in tools.py).
    if IS_WINDOWS:
        arg, use_shell = _t._route_windows_command(cmd)
        epilogue = _ps_epilogue if (not use_shell and isinstance(arg, list)) else None
        return arg, use_shell, epilogue

    # POSIX (macOS / Linux / Raspberry Pi) — run the model's command verbatim in bash/sh. No PowerShell
    # translation ever reaches here (it's gated in tools.py and guarded in _route/_lint as well).
    return [*posix_shell(config), cmd], False, _posix_epilogue
