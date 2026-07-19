"""Gated self-code-editing for eiDOS. OFF-LIMITS to self-editing.

The safety boundary: eiDOS PROPOSES, the operator-controlled dashboard APPLIES.

- eiDOS-side  `propose()` — stages a proposed FULL-FILE edit into workspace/proposals/ after
  validating the target (must be a tracked, non-off-limits .py) and compile-checking it. It
  never writes source, never commits, never restarts anything.
- Dashboard-side `apply()` (operator approval only) — re-validates, makes a PRE-APPLY git
  checkpoint (so `last_good` captures the known-good state), writes the file, and commits.
  The caller then restarts eiDOS. If the applied code crash-loops, the watchdog's
  auto-rollback restores `last_good` (= the pre-apply checkpoint) automatically.

Pragmatic accident-safety mode: real, git-reversible protection against bricking the source
— not a hard wall against a deliberately adversarial agent (that needs OS-level isolation).
"""

from __future__ import annotations

import difflib
import json
import os
import py_compile
import tempfile
import time
from pathlib import Path

from config import Config
from atomicio import replace_with_retry
import git_safety

OFF_LIMITS = git_safety.PROTECT_PATHS  # never self-editable (safety machinery + lifecycle)
MAX_PENDING = 5


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _editable_target(config: Config, target_file: str):
    """Validate + canonicalize a proposed target path. Returns (ok, rel_or_error)."""
    if not target_file or not isinstance(target_file, str):
        return False, "no target_file"
    t = target_file.strip().replace("\\", "/")
    if t.startswith("/") or t.startswith("~") or t.startswith("..") or ":" in t:
        return False, "target must be a repo-relative path (e.g. 'prompts.py')"
    repo = _repo_root()
    resolved = (repo / t).resolve()
    try:
        rel = resolved.relative_to(repo)
    except ValueError:
        return False, "target escapes the repo"
    rel_str = str(rel).replace("\\", "/")
    base = resolved.name
    if not base.endswith(".py"):
        return False, "only .py source files are self-editable"
    if base in OFF_LIMITS:
        return False, f"{base} is off-limits to self-editing"
    if "/workspace/" in ("/" + rel_str) or rel_str.startswith("workspace/"):
        return False, "workspace/ is runtime state, not source"
    if not resolved.exists():
        return False, f"{rel_str} does not exist (can only edit existing source)"
    if not git_safety._run_git(config, "ls-files", "--error-unmatch", "--", rel_str)["ok"]:
        return False, f"{rel_str} is not tracked by git"
    return True, rel_str


# --- proposal manifest store (workspace/proposals/<id>.json + .staged.py + .diff) ---

def _manifest_path(config: Config, pid: str) -> Path:
    return config.proposals_dir / f"{pid}.json"


