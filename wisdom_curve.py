#!/usr/bin/env python3
"""wisdom_curve.py — the experience-curve instrument (WISDOM_PLAN §4).

The project's central claim under test: *a small, wise model — its lived experience stored
decision-shaped and retrieved at the moment of use — can match or beat a naive larger model on
its home domain.* This harness turns that claim into a plotted, falsifiable curve.

THREE ARMS, one frozen battery, run serially:

  naive-12b  — the resident house mind (config llm_model), EMPTY temp workspace fixture.
  wise-12b   — the same resident mind + a READ-ONLY COPY of the live workspace's memory stores
               into a temp fixture; recall + the §3 "Before you act" block active when the
               [wisdom] recall flag is on. The copy is never written back to the original.
  naive-27b  — "qwen27b" via the SAME llama-swap URL, model swapped by NAME in the request,
               EMPTY temp workspace fixture.

Each arm runs every task in the battery through one `llm.complete` call with the standard
per-model sampler discipline, and scores the answer with the battery's MECHANICAL scorer.

DISCIPLINE (WIS6 — the instrument never runs itself on the live mind):
  --run REFUSES unless the eidos tick loop is NOT running. We check the dashboard's control
  artifacts READ-ONLY: the workspace `eidos.pid` file (is that PID alive?) and a FRESH
  `heartbeat.json` timestamp. We NEVER touch the control API. Pause/stop eidos yourself first
  (see --help). At exit, the resident model is touched back in with one tiny request so the
  house mind is loaded again (llama-swap loads on demand; the 27b arm evicts gemma).

  Results append to a BOUNDED `state/wisdom_curve.jsonl` (WIS8): each row carries
  {ts, battery_version, battery_sha256, creature_age_days, per-arm scores, per-domain breakdown}.

  A battery, once its results exist, is IMMUTABLE: we record the SHA-256 of every battery used
  and REFUSE to run a version whose bytes have drifted from what prior results were scored
  against (score comparability across the curve).

USAGE
  # 1. Pause or stop the eidos tick loop first (the harness refuses otherwise):
  #      dashboard chat "STOP", or  POST http://127.0.0.1:8099/api/control/stop
  #      (or fully: sudo systemctl stop eidos-dashboard.service)
  # 2. Run the curve:
  PYTHONUTF8=1 .venv/bin/python wisdom_curve.py --run
  # Dry run (validate battery + arms, NO LLM calls, no refusal check):
  PYTHONUTF8=1 .venv/bin/python wisdom_curve.py --dry-run
  # Choose arms:
  PYTHONUTF8=1 .venv/bin/python wisdom_curve.py --run --arms naive12,wise12,naive27
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config, load_config, active_sampler  # noqa: E402

# ── constants ──────────────────────────────────────────────────────────────

BATTERY_DIR = Path(__file__).resolve().parent / "exam_battery"
RESULTS_MAX_ROWS = 500                       # WIS8 bounded store
HEARTBEAT_STALE_S = 90                        # a heartbeat newer than this ⇒ the loop is alive
ARM_MODELS = {                                # arm → (model_key, uses_wisdom_stores)
    "naive12": ("__resident__", False),
    "wise12":  ("__resident__", True),
    "naive27": ("qwen27b", False),
}
# Memory stores the wise arm copies (read-only) out of the live workspace into its fixture.
# Copy, never touch the original (WIS6). Fail-open per store if absent.
WISDOM_STORE_NAMES = [
    "engram_episodic.jsonl",
    "knowledge",                # the long-term engram store + vectors + reflections (a directory)
    "quests.jsonl",
    "persona.json",
    "creature.json",
]

_GREEN, _YELLOW, _RED, _CYAN, _DIM, _BOLD, _RESET = (
    "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[2m", "\033[1m", "\033[0m")


# ═══════════════════════════════════════════════════════════════════════════
#  Battery loading + immutability
# ═══════════════════════════════════════════════════════════════════════════

SCORER_TYPES = {"exact", "regex", "claim", "action_signature"}


class BatteryError(Exception):
    """Raised when a battery is malformed, or its bytes drifted after results exist."""


def battery_path(version: str) -> Path:
    return BATTERY_DIR / f"{version}.jsonl"


def battery_sha256(version: str) -> str:
    """SHA-256 of the raw battery bytes — the immutability fingerprint."""
    return hashlib.sha256(battery_path(version).read_bytes()).hexdigest()


def load_battery(version: str = "v1") -> list[dict]:
    """Parse and VALIDATE a battery file. Raises BatteryError on any malformation:
    unparseable line, duplicate id, unknown/absent scorer type, missing prompt, or a scorer
    that isn't fully mechanical (missing its required fields)."""
    path = battery_path(version)
    if not path.exists():
        raise BatteryError(f"battery not found: {path}")

    tasks: list[dict] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError as e:
            raise BatteryError(f"{version}.jsonl line {lineno}: bad JSON: {e}") from e
        _validate_task(task, version, lineno)
        if task["id"] in seen:
            raise BatteryError(f"{version}.jsonl line {lineno}: duplicate id {task['id']!r}")
        seen.add(task["id"])
        tasks.append(task)
    if not tasks:
        raise BatteryError(f"{version}.jsonl is empty")
    return tasks


