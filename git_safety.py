"""Emergency git tracking + restore for eiDOS self-modification.

OFF-LIMITS to self-editing. Imported and driven ONLY by the operator-controlled dashboard
process — never exposed as an eiDOS tool. eiDOS can propose code changes, but only the
dashboard checkpoints, applies, and restores them, so every change is git-reversible.

Pragmatic accident-safety mode: this is a real, working safety net against eiDOS (or us)
bricking the source — NOT a hard wall against a deliberately adversarial agent (that would
need OS-level account/ACL isolation, deferred by design).

Checkpoints are git commits + lightweight `eidos-good-<ts>` tags scoped to the source tree
(workspace/ runtime state is excluded). Restore checks those tags back out. Nothing here
ever pushes to a remote or runs `git reset --hard`.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from config import Config
from atomicio import replace_with_retry

# Files self-editing may NEVER touch (the safety machinery + lifecycle owner). Also enforced
# at the self-edit propose/apply layer; duplicated here as the git-side backstop.
PROTECT_PATHS = frozenset({
    "dashboard.py", "git_safety.py", "selfedit.py", "safety.py", "atomicio.py",
    "config.py", "config.toml", ".gitignore", "llm.py", "skills.py",
})

_TAG_PREFIX = "eidos-good-"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _run_git(config: Config, *args: str, timeout: int = 30) -> dict:
    """Run a git command in the repo (no shell, no network). Never raises."""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(_repo_root()),
            capture_output=True, text=True, timeout=timeout, shell=False,
        )
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": (p.stdout or "").strip(), "err": (p.stderr or "").strip()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": -1, "out": "", "err": f"{type(e).__name__}: {e}"}


def current_sha(config: Config) -> str:
    r = _run_git(config, "rev-parse", "HEAD")
    return r["out"] if r["ok"] else ""


def _last_good_path(config: Config) -> Path:
    return config.state_dir / "last_good"


def read_last_good(config: Config) -> str:
    try:
        return _last_good_path(config).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_last_good(config: Config, tag: str) -> None:
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = _last_good_path(config).with_suffix(".tmp")
        tmp.write_text(tag, encoding="utf-8")
        replace_with_retry(str(tmp), str(_last_good_path(config)))
    except OSError:
        pass


def is_git_repo(config: Config) -> bool:
    return _run_git(config, "rev-parse", "--is-inside-work-tree").get("out") == "true"


def make_checkpoint(config: Config, label: str = "") -> dict:
    """Commit the current SOURCE state (workspace/ excluded) and tag it as a good point.

    Captures all tracked source so a later restore returns the tree to here. Returns
    {ok, tag, sha, message}. Best-effort: an empty commit (nothing changed) still tags HEAD.
    """
    if not getattr(config, "git_safety_enabled", True):
        return {"ok": False, "error": "git safety disabled"}
    if not is_git_repo(config):
        return {"ok": False, "error": "not a git repository"}
    # Stage source changes but never runtime workspace state.
    _run_git(config, "add", "-A", "--", ".", ":(exclude)workspace")
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    label = (label or "checkpoint").replace("\n", " ")[:80]
    msg = f"eidos checkpoint: {label} [{ts}]"
    # Commit if there's something staged; otherwise just tag current HEAD.
    status = _run_git(config, "diff", "--cached", "--quiet")
    if not status["ok"]:  # non-zero => staged changes exist
        c = _run_git(config, "commit", "-m", msg, "--no-verify")
        if not c["ok"] and "nothing to commit" not in (c["err"] + c["out"]).lower():
            return {"ok": False, "error": f"commit failed: {c['err'] or c['out']}"}
    tag = f"{_TAG_PREFIX}{ts}"
    t = _run_git(config, "tag", "-f", tag)
    if not t["ok"]:
        return {"ok": False, "error": f"tag failed: {t['err']}"}
    _write_last_good(config, tag)
    prune_checkpoints(config, keep=int(getattr(config, "git_checkpoint_keep", 30)))
    return {"ok": True, "tag": tag, "sha": current_sha(config), "message": msg}


def list_checkpoints(config: Config, n: int = 30) -> list[dict]:
    """Recent eidos-good-* tags, newest first, with their subject + relative time."""
    r = _run_git(config, "tag", "--list", f"{_TAG_PREFIX}*", "--sort=-creatordate",
                 "--format=%(refname:short)\t%(creatordate:relative)\t%(subject)")
    if not r["ok"] or not r["out"]:
        return []
    out = []
    for line in r["out"].splitlines()[:n]:
        parts = line.split("\t")
        out.append({"tag": parts[0],
                    "when": parts[1] if len(parts) > 1 else "",
                    "subject": parts[2] if len(parts) > 2 else ""})
    return out


def prune_checkpoints(config: Config, keep: int = 30) -> int:
    tags = [c["tag"] for c in list_checkpoints(config, n=10000)]
    active = read_last_good(config)
    removed = 0
    for tag in tags[keep:]:
        if tag == active:
            continue  # never prune the active restore floor
        if _run_git(config, "tag", "-d", tag)["ok"]:
            removed += 1
    return removed


def git_log_summary(config: Config, n: int = 15) -> dict:
    r = _run_git(config, "log", "-n", str(n), "--pretty=%h\t%cr\t%s")
    commits = []
    if r["ok"] and r["out"]:
        for line in r["out"].splitlines():
            parts = line.split("\t")
            commits.append({"sha": parts[0],
                            "when": parts[1] if len(parts) > 1 else "",
                            "subject": parts[2] if len(parts) > 2 else ""})
    return {
        "branch": _run_git(config, "rev-parse", "--abbrev-ref", "HEAD").get("out", "?"),
        "head": current_sha(config)[:9],
        "last_good": read_last_good(config),
        "checkpoints": list_checkpoints(config, n=10),
        "commits": commits,
    }


def _tracked_source_files(config: Config) -> list[str]:
    """Tracked files outside workspace/ — the source we restore."""
    r = _run_git(config, "ls-files", "--", ".", ":(exclude)workspace")
    if not r["ok"]:
        return []
    return [f for f in r["out"].splitlines() if f]


def restore_to(config: Config, tag: str = "") -> dict:
    """Check the SOURCE tree (workspace/ excluded) back out to a checkpoint tag.

    Uses per-file `git checkout <tag> -- <file>` (never `reset --hard`, which would clobber
    untracked/dirty workspace state). PROTECT_PATHS are left untouched so a stale checkpoint
    can never downgrade the dashboard/kill-switch/rollback machinery itself.
    Returns {ok, tag, restored, error}.
    """
    if not is_git_repo(config):
        return {"ok": False, "error": "not a git repository"}
    tag = tag or read_last_good(config)
    if not tag:
        return {"ok": False, "error": "no checkpoint/last_good to restore"}
    if not _run_git(config, "rev-parse", "--verify", f"{tag}^{{commit}}")["ok"]:
        return {"ok": False, "error": f"unknown checkpoint '{tag}'"}
    restored = 0
    errors = []
    for f in _tracked_source_files(config):
        base = f.split("/")[-1]
        if base in PROTECT_PATHS:
            continue  # never revert the safety machinery
        # Restore this file's content from the tag (skip files absent in the tag).
        if not _run_git(config, "cat-file", "-e", f"{tag}:{f}")["ok"]:
            continue
        co = _run_git(config, "checkout", tag, "--", f)
        if co["ok"]:
            restored += 1
        else:
            errors.append(f"{f}: {co['err']}")
    ok = not errors
    return {"ok": ok, "tag": tag, "restored": restored,
            "error": ("; ".join(errors[:5]) if errors else "")}


def restore_file_to(config: Config, target: str, sha_or_tag: str) -> dict:
    """Restore a single source file to a sha/tag (used by self-edit auto-rollback)."""
    base = target.split("/")[-1].split("\\")[-1]
    if base in PROTECT_PATHS:
        return {"ok": False, "error": f"{base} is protected"}
    if not _run_git(config, "cat-file", "-e", f"{sha_or_tag}:{target}")["ok"]:
        return {"ok": False, "error": f"{target} absent at {sha_or_tag}"}
    co = _run_git(config, "checkout", sha_or_tag, "--", target)
    return {"ok": co["ok"], "error": co["err"]}