def _load_manifest(config: Config, pid: str):
    try:
        return json.loads(_manifest_path(config, pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_manifest(config: Config, m: dict) -> None:
    config.proposals_dir.mkdir(parents=True, exist_ok=True)
    p = _manifest_path(config, m["id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, indent=2), encoding="utf-8")
    replace_with_retry(str(tmp), str(p))


def list_proposals(config: Config, kind: str | None = None) -> list[dict]:
    out = []
    d = config.proposals_dir
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if kind and m.get("kind") != kind:
            continue
        out.append(m)
    # newest first
    out.sort(key=lambda m: m.get("ts", ""), reverse=True)
    return out


def _compile_ok(new_content: str):
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        Path(tmp).write_text(new_content, encoding="utf-8")
        try:
            py_compile.compile(tmp, doraise=True)
            return True, ""
        except py_compile.PyCompileError as e:
            return False, str(e)[:300]
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def propose(config: Config, target_file: str, new_content: str, rationale: str = "", tick=None) -> dict:
    """eiDOS-side: stage a proposed full-file edit (validated + compile-checked). Never applies."""
    if not getattr(config, "self_edit_enabled", False):
        return {"ok": False, "error": "self-editing is disabled — Dean must enable it in config."}
    ok, rel = _editable_target(config, target_file)
    if not ok:
        return {"ok": False, "error": rel}
    if not isinstance(new_content, str) or not new_content.strip():
        return {"ok": False, "error": "new_content (the FULL new file) is required"}
    if len(new_content.encode("utf-8", "replace")) > int(getattr(config, "self_edit_max_proposal_bytes", 200000)):
        return {"ok": False, "error": "proposal too large"}
    pending = [m for m in list_proposals(config, kind="self_edit") if m.get("status") == "pending"]
    if len(pending) >= MAX_PENDING:
        return {"ok": False, "error": f"too many pending proposals ({MAX_PENDING}) — wait for Dean to review"}
    cok, cerr = _compile_ok(new_content)
    if not cok:
        return {"ok": False, "error": f"proposed code does not compile: {cerr}"}
    try:
        live = (_repo_root() / rel).read_text(encoding="utf-8")
    except OSError:
        live = ""
    diff = "".join(difflib.unified_diff(
        live.splitlines(keepends=True), new_content.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    if not diff.strip():
        return {"ok": False, "error": "no change versus the current file"}
    config.proposals_dir.mkdir(parents=True, exist_ok=True)
    base = "se_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    n = len(pending)
    pid = f"{base}_{n}"
    while _manifest_path(config, pid).exists() or (config.proposals_dir / f"{pid}.staged.py").exists():
        n += 1
        pid = f"{base}_{n}"
    (config.proposals_dir / f"{pid}.staged.py").write_text(new_content, encoding="utf-8")
    (config.proposals_dir / f"{pid}.diff").write_text(diff, encoding="utf-8")
    _save_manifest(config, {
        "id": pid, "kind": "self_edit", "target": rel, "rationale": (rationale or "")[:500],
        "base_sha": git_safety.current_sha(config), "status": "pending",
        "added": sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")),
        "removed": sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tick": tick,
    })
    return {"ok": True, "id": pid, "target": rel,
            "summary": f"Proposed edit to {rel} staged as {pid} for Dean to review. Not live until approved."}


def get_diff(config: Config, pid: str) -> dict:
    """Render the diff from the CURRENT live file (TOCTOU-safe) vs the staged content."""
    m = _load_manifest(config, pid)
    if not m:
        return {"ok": False, "error": "no such proposal"}
    try:
        new = (config.proposals_dir / f"{pid}.staged.py").read_text(encoding="utf-8")
    except OSError:
        return {"ok": False, "error": "staged content missing"}
    rel = m["target"]
    try:
        live = (_repo_root() / rel).read_text(encoding="utf-8")
    except OSError:
        live = ""
    diff = "".join(difflib.unified_diff(
        live.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return {"ok": True, "id": pid, "target": rel, "rationale": m.get("rationale", ""),
            "status": m.get("status"), "diff": diff,
            "stale": m.get("base_sha") != git_safety.current_sha(config)}


def reject(config: Config, pid: str, reason: str = "") -> dict:
    m = _load_manifest(config, pid)
    if not m:
        return {"ok": False, "error": "no such proposal"}
    m["status"] = "rejected"
    m["reject_reason"] = (reason or "")[:300]
    _save_manifest(config, m)
    return {"ok": True, "id": pid}


def apply(config: Config, pid: str) -> dict:
    """Dashboard-only (operator approval): checkpoint, write the file, commit. Caller restarts eidos.

    Makes a PRE-APPLY checkpoint first so `last_good` captures the known-good state — the
    watchdog will auto-restore it if the applied code crash-loops.
    """
    m = _load_manifest(config, pid)
    if not m:
        return {"ok": False, "error": "no such proposal"}
    if m.get("status") != "pending":
        return {"ok": False, "error": f"proposal is '{m.get('status')}', not pending"}
    rel = m["target"]
    ok, val = _editable_target(config, rel)
    if not ok:
        return {"ok": False, "error": f"target no longer valid: {val}"}
    try:
        new_content = (config.proposals_dir / f"{pid}.staged.py").read_text(encoding="utf-8")
    except OSError:
        return {"ok": False, "error": "staged content missing"}
    cok, cerr = _compile_ok(new_content)
    if not cok:
        return {"ok": False, "error": f"staged code does not compile: {cerr}"}
    # PRE-APPLY checkpoint — this becomes the rollback floor.
    cp = git_safety.make_checkpoint(config, f"pre self-edit {rel} ({pid})")
    if not cp.get("ok"):
        return {"ok": False, "error": f"pre-apply checkpoint failed: {cp.get('error')}"}
    prev_sha = git_safety.current_sha(config)
    try:
        (_repo_root() / rel).write_text(new_content, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"write failed: {e}"}
    git_safety._run_git(config, "add", "--", rel)
    git_safety._run_git(config, "commit", "-m",
                        f"self-edit apply {pid}: {rel} (prev {prev_sha[:9]})", "--no-verify")
    m["status"] = "applied"
    m["applied_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    m["prev_sha"] = prev_sha
    m["checkpoint"] = cp.get("tag")
    _save_manifest(config, m)
    return {"ok": True, "id": pid, "target": rel, "prev_sha": prev_sha,
            "checkpoint": cp.get("tag"),
            "message": f"Applied {rel}. Pre-apply checkpoint {cp.get('tag')} is the rollback floor."}


# ---------------------------------------------------------------------------
# Health-probe leg (the missing safety: a self-edit that BOOTS but misbehaves was
# invisible — the watchdog checked PID-exists only). The apply writes a pending_apply
# marker; the booting eidos drops an applied_ok breadcrumb (a paused eidos never ticks,
# so the heartbeat alone can't prove a healthy boot); the watchdog resolves or rolls back
# within self_edit_health_probe_s. State lives in state_dir (dashboard-owned).
# ---------------------------------------------------------------------------

def _pending_apply_path(config: Config) -> Path:
    return config.state_dir / "selfedit_pending_apply.json"


def _applied_ok_path(config: Config) -> Path:
    return config.state_dir / "selfedit_applied_ok.json"


def write_pending_apply(config: Config, pid: str, prev_sha: str,
                        baseline_heartbeat_ts: float, deadline_epoch: float) -> None:
    """Dashboard-side: record an in-flight apply so the watchdog can probe its health."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    p = _pending_apply_path(config)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "id": pid, "prev_sha": prev_sha,
        "baseline_heartbeat_ts": float(baseline_heartbeat_ts or 0),
        "deadline_epoch": float(deadline_epoch),
        "ts": time.time(),
    }), encoding="utf-8")
    replace_with_retry(str(tmp), str(p))
    # A fresh apply invalidates any stale breadcrumb from a prior one.
    try:
        _applied_ok_path(config).unlink()
    except OSError:
        pass


def read_pending_apply(config: Config):
    try:
        return json.loads(_pending_apply_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_pending_apply(config: Config) -> None:
    for p in (_pending_apply_path(config), _applied_ok_path(config)):
        try:
            p.unlink()
        except OSError:
            pass


def write_applied_ok(config: Config) -> None:
    """eidos-side breadcrumb: the new code IMPORTED and reached run_loop. Written early (before
    the boot-paused wait) because a paused eidos never produces a post-tick heartbeat. Carries
    the pending id so a stale breadcrumb from a previous apply can't satisfy a new probe."""
    pend = read_pending_apply(config)
    if not pend:
        return
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        p = _applied_ok_path(config)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": pend.get("id"), "ts": time.time()}), encoding="utf-8")
        replace_with_retry(str(tmp), str(p))
    except OSError:
        pass


def read_applied_ok(config: Config):
    try:
        return json.loads(_applied_ok_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def autorollback(config: Config, prev_sha: str, pid: str = "") -> dict:
    """Terminal: a self-edit failed its health probe. Revert ONLY the applied file to prev_sha —
    the target is known from the proposal manifest, so we don't need the whole-tree restore_to,
    which would also clobber any UNRELATED source change landed since prev_sha (the blast radius
    the old 'single-file safe' comment wrongly claimed). Fall back to the full non-PROTECT restore
    only when the target is unknown (no pid/manifest). Marks the proposal rolled_back."""
    m = _load_manifest(config, pid) if pid else None
    target = (m or {}).get("target")
    res = (git_safety.restore_file_to(config, target, prev_sha) if target
           else git_safety.restore_to(config, prev_sha))
    try:
        if m:
            m["status"] = "rolled_back"
            m["rolled_back_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_manifest(config, m)
    except Exception:  # noqa: BLE001
        pass
    clear_pending_apply(config)
    return res