def _validate_task(task: dict, version: str, lineno: int) -> None:
    where = f"{version}.jsonl line {lineno}"
    if not isinstance(task, dict):
        raise BatteryError(f"{where}: task is not an object")
    for field in ("id", "domain", "prompt", "scorer"):
        if field not in task:
            raise BatteryError(f"{where}: missing required field {field!r}")
    if not isinstance(task["id"], str) or not task["id"]:
        raise BatteryError(f"{where}: id must be a non-empty string")
    if not isinstance(task["prompt"], str) or not task["prompt"].strip():
        raise BatteryError(f"{where}: prompt must be a non-empty string")
    scorer = task["scorer"]
    if not isinstance(scorer, dict):
        raise BatteryError(f"{where}: scorer must be an object")
    stype = scorer.get("type")
    if stype not in SCORER_TYPES:
        raise BatteryError(f"{where}: scorer.type {stype!r} not in {sorted(SCORER_TYPES)}")
    # Every scorer must be fully MECHANICAL — carry the fields its type needs to decide alone.
    if stype == "exact":
        if not (scorer.get("answer") or scorer.get("answers")):
            raise BatteryError(f"{where}: exact scorer needs 'answer' or 'answers'")
    elif stype == "regex":
        pat = scorer.get("pattern")
        if not isinstance(pat, str) or not pat:
            raise BatteryError(f"{where}: regex scorer needs a 'pattern' string")
        try:
            re.compile(pat)
        except re.error as e:
            raise BatteryError(f"{where}: uncompilable regex: {e}") from e
    elif stype == "claim":
        mi = scorer.get("must_include")
        if not isinstance(mi, list) or not mi:
            raise BatteryError(f"{where}: claim scorer needs a non-empty 'must_include' list")
    elif stype == "action_signature":
        if not scorer.get("tool"):
            raise BatteryError(f"{where}: action_signature scorer needs a 'tool' name")
        ap = scorer.get("args_pattern")
        if ap is not None:
            try:
                re.compile(ap)
            except re.error as e:
                raise BatteryError(f"{where}: uncompilable args_pattern: {e}") from e


# ═══════════════════════════════════════════════════════════════════════════
#  Mechanical scorers
# ═══════════════════════════════════════════════════════════════════════════

def _normalize(s: str) -> str:
    """Casefold, collapse whitespace, strip surrounding quotes/punctuation."""
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s).casefold()
    return s.strip(" \t\n\r.,:;!?'\"`")


