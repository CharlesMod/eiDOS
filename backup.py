"""Workspace backups — life insurance for the individual (Pillars Phase 0.4).

eiDOS runs on FROZEN weights: the creature's individuality — its episodes, learned knowledge,
persona/XP, skills, self-guide — lives entirely in the `workspace/` directory. That artifact layer
IS the person, and it has been lost to a wipe once already (see PILLARS_PLAN.md §8 pitfall #13).
This module snapshots `workspace/` to a rotated set of timestamped tarballs and, crucially, provides
a RESTORE-VERIFY routine so a snapshot is proven recoverable, not merely written.

Design:
  - `snapshot(config)`  → tar.gz of workspace/, EXCLUDING regenerable caches (embedding vectors,
                          __pycache__, lifecycle markers). Written atomically (tmp → replace) into
                          `workspace-backups/` beside the workspace, then rotation prunes old ones.
  - `verify(config, path)` → unpack to a temp dir and validate integrity: the tar extracts cleanly
                          AND the critical files parse (JSON loads for persona/skills/knowledge index;
                          *.jsonl line-parse). Returns a structured VerifyResult(ok, reasons, checks).
  - rotation keeps daily × `pillars_backup_daily_keep` and weekly × `pillars_backup_weekly_keep`.

Standalone CLI (runnable NOW; sleep-job/quest wiring is a later phase — NOT wired into eidos.py):
    python backup.py snapshot [--config config.toml]
    python backup.py verify   [--config config.toml] [path]   # path defaults to newest snapshot
    python backup.py list     [--config config.toml]

Taking the wall-clock time inside this standalone module is fine — it is not the resume-sensitive
tick loop, so a normal time.strftime() for the snapshot timestamp is correct here.
"""

import argparse
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomicio import replace_with_retry
from config import Config, load_config

logger = logging.getLogger("eidos.backup")

# Directory (sibling of workspace/, NOT inside it) that holds the rotated tarballs. Keeping it outside
# the workspace means a backup never snapshots the previous backups, and a workspace wipe/reset that
# clears workspace/ leaves the tarballs standing.
BACKUP_DIRNAME = "workspace-backups"
SNAPSHOT_PREFIX = "workspace-"
SNAPSHOT_SUFFIX = ".tar.gz"
# strftime pattern for the timestamp segment, and the strptime pattern to read it back for rotation.
TS_FMT = "%Y%m%d-%H%M%S"
_TS_LEN = len(time.strftime(TS_FMT, time.localtime(0)))  # width of a rendered timestamp (15 chars)

# Rotation-keep defaults, mirroring config.Config.pillars_backup_{daily,weekly}_keep. Read via
# _keep_config() with getattr fallback so this module works whether or not the running Config carries
# the pillars_backup_* fields yet (they land with the Pillars config wiring on the same milestone).
_DEFAULT_DAILY_KEEP = 14
_DEFAULT_WEEKLY_KEEP = 8

# --- What is EXCLUDED from a snapshot (regenerable / meaningless-in-a-restore) ---
# Directory basenames pruned wherever they appear in the tree.
EXCLUDE_DIR_NAMES = frozenset({
    "__pycache__",          # python bytecode
})
# Individual file paths RELATIVE to the workspace root that are caches or lifecycle markers.
#   knowledge/vectors.npy, knowledge/vector_ids.json — the embedding vector store; rebuilt from the
#     knowledge entries on demand (embedding.py). Big and trivially regenerable.
#   eidos.pid / eidos.should_run / eidos_spawn.ts — process + watchdog lifecycle markers; a copy
#     restored elsewhere must NOT carry a stale live-process pid or an "arm the watchdog" flag.
#   skills/.dryrun.py — the transient skill dry-run harness (rewritten every compile check).
EXCLUDE_REL_FILES = frozenset({
    "knowledge/vectors.npy",
    "knowledge/vector_ids.json",
    "eidos.pid",
    "eidos.should_run",
    "eidos_spawn.ts",
    "skills/.dryrun.py",
})
# File basename suffixes pruned wherever they appear (compiled python bytecode).
EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo")

