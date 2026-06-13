"""Re-initialize eiDOS to a clean Level-0 state.

What it does (in order):
  1. Stops eidos and DISARMS the watchdog (so it won't respawn).
  2. Reaps eidos's background jobs — they detach and would otherwise hold workspace files open
     (the "device busy" trap) and keep running.
  3. Archives the current workspace (so a run is never truly lost) unless --no-archive.
  4. Rebuilds a clean workspace, KEEPING goal.md (mission) + self_guide.md (Dean's directives).
  5. Re-seeds the bootstrap knowledge nuggets unless --no-seed.
  6. Leaves eidos STOPPED — you start it from the dashboard when the GPU is free.

Safe by default: run with no flags for a DRY RUN (prints the plan); add --yes to actually do it.

Usage:
    python reset_eidos.py                 # dry run — shows what it would do
    python reset_eidos.py --yes           # do it (archive + reseed, keep goal/self_guide)
    python reset_eidos.py --yes --no-archive   # skip the archive (faster, no preservation)
    python reset_eidos.py --yes --no-seed      # truly empty knowledge (no bootstrap nuggets)
    python reset_eidos.py --yes --reset-guide  # also reset self_guide.md to the seed default
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

KDIR = Path(__file__).resolve().parent
sys.path.insert(0, str(KDIR))

from config import load_config  # noqa: E402

KEEP_FILES = {"goal.md", "self_guide.md"}  # the "config": mission + Dean's standing directives

DEFAULT_SELF_GUIDE = """\
# eiDOS self-guide — standing directives from Boss

## How to address Boss
- Call him **Boss** (Dean). Warm, brief, natural.