def score_answer(scorer: dict, answer: str) -> float:
    """Return a score in [0.0, 1.0] for `answer` against `scorer`. MECHANICAL only."""
    stype = scorer["type"]
    answer = answer or ""
    if stype == "exact":
        cands = scorer.get("answers") or [scorer.get("answer")]
        norm = _normalize(answer)
        # accept an exact answer, or the answer appearing as a whole token/substring line
        for c in cands:
            cn = _normalize(str(c))
            if cn and (norm == cn or cn in norm):
                return 1.0
        return 0.0
    if stype == "regex":
        flags = re.IGNORECASE
        for ch in scorer.get("flags", ""):
            flags |= {"s": re.DOTALL, "m": re.MULTILINE, "x": re.VERBOSE}.get(ch, 0)
        return 1.0 if re.search(scorer["pattern"], answer, flags) else 0.0
    if stype == "claim":
        low = answer.casefold()
        for bad in scorer.get("must_exclude", []):
            if str(bad).casefold() in low:
                return 0.0
        needed = scorer["must_include"]
        hits = sum(1 for m in needed if str(m).casefold() in low)
        return hits / len(needed) if needed else 0.0
    if stype == "action_signature":
        from parser import parse_tool_call
        call = parse_tool_call(answer)
        if not call or call.tool != scorer["tool"]:
            return 0.0
        ap = scorer.get("args_pattern")
        if ap:
            raw = getattr(call, "raw", "") or json.dumps(getattr(call, "args", {}))
            if not re.search(ap, raw, re.IGNORECASE):
                return 0.0
        return 1.0
    raise BatteryError(f"unknown scorer type {stype!r}")   # unreachable post-validation


# ═══════════════════════════════════════════════════════════════════════════
#  WIS6 — refusal: the instrument never runs on the live mind
# ═══════════════════════════════════════════════════════════════════════════

def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def eidos_running(config: Config) -> tuple[bool, str]:
    """Read-only check of the dashboard's control artifacts. Returns (running, reason).
    Two independent signals, either one ⇒ running:
      1. workspace/eidos.pid names a PID that is alive.
      2. workspace/heartbeat.json carries a timestamp newer than HEARTBEAT_STALE_S.
    We never touch the control API (WIS6) — reads only."""
    ws = config.workspace
    pidfile = ws / "eidos.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except Exception:
        pid = 0
    if _pid_alive(pid):
        return True, f"eidos.pid {pid} is alive"

    hb = ws / "heartbeat.json"
    try:
        data = json.loads(hb.read_text())
        ts = float(data.get("ts", 0))
        age = time.time() - ts
        if 0 <= age < HEARTBEAT_STALE_S:
            return True, f"heartbeat is fresh ({age:.0f}s old)"
    except Exception:
        pass
    return False, "no live pid and heartbeat is stale/absent"


# ═══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════

def make_arm_config(base: Config, model: str, tmp_workspace: str, timeout: int) -> Config:
    """A fresh Config for one arm: same LLM endpoint, chosen model, isolated temp workspace."""
    cfg = Config()
    # carry the live endpoint + any profiles (sampler discipline) from the loaded base config
    cfg.llm_url = base.llm_url
    cfg.llm_model = model
    cfg.llm_max_tokens = base.llm_max_tokens
    cfg.llm_request_timeout_s = timeout
    cfg.llm_profiles = getattr(base, "llm_profiles", {}) or {}
    for k in ("llm_temperature", "llm_top_p", "llm_top_k", "llm_min_p",
              "llm_presence_penalty", "llm_frequency_penalty", "llm_repeat_penalty"):
        if hasattr(base, k):
            setattr(cfg, k, getattr(base, k))
    cfg.workspace_dir = tmp_workspace
    (Path(tmp_workspace) / "state").mkdir(parents=True, exist_ok=True)
    return cfg


def copy_wisdom_stores(live_workspace: Path, dest_workspace: Path) -> list[str]:
    """READ-ONLY copy of the live memory stores into the wise-arm fixture. Copy, NEVER touch the
    original (WIS6). Returns the list of stores actually copied. Fail-open per store."""
    dest_workspace.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in WISDOM_STORE_NAMES:
        src = live_workspace / name
        if not src.exists():
            continue
        dst = dest_workspace / name
        try:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            copied.append(name)
        except OSError:
            continue   # fail-open: a store we can't copy just isn't available to the wise arm
    return copied