# --- Critical files the restore-verify insists must parse (relative to workspace root) ---
# JSON files: must json.load() cleanly. Each is checked ONLY if present in the snapshot (a young
# creature may not have earned a persona/skills index yet — absence is fine, corruption is not).
VERIFY_JSON_FILES = (
    "persona.json",
    "skills/_index.json",
    "knowledge/index.json",
)
# JSONL files: every non-blank line must json.loads(). These carry the lived history.
VERIFY_JSONL_FILES = (
    "episodes.jsonl",
    "observations.jsonl",
)


def backup_dir(config: Config) -> Path:
    """The rotated-tarballs directory — a SIBLING of the workspace, not inside it."""
    return config.workspace.parent / BACKUP_DIRNAME


def _keep_config(config: Config) -> tuple:
    """(daily_keep, weekly_keep) from config, falling back to module defaults if the fields are
    absent on this Config (pre-Pillars-config-wiring)."""
    daily = getattr(config, "pillars_backup_daily_keep", _DEFAULT_DAILY_KEEP)
    weekly = getattr(config, "pillars_backup_weekly_keep", _DEFAULT_WEEKLY_KEEP)
    return int(daily), int(weekly)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _is_excluded(rel_posix: str, name: str) -> bool:
    """True if a workspace-relative path (posix form) is a cache/marker we never snapshot."""
    if rel_posix in EXCLUDE_REL_FILES:
        return True
    parts = rel_posix.split("/")
    if any(p in EXCLUDE_DIR_NAMES for p in parts):
        return True
    if name.endswith(EXCLUDE_FILE_SUFFIXES):
        return True
    return False


def _snapshot_filter(arcroot: str):
    """Return a tarfile `filter=` callable that drops excluded members and normalizes ownership."""

    def _filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        # tarinfo.name is "<arcroot>/<rel>"; strip the leading arcroot segment to get the
        # workspace-relative path. The root member itself (name == arcroot) is kept.
        name = tarinfo.name
        rel = name.split("/", 1)[1] if "/" in name else ""
        if rel and _is_excluded(rel, Path(rel).name):
            return None
        # Deterministic ownership so a restore on another box/user is clean.
        tarinfo.uid = tarinfo.gid = 0
        tarinfo.uname = tarinfo.gname = ""
        return tarinfo

    return _filter