## How to work
- Reuse your existing skills (call them by name) instead of authoring near-duplicates.
- Store what you discover with `memorize`; don't build your own JSON memory.
- If you're blocked waiting on Boss, ask ONCE then switch to other useful work.
"""


def _alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True)
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False if isinstance(sys.exc_info()[1], ProcessLookupError) else True


def _eidos_pids() -> list:
    """Every live eidos.py PID — by command line, not just eidos.pid. The watchdog
    can leave stale/duplicate instances that a single-pid kill misses; those keep
    ticking and rewrite persona.json/creature.json right after we clear them.
    The '\\eidos.py' match (backslash before the name) excludes reset_eidos.py itself."""
    if os.name == "nt":
        # Single-quoted only — escaping double quotes through -Command silently
        # breaks the query (returns nothing → the kill misses every instance).
        # Name test lives in Where-Object so we never match the powershell host.
        ps = (r"Get-CimInstance Win32_Process "
              r"| Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*\eidos.py*' } "
              r"| Select-Object -ExpandProperty ProcessId")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True)
    else:
        out = subprocess.run(["pgrep", "-f", r"/eidos\.py"],
                             capture_output=True, text=True)
    return [int(x) for x in (out.stdout or "").split() if x.strip().isdigit()]


def stop_eidos(config) -> None:
    """Disarm the watchdog, reap background jobs, then kill the eidos process tree."""
    ws = config.workspace
    # 1. disarm watchdog FIRST so it never respawns mid-reset
    for f in ("eidos.should_run",):
        try:
            (ws / f).unlink()
        except OSError:
            pass
    # 2. reap tracked background jobs (releases workspace file handles; stops detached loops)
    try:
        from tools import reap_jobs
        n = reap_jobs(config, kill_all=True)
        print(f"  reaped {n} background job(s)")
    except Exception as e:  # noqa: BLE001
        print(f"  (reap skipped: {e})")
    # 3. kill EVERY eidos.py process (not just eidos.pid). Loop until none remain
    #    or we time out — a writer surviving the clear is what corrupts a wipe.
    killed = []
    deadline = time.time() + 12
    while True:
        pids = _eidos_pids()
        if not pids:
            break
        for pid in pids:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            killed.append(pid)
        time.sleep(0.5)
        if time.time() > deadline:
            break
    leftover = _eidos_pids()
    if killed:
        print(f"  killed eidos pid(s): {sorted(set(killed))}")
    else:
        print("  eidos was not running")
    if leftover:
        # Refuse to clear the workspace while a live eidos can rewrite it.
        raise SystemExit(f"ABORT: eidos still alive after kill: {leftover}. "
                         f"Workspace NOT cleared. Kill them manually and retry.")
    # let handles release before we touch the workspace
    time.sleep(1.5)


def main():
    ap = argparse.ArgumentParser(description="Re-initialize eiDOS to a clean Level-0 state.")
    ap.add_argument("--yes", action="store_true", help="actually do it (otherwise dry run)")
    ap.add_argument("--no-archive", action="store_true", help="don't archive the old workspace")
    ap.add_argument("--no-seed", action="store_true", help="don't re-seed bootstrap knowledge")
    ap.add_argument("--reset-guide", action="store_true", help="also reset self_guide.md to default")
    args = ap.parse_args()

    config = load_config(str(KDIR / "config.toml"))
    ws = config.workspace
    ts = time.strftime("%Y%m%d_%H%M%S")
    archive = KDIR.parent / f"eidos_ws_pre-reset_{ts}"

    kept = sorted(KEEP_FILES - ({"self_guide.md"} if args.reset_guide else set()))
    print("eiDOS reset plan:")
    print(f"  workspace:   {ws}")
    print(f"  archive:     {'(skipped)' if args.no_archive else archive}")
    print(f"  keep:        {', '.join(kept)}")
    print(f"  reseed:      {'no' if args.no_seed else 'yes (bootstrap nuggets)'}")
    print(f"  self_guide:  {'RESET to default' if args.reset_guide else 'kept'}")
    print("  eidos after: STOPPED (start it yourself from the dashboard)")
    if not args.yes:
        print("\nDRY RUN — re-run with --yes to execute.")
        return

    print("\nStopping eidos…")
    stop_eidos(config)

    if not args.no_archive and ws.exists():
        print(f"Archiving workspace → {archive} …")
        try:
            shutil.copytree(ws, archive, dirs_exist_ok=True)
            print("  archived")
        except Exception as e:  # noqa: BLE001
            print(f"  archive WARNING (continuing): {e}")

    # Clear workspace contents, keeping the config files.
    print("Clearing workspace to Level 0…")
    keep = KEEP_FILES - ({"self_guide.md"} if args.reset_guide else set())
    if ws.exists():
        for item in ws.iterdir():
            if item.name in keep:
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
            except OSError as e:
                print(f"  could not remove {item.name}: {e}")
    ws.mkdir(parents=True, exist_ok=True)

    if args.reset_guide:
        (ws / "self_guide.md").write_text(DEFAULT_SELF_GUIDE, encoding="utf-8")
        print("  self_guide.md reset to default")

    if not args.no_seed:
        print("Re-seeding bootstrap knowledge…")
        r = subprocess.run([sys.executable, str(KDIR / "seed_knowledge.py")],
                           cwd=str(KDIR), env={**os.environ, "PYTHONUTF8": "1"},
                           capture_output=True, text=True)
        print("  " + (r.stdout or r.stderr or "").strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else "  (seed produced no output)")

    # Verify
    persona = (ws / "persona.json").exists()
    obs = ws / "observations.jsonl"
    skills_n = len([p for p in (ws / "skills").glob("*.py")]) if (ws / "skills").exists() else 0
    print("\nClean Level-0 state:")
    print(f"  persona: {'PRESENT (!)' if persona else 'absent → boots Lv.0'}")
    print(f"  observations: {sum(1 for _ in obs.open()) if obs.exists() else 0} | skills: {skills_n}")
    print(f"  goal.md: {'Y' if (ws / 'goal.md').exists() else 'N'} | self_guide.md: {'Y' if (ws / 'self_guide.md').exists() else 'N'}")
    print("\nDone. eiDOS is STOPPED — start it from the dashboard (Start → Go) when the GPU is free.")


if __name__ == "__main__":
    main()