def build_wisdom_prefix(cfg: Config, prompt: str) -> str:
    """The §3 calling convention, rendered from the wise arm's COPIED stores when the [wisdom]
    recall flag is on. Best-effort and fail-open: if recall/embeddings aren't available we
    return an empty prefix rather than crash the arm. Never touches the live workspace (cfg
    points at the temp copy)."""
    if not getattr(cfg, "wisdom_recall_enabled", False):
        return ""
    try:
        from memory_manager import MemoryManager
        mm = MemoryManager(cfg)
        budget = getattr(cfg, "wisdom_block_max_chars", 700)
        engrams = mm.recall(prompt, budget_chars=budget)
    except Exception:
        return ""
    if not engrams:
        return ""
    lines = ["## Before you act", ""]
    for e in engrams[:3]:
        body = (getattr(e, "body", "") or "").strip().replace("\n", " ")
        if body:
            lines.append(f"- {body[:200]}")
    lines.append("")
    lines.append("These are YOUR precedents, not orders — verify they transfer before leaning on them.")
    return "\n".join(lines) + "\n\n"


# ═══════════════════════════════════════════════════════════════════════════
#  Running an arm
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_model(arm: str, base: Config) -> str:
    model_key, _ = ARM_MODELS[arm]
    return base.llm_model if model_key == "__resident__" else model_key