def snapshot(config: Config, dest_dir: Optional[Path] = None) -> Path:
    """Write a timestamped tar.gz of `workspace/` (excluding caches) and prune old snapshots.

    Returns the path to the new snapshot. Written to a `.partial` temp file first, then atomically
    moved into place so a crash mid-write never leaves a half-tarball that verify would trust.
    """
    ws = config.workspace
    if not ws.exists():
        raise FileNotFoundError(f"workspace does not exist: {ws}")
    out_dir = Path(dest_dir) if dest_dir is not None else backup_dir(config)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime(TS_FMT, time.localtime())
    final = out_dir / f"{SNAPSHOT_PREFIX}{ts}{SNAPSHOT_SUFFIX}"
    # If a snapshot with this exact second already exists, disambiguate rather than clobber.
    n = 1
    while final.exists():
        final = out_dir / f"{SNAPSHOT_PREFIX}{ts}-{n}{SNAPSHOT_SUFFIX}"
        n += 1

    arcroot = ws.name  # snapshots restore as "<workspace-name>/..." — self-describing
    tmp = out_dir / (final.name + ".partial")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(str(ws), arcname=arcroot, filter=_snapshot_filter(arcroot))
        replace_with_retry(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    logger.info("backup: wrote snapshot %s (%d bytes)", final.name, final.stat().st_size)
    pruned = prune(config, dest_dir=out_dir)
    if pruned:
        logger.info("backup: pruned %d old snapshot(s)", len(pruned))
    return final


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

@dataclass
class _Snap:
    path: Path
    ts: time.struct_time
    epoch: float

    @property
    def day_key(self) -> str:
        return time.strftime("%Y%m%d", self.ts)

    @property
    def week_key(self) -> str:
        # ISO year-week — one bucket per calendar week.
        return time.strftime("%G-W%V", self.ts)


def _parse_ts(name: str) -> Optional[time.struct_time]:
    """Extract the timestamp struct from a snapshot filename, or None if it doesn't match."""
    if not (name.startswith(SNAPSHOT_PREFIX) and name.endswith(SNAPSHOT_SUFFIX)):
        return None
    core = name[len(SNAPSHOT_PREFIX):-len(SNAPSHOT_SUFFIX)]
    # Drop any "-N" disambiguation suffix before parsing the timestamp head.
    stamp = core[:_TS_LEN]
    try:
        return time.strptime(stamp, TS_FMT)
    except ValueError:
        return None


def list_snapshots(config: Config, dest_dir: Optional[Path] = None) -> List[_Snap]:
    """All valid snapshots in the backup dir, NEWEST first."""
    out_dir = Path(dest_dir) if dest_dir is not None else backup_dir(config)
    snaps: List[_Snap] = []
    if not out_dir.exists():
        return snaps
    for p in out_dir.iterdir():
        if not p.is_file():
            continue
        ts = _parse_ts(p.name)
        if ts is None:
            continue
        snaps.append(_Snap(path=p, ts=ts, epoch=time.mktime(ts)))
    snaps.sort(key=lambda s: s.epoch, reverse=True)
    return snaps


def _keep_set(snaps: List[_Snap], daily_keep: int, weekly_keep: int) -> set:
    """Decide which snapshot paths to RETAIN under daily×N / weekly×M rotation.

    Newest-first, take the newest snapshot per distinct day for up to `daily_keep` days, and the
    newest per distinct ISO-week for up to `weekly_keep` weeks. A snapshot kept for either reason
    survives; everything else is prunable. Always keep the single newest snapshot regardless.
    """
    keep = set()
    if not snaps:
        return keep
    keep.add(snaps[0].path)  # never prune the most recent

    seen_days: List[str] = []
    seen_weeks: List[str] = []
    day_first = {}   # day_key -> newest snap that day
    week_first = {}  # week_key -> newest snap that week
    for s in snaps:  # already newest-first
        if s.day_key not in day_first:
            day_first[s.day_key] = s
            seen_days.append(s.day_key)
        if s.week_key not in week_first:
            week_first[s.week_key] = s
            seen_weeks.append(s.week_key)
    for dk in seen_days[:max(0, daily_keep)]:
        keep.add(day_first[dk].path)
    for wk in seen_weeks[:max(0, weekly_keep)]:
        keep.add(week_first[wk].path)
    return keep


def prune(config: Config, dest_dir: Optional[Path] = None) -> List[Path]:
    """Delete snapshots outside the daily×N / weekly×M retention. Returns the removed paths."""
    snaps = list_snapshots(config, dest_dir=dest_dir)
    daily, weekly = _keep_config(config)
    keep = _keep_set(snaps, daily, weekly)
    removed: List[Path] = []
    for s in snaps:
        if s.path in keep:
            continue
        try:
            s.path.unlink()
            removed.append(s.path)
        except OSError as e:  # noqa: BLE001 — a stuck delete must not abort the snapshot
            logger.warning("backup: could not prune %s: %s", s.path.name, e)
    return removed


# ---------------------------------------------------------------------------
# Restore-verify
# ---------------------------------------------------------------------------

@dataclass
class VerifyResult:
    ok: bool
    path: str
    reasons: List[str] = field(default_factory=list)   # failure reasons (empty on pass)
    checks: List[str] = field(default_factory=list)    # human-readable checks performed / passed

    def __str__(self) -> str:
        head = f"{'PASS' if self.ok else 'FAIL'}  {self.path}"
        lines = [head]
        for c in self.checks:
            lines.append(f"  ok:   {c}")
        for r in self.reasons:
            lines.append(f"  FAIL: {r}")
        return "\n".join(lines)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract, refusing any member that would escape `dest` (path-traversal / absolute paths)."""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not (target == dest or str(target).startswith(str(dest) + os.sep)):
            raise ValueError(f"unsafe member path escapes extract dir: {member.name!r}")
    # filter="data" (py3.12+) sanitizes member metadata; guarded above for older pythons too.
    try:
        tar.extractall(dest, filter="data")  # noqa: S202 — path-guarded above
    except TypeError:
        tar.extractall(dest)  # noqa: S202 — pre-3.12 has no filter kwarg


def verify(config: Config, path: Optional[Path] = None) -> VerifyResult:
    """Unpack a snapshot to a temp dir and validate integrity. Returns a structured pass/fail.

    Pass requires: (1) the tarball opens and extracts cleanly, and (2) every present critical file
    parses — JSON files json.load(), JSONL files line-parse. Missing critical files are allowed (a
    young creature may not have them yet); PRESENT-but-corrupt files fail with a clear reason.
    """
    if path is None:
        snaps = list_snapshots(config)
        if not snaps:
            return VerifyResult(ok=False, path="(none)", reasons=["no snapshots found to verify"])
        path = snaps[0].path
    path = Path(path)
    result = VerifyResult(ok=True, path=str(path))

    if not path.exists():
        return VerifyResult(ok=False, path=str(path), reasons=["snapshot file does not exist"])

    tmp = Path(tempfile.mkdtemp(prefix="eidos-verify-"))
    try:
        # 1. Extraction.
        try:
            with tarfile.open(path, "r:gz") as tar:
                _safe_extract(tar, tmp)
        except (tarfile.TarError, OSError, EOFError, ValueError) as e:
            result.ok = False
            result.reasons.append(f"tar extract failed: {e}")
            return result
        result.checks.append("tar extracted cleanly")

        # Locate the single arcroot dir the snapshot restores into.
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        if not roots:
            result.ok = False
            result.reasons.append("snapshot contained no workspace directory")
            return result
        ws = roots[0]

        # 2. Critical JSON files parse (if present).
        for rel in VERIFY_JSON_FILES:
            f = ws / rel
            if not f.exists():
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    json.load(fh)
                result.checks.append(f"{rel} parses as JSON")
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                result.ok = False
                result.reasons.append(f"{rel} failed JSON parse: {e}")

        # 3. Critical JSONL files line-parse (if present).
        for rel in VERIFY_JSONL_FILES:
            f = ws / rel
            if not f.exists():
                continue
            bad = _verify_jsonl(f)
            if bad is None:
                result.checks.append(f"{rel} line-parses as JSONL")
            else:
                result.ok = False
                result.reasons.append(f"{rel} bad JSONL at line {bad[0]}: {bad[1]}")

        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _verify_jsonl(path: Path) -> Optional[tuple]:
    """Return None if every non-blank line json-parses, else (line_no, error_message)."""
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as e:
                    return (i, str(e))
    except (OSError, UnicodeDecodeError) as e:
        return (0, str(e))
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_list(config: Config) -> int:
    snaps = list_snapshots(config)
    if not snaps:
        print(f"No snapshots in {backup_dir(config)}")
        return 0
    daily, weekly = _keep_config(config)
    keep = _keep_set(snaps, daily, weekly)
    print(f"{len(snaps)} snapshot(s) in {backup_dir(config)}:")
    for s in snaps:
        size = s.path.stat().st_size
        flag = "keep " if s.path in keep else "prune"
        print(f"  [{flag}] {s.path.name}  ({size / 1024:.1f} KB)")
    return 0


def _cmd_snapshot(config: Config) -> int:
    p = snapshot(config)
    print(f"Snapshot written: {p}  ({p.stat().st_size / 1024:.1f} KB)")
    return 0


def _cmd_verify(config: Config, path: Optional[str]) -> int:
    result = verify(config, Path(path) if path else None)
    print(result)
    return 0 if result.ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="eiDOS workspace backup: snapshot / verify / list.")
    ap.add_argument("cmd", choices=("snapshot", "verify", "list"))
    ap.add_argument("path", nargs="?", default=None,
                    help="verify: snapshot to check (default: newest)")
    ap.add_argument("--config", default="config.toml", help="config path (default: config.toml)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config(args.config)

    if args.cmd == "snapshot":
        return _cmd_snapshot(config)
    if args.cmd == "verify":
        return _cmd_verify(config, args.path)
    if args.cmd == "list":
        return _cmd_list(config)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