def run_arm(arm: str, tasks: list[dict], base: Config, *, timeout: int,
            live_workspace: Path, verbose: bool = True) -> dict:
    """Run every battery task through one arm. Returns an arm result dict with per-task and
    per-domain scores. Serial: one llm.complete per task with the arm's sampler discipline."""
    from llm import complete, LLMError

    model = _resolve_model(arm, base)
    _, uses_wisdom = ARM_MODELS[arm]

    tmp = tempfile.mkdtemp(prefix=f"eidos_wc_{arm}_")
    cfg = make_arm_config(base, model, tmp, timeout)
    copied: list[str] = []
    try:
        if uses_wisdom:
            copied = copy_wisdom_stores(live_workspace, cfg.workspace)
            # inherit the §3 flag from the loaded base config (getattr-only; never touch files)
            cfg.wisdom_recall_enabled = getattr(base, "wisdom_recall_enabled", False)
            cfg.wisdom_block_max_chars = getattr(base, "wisdom_block_max_chars", 700)

        sampler = active_sampler(cfg, model)
        per_task, per_domain, per_domain_max = [], {}, {}
        for task in tasks:
            prefix = build_wisdom_prefix(cfg, task["prompt"]) if uses_wisdom else ""
            messages = [{"role": "user", "content": prefix + task["prompt"]}]
            try:
                answer = complete(
                    messages, cfg,
                    temperature=sampler["temperature"],
                    run_id=f"wisdom_curve_{arm}", tick=0)
            except LLMError as e:
                answer = ""
                if verbose:
                    print(f"      {_RED}{task['id']}: LLM error {e}{_RESET}")
            score = score_answer(task["scorer"], answer)
            per_task.append({"id": task["id"], "domain": task["domain"], "score": score})
            d = task["domain"]
            per_domain[d] = per_domain.get(d, 0.0) + score
            per_domain_max[d] = per_domain_max.get(d, 0) + 1
            if verbose:
                mark = _GREEN + "✓" if score >= 1.0 else (_YELLOW + "~" if score > 0 else _RED + "✗")
                print(f"      {mark}{_RESET} {task['id']:<32} {score:.2f}")

        total = sum(t["score"] for t in per_task)
        return {
            "arm": arm,
            "model": model,
            "score": round(total, 3),
            "max": len(tasks),
            "pct": round(100 * total / len(tasks), 1) if tasks else 0.0,
            "per_domain": {d: round(per_domain[d], 3) for d in per_domain},
            "per_domain_max": per_domain_max,
            "wisdom_stores_copied": copied,
            "wisdom_active": bool(uses_wisdom and getattr(cfg, "wisdom_recall_enabled", False)),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
#  Restore discipline (WIS6) + results append
# ═══════════════════════════════════════════════════════════════════════════

def touch_resident_model(base: Config, *, timeout: int = 120) -> bool:
    """After the run (the 27b arm evicted gemma), touch the resident model back in with one tiny
    request so the house mind is loaded again — llama-swap loads on demand. Returns True on
    success. Fail-soft: a failure here is reported but never crashes the harness."""
    from llm import complete, LLMError
    cfg = Config()
    cfg.llm_url = base.llm_url
    cfg.llm_model = base.llm_model
    cfg.llm_request_timeout_s = timeout
    cfg.llm_profiles = getattr(base, "llm_profiles", {}) or {}
    cfg.workspace_dir = tempfile.mkdtemp(prefix="eidos_wc_restore_")
    try:
        complete([{"role": "user", "content": "ok"}], cfg,
                 max_tokens=1, run_id="wisdom_curve_restore")
        return True
    except LLMError:
        return False
    finally:
        shutil.rmtree(cfg.workspace_dir, ignore_errors=True)


def creature_age_days(live_workspace: Path) -> float:
    """Days since the creature's birth (creature.json born_ts). -1.0 if unknown."""
    try:
        data = json.loads((live_workspace / "creature.json").read_text())
        born = float(data.get("born_ts", 0))
        if born > 0:
            return round((time.time() - born) / 86400.0, 2)
    except Exception:
        pass
    return -1.0


def append_result(state_dir: Path, row: dict, *, max_rows: int = RESULTS_MAX_ROWS) -> Path:
    """Append one result row to the BOUNDED state/wisdom_curve.jsonl (WIS8). Atomic rewrite,
    keeping only the most recent `max_rows`. Returns the results path."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "wisdom_curve.jsonl"
    existing = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    existing.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    existing.append(row)
    existing = existing[-max_rows:]
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(r) for r in existing) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def prior_battery_hashes(state_dir: Path) -> dict[str, set[str]]:
    """Map battery_version → set of battery_sha256 values seen in prior results. Used to enforce
    immutability: a version whose file bytes no longer match a published hash is REFUSED."""
    path = state_dir / "wisdom_curve.jsonl"
    seen: dict[str, set[str]] = {}
    if not path.exists():
        return seen
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        v, h = row.get("battery_version"), row.get("battery_sha256")
        if v and h:
            seen.setdefault(v, set()).add(h)
    return seen


def assert_battery_immutable(version: str, state_dir: Path) -> None:
    """WIS: if results referencing `version` already exist and were scored against a DIFFERENT
    byte-hash, refuse — a used battery is immutable (score comparability)."""
    current = battery_sha256(version)
    prior = prior_battery_hashes(state_dir).get(version, set())
    if prior and current not in prior:
        raise BatteryError(
            f"battery {version} has CHANGED since results were recorded "
            f"(current sha256 {current[:12]}…, results scored against {sorted(h[:12] for h in prior)}). "
            f"A used battery is IMMUTABLE — bump to a new version file instead of editing {version}.jsonl.")


# ═══════════════════════════════════════════════════════════════════════════
#  Orchestration
# ═══════════════════════════════════════════════════════════════════════════

def run_curve(base: Config, *, arms: list[str], version: str, timeout: int,
              dry_run: bool, verbose: bool = True) -> dict:
    """Full instrument run. On --run, enforces WIS6 refusal + immutability, runs the arms
    serially, appends a bounded result row, and restores the resident model. On --dry-run,
    validates the battery and arms and makes NO LLM calls, NO refusal check, NO writes."""
    tasks = load_battery(version)
    sha = battery_sha256(version)
    live_workspace = base.workspace
    state_dir = live_workspace / "state"

    if dry_run:
        if verbose:
            print(f"{_BOLD}wisdom_curve DRY RUN{_RESET} — battery {version} "
                  f"({len(tasks)} tasks, sha256 {sha[:12]}…)")
            doms = {}
            for t in tasks:
                doms[t["domain"]] = doms.get(t["domain"], 0) + 1
            print(f"  domains: {doms}")
            print(f"  arms: {arms} → models "
                  f"{[_resolve_model(a, base) for a in arms]}")
            print(f"  {_GREEN}battery well-formed; no LLM calls made.{_RESET}")
        return {"dry_run": True, "battery_version": version, "battery_sha256": sha,
                "tasks": len(tasks), "arms": arms}

    # WIS6 — refuse if the live loop is running.
    running, reason = eidos_running(base)
    if running:
        raise SystemExit(
            f"{_RED}REFUSED (WIS6): the eidos loop appears to be RUNNING — {reason}.{_RESET}\n"
            f"Pause or stop it first (dashboard chat 'STOP', or "
            f"POST http://127.0.0.1:8099/api/control/stop), then re-run.")

    # Immutability guard.
    assert_battery_immutable(version, state_dir)

    if verbose:
        print(f"{_BOLD}wisdom_curve RUN{_RESET} — battery {version} "
              f"({len(tasks)} tasks, sha256 {sha[:12]}…)")
        print(f"  eidos not running ({reason}); proceeding.\n")

    arm_results = []
    for arm in arms:
        if verbose:
            print(f"  {_CYAN}── arm {arm} ({_resolve_model(arm, base)}) ──{_RESET}")
        arm_results.append(
            run_arm(arm, tasks, base, timeout=timeout,
                    live_workspace=live_workspace, verbose=verbose))

    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "battery_version": version,
        "battery_sha256": sha,
        "creature_age_days": creature_age_days(live_workspace),
        "arms": {r["arm"]: {"model": r["model"], "score": r["score"], "max": r["max"],
                            "pct": r["pct"], "per_domain": r["per_domain"],
                            "per_domain_max": r["per_domain_max"],
                            "wisdom_active": r["wisdom_active"]}
                 for r in arm_results},
    }
    results_path = append_result(state_dir, row)

    restored = touch_resident_model(base)
    if verbose:
        print()
        for r in arm_results:
            print(f"  {_BOLD}{r['arm']:<9}{_RESET} {r['score']:.1f}/{r['max']} "
                  f"({r['pct']:.0f}%)  per-domain {r['per_domain']}")
        print(f"\n  results → {results_path}")
        print(f"  resident model restore: "
              f"{_GREEN + 'ok' if restored else _YELLOW + 'FAILED (touch it in manually)'}{_RESET}")
    row["_restored"] = restored
    row["_results_path"] = str(results_path)
    return row


# ── main ─────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="wisdom_curve.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
        epilog=(
            "WIS6 discipline: --run REFUSES unless the eidos tick loop is paused/stopped. It checks "
            "the workspace eidos.pid (alive?) and heartbeat.json (fresh?) READ-ONLY — it never touches "
            "the control API. Pause eidos yourself first: dashboard chat 'STOP', or "
            "POST http://127.0.0.1:8099/api/control/stop. The 27b arm evicts gemma from VRAM; at exit "
            "the harness touches the resident model back in with one tiny request (llama-swap loads on "
            "demand) so the house mind is loaded again."),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="run the curve (enforces WIS6 refusal)")
    mode.add_argument("--dry-run", action="store_true",
                      help="validate battery + arms, make NO LLM calls, write nothing")
    parser.add_argument("--arms", default="naive12,wise12,naive27",
                        help="comma-separated arms (default: naive12,wise12,naive27)")
    parser.add_argument("--battery", default="v1", help="battery version (default: v1)")
    parser.add_argument("--config", default="config.toml", help="config file (default: config.toml)")
    parser.add_argument("--timeout", type=int, default=300, help="per-task LLM timeout s (default 300)")
    parser.add_argument("--quiet", action="store_true", help="only final summary")
    args = parser.parse_args(argv)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in arms if a not in ARM_MODELS]
    if bad:
        parser.error(f"unknown arm(s) {bad}; valid: {sorted(ARM_MODELS)}")

    base = load_config(args.config)
    try:
        run_curve(base, arms=arms, version=args.battery, timeout=args.timeout,
                  dry_run=args.dry_run, verbose=not args.quiet)
    except BatteryError as e:
        print(f"{_RED}battery error: {e}{_RESET}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
