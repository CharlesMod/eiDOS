"""Self-authored skills: let eiDOS write, validate, hot-load, version, and reuse its own tools.

A *skill* is a Python tool function with the same contract as a built-in tool:

    def tool_<name>(args: dict, config: Config) -> ToolResult: ...

The agent authors one via the `create_skill` tool. We:
  1. AST-validate it (parseable, correct function + signature, no eval/exec/compile/__import__),
  2. dry-run it in an isolated subprocess (catch import/syntax/hang defects),
  3. save it as a versioned file under workspace/skills/,
  4. hot-load it into the live tool registry so it's usable on the very next tick,
  5. track invocations/successes and auto-promote reliable skills to "trusted".

Safety posture (deliberate, for a local single-user house AI): skills MAY import and use
`os`, `subprocess`, `requests`/`httpx`, `socket`, etc. — that is the whole point (Tuya plugs,
cameras, HTTP). We block only the meta-execution builtins (eval/exec/compile/__import__) in
skill *source*, and the existing command-blocking in tool_bash still guards shelled commands.
"""

import ast
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from config import Config
from tools import ToolResult, TOOLS, _kill_pid_tree  # _kill_pid_tree: hard-kill a subprocess tree
from tools import _creature_root                     # the world skill code runs in (same cwd rule as bash)
from skill_atoms import build_atoms, ATOM_NAMES  # M2: the in-scope vocabulary skills compose
from skill_atoms import (                          # 3.3 composition: authoring-time cycle check + graph
    check_composition_cycle, static_calls_in_source, COMPOSITION_MAX_DEPTH,
)

logger = logging.getLogger("eidos.skills")

KAIROS_DIR = Path(__file__).resolve().parent
SKILLS_SUBDIR = "skills"
MANIFEST_NAME = "_index.json"

# Names the agent may NOT use for a skill (built-in tools + skill-admin tools).
RESERVED_NAMES = {
    "bash", "write_file", "read_file", "bg_run", "bg_check",
    "update_plan", "memorize", "recall",
    "create_skill", "edit_skill", "list_skills", "rollback_skill",
    "call",        # 3.3 composition atom — a skill named `call` would shadow it in every namespace
    *ATOM_NAMES,   # a skill must not shadow an atom in its own namespace
}

# Meta-execution builtins forbidden inside skill source (they bypass review).
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_TRUST_MIN_USES = 5
_TRUST_MIN_RATE = 0.8
_QUARANTINE_MIN_USES = 5     # this many uses with ZERO successes → quarantined (auto-disabled)
_DEMOTE_RATE = 0.5           # a trusted skill's active version under this rate loses trust

# Statuses that mean "a live, callable skill" (brief/dup-guard/etc. must include BOTH —
# filtering on just "active" made trusted skills invisible, the worst possible incentive).
_LIVE_STATUSES = ("active", "trusted")

# --- Pillars 1.2: killable subprocess execution + per-skill telemetry --------------------------
# The old thread-watchdog ABANDONED a hung skill (Python can't kill a thread) — tick 342's 6.7-min
# freeze. When `config.pillars_killable_skills_enabled` is ON, a skill instead runs in a one-shot
# subprocess (same harness family as the author-time dry-run) that we HARD-KILL on overrun. The
# whole path is dark behind the flag; flag-OFF keeps the thread-watchdog byte-for-byte.
_TELEMETRY_LATENCY_SAMPLES = 50      # declared knob: rolling window of per-skill latency samples kept
                                     # for p50/p95 (bounded so the manifest can't grow unboundedly).
_SKILL_TIMEOUT_MULTIPLIER = 3.0      # declared knob: per-skill timeout = measured p95 × 3, then
                                     # clamped to [pillars_skill_timeout_floor_s, ..._ceiling_s].
_SUBPROC_KILL_GRACE_S = 3.0          # declared knob: after terminate on overrun, wait this long for
                                     # the process to die before the hard SIGKILL tree-kill.

# --- Pillars 3.1/3.2: the skill economy (reuse as the resting state) ---------------------------
# 3.1 Affordances: at the decision point we surface the top-K existing skills most relevant to the
# CURRENT situation, ranked by similarity × trust × birth-episode-strength. Similarity is cosine over
# MiniLM embeddings (embedding.py) of "description + tags" vs the situation; trust is derived from the
# manifest status + success rate. A small exploration ε occasionally hands the LAST slot to a cold
# (untrusted / never-used) skill so it can earn — norepinephrine's explore/exploit knob, wired into
# the ranker so the affordance list can't collapse into a rich-get-richer echo chamber (§6 Matthew).
_AFFORDANCE_EXPLORE_EPS = 0.2        # declared knob: P(last affordance slot goes to a cold skill).
_TRUST_STATUS_WEIGHT = {             # declared: status → trust prior (before the success-rate factor).
    "trusted": 1.0, "active": 0.6, "quarantined": 0.0, "disabled": 0.0, "retired": 0.0,
}
_TRUST_RATE_FLOOR = 0.3              # declared: a live skill with no record yet still gets this much
                                     # trust (so a brand-new skill isn't invisible until it has stats).
_BIRTH_EPISODE_STRENGTH = 1.0        # TODO(Pillars S-6): skills will carry links to the episodes that
                                     # birthed them; a future phase ranks affordances by that episode's
                                     # strength. STUBBED at 1.0 now — no dependency on any engram module
                                     # (another agent owns it). Left as a named factor so wiring it in
                                     # later is a one-line change, not a re-derivation of the formula.

# 3.2 Economics. Authoring is priced by novelty: a fully-novel skill costs the base energy; a
# near-duplicate costs a steep multiple (the PRICE is the dedup pressure — replacing the old hard
# duplicate-veto with an economic disincentive, §3). Reuse (calling an existing skill) pays MORE XP
# than authoring, so the settled incentive favours reuse over creation. Auto-retire archives skills
# unused past a threshold so the decision space stays clean (use-dependent pruning).
_AUTHOR_DUP_MAX_MULTIPLIER = 10.0    # declared: near-duplicate authoring costs up to base × this
                                     # (at max similarity 1.0); a fully-novel skill (sim 0) costs base×1.
_XP_CREATE = 3                       # declared: XP for authoring a NEW skill (creation is the cheap,
                                     # abundant act — it should not be the way to farm XP).
_XP_REUSE = 8                        # declared: XP for a SUCCESSFUL reuse of an existing skill. Strictly
                                     # > _XP_CREATE so the settled economy rewards reuse over creation.
# Statuses that mean "archived / retired" — filtered out of the brief AND the affordance list.
_RETIRED_STATUS = "retired"

# --- Pillars 3.3: promotion-to-atom (a proven, reused composition congeals into a new atom) ----------
# The chunking half of the striatal design: when a TRUSTED composition has been REUSED enough, it is a
# candidate to become an atom of the vocabulary — congealed experience formalized (plan S-4). The seam
# mirrors selfedit: eiDOS/glue PROPOSES (propose_promotion → a candidate queue), the operator-controlled
# dashboard APPLIES (apply_promotion → the composition is compiled into the promoted-atom store that
# build_atoms injects). All dark behind pillars_skill_composition_enabled.
_PROMOTION_MIN_REUSES = 5            # declared: a composition needs at least this many SUCCESSFUL uses
                                     # before it's an atom candidate — the same automatization bar the
                                     # trust promotion uses (_TRUST_MIN_USES), applied to composition:
                                     # an atom is a move that has proven itself by repetition, not once.
_PROMOTED_ATOMS_NAME = "_promoted_atoms.json"   # the compiled-atom store, alongside the manifest.


# ---------------------------------------------------------------------------
# Paths & manifest
# ---------------------------------------------------------------------------

def _skills_dir(config: Config) -> Path:
    d = config.workspace / SKILLS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(config: Config) -> Path:
    return _skills_dir(config) / MANIFEST_NAME


def _skill_file(config: Config, name: str, version: str) -> Path:
    return _skills_dir(config) / f"{name}__{version}.py"


def _load_manifest(config: Config) -> dict:
    try:
        return json.loads(_manifest_path(config).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"skills": {}}


def _save_manifest(config: Config, manifest: dict) -> None:
    p = _manifest_path(config)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(p)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _next_version(entry: Optional[dict]) -> str:
    if not entry or not entry.get("versions"):
        return "1.0.0"
    def key(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0, 0, 0)
    latest = sorted(entry["versions"], key=key)[-1]
    parts = latest.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        parts[2] = str(int(parts[2]) + 1)
        return ".".join(parts)
    return "1.0.0"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_source(name: str, code: str, sandbox: bool = True) -> list[str]:
    """Static checks. Returns a list of error strings (empty == valid). When `sandbox` (the default),
    the meta-execution builtins (eval/exec/compile/__import__) are forbidden in skill source; the
    `skill_sandbox_enabled=false` checkbox sets it free (full coding-agent freedom)."""
    errors: list[str] = []

    if not _NAME_RE.match(name):
        errors.append(f"invalid skill name '{name}': must match [a-z][a-z0-9_]{{1,40}}")
    if name in RESERVED_NAMES:
        errors.append(f"'{name}' is a reserved/built-in tool name")

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return errors + [f"SyntaxError at line {e.lineno}: {e.msg}"]

    func_name = f"tool_{name}"
    funcdef = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name),
        None,
    )
    if funcdef is None:
        errors.append(f"source must define exactly: def {func_name}(args, config) -> ToolResult")
    else:
        if isinstance(funcdef, ast.AsyncFunctionDef):
            errors.append(f"{func_name} must be a regular (non-async) function")
        argnames = [a.arg for a in funcdef.args.args]
        if argnames[:2] != ["args", "config"]:
            errors.append(f"{func_name} signature must be (args, config); got {argnames or '()'}")

    if sandbox:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in FORBIDDEN_BUILTINS:
                errors.append(f"forbidden builtin '{node.func.id}()' in skill source")
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_BUILTINS \
                    and isinstance(getattr(node, "ctx", None), ast.Load):
                # catch assignment/aliasing like `e = exec`
                errors.append(f"reference to forbidden builtin '{node.id}'")

    # de-dupe, preserve order
    seen, out = set(), []
    for e in errors:
        if e not in seen:
            seen.add(e); out.append(e)
    return out


def _validate_composition(config: Config, name: str, code: str) -> list[str]:
    """3.3: authoring-time composition checks. When composition is ON, REJECT source that introduces a
    call-graph cycle (A→B→A) — a static check so a cyclic composition never reaches the runtime (the
    runtime then only has to bound depth + budget of an acyclic-but-large composition). When composition
    is OFF, a `call(...)` in the source references a name that isn't in scope, so refuse it early with a
    clear message instead of letting the dry-run NameError. Empty list == OK."""
    calls = static_calls_in_source(code)
    if not calls:
        return []
    if not getattr(config, "pillars_skill_composition_enabled", False):
        return [f"skill '{name}' uses call(...) but composition is disabled "
                f"(pillars_skill_composition_enabled=false) — the `call` atom is not in scope."]
    return check_composition_cycle(config, name, code)


def _sample_args(args_schema: Optional[dict]) -> dict:
    """Build a schema-shaped sample argument dict so the dry-run CALLS the skill the way the model will,
    not with an empty `{}` that masks arg-independent contract bugs. Best-effort and type-guessed: a
    skill declares `args_schema` as {arg_name: type_hint | {"type": ...}}; we fill each with a benign
    value of the hinted type. An unhinted/opaque schema yields `{}` (same as the old behavior)."""
    out: dict = {}
    if not isinstance(args_schema, dict):
        return out
    for key, spec in args_schema.items():
        t = spec.get("type") if isinstance(spec, dict) else spec
        t = str(t or "").lower()
        if "int" in t:
            out[key] = 1
        elif "float" in t or "number" in t:
            out[key] = 1.0
        elif "bool" in t:
            out[key] = False
        elif "list" in t or "array" in t:
            out[key] = []
        elif "dict" in t or "object" in t or "map" in t:
            out[key] = {}
        else:
            out[key] = "x"   # default: a short non-empty string (covers str / unknown)
    return out


def _dry_run(config: Config, name: str, version: str,
             args_schema: Optional[dict] = None, strict_contract: bool = False) -> tuple[bool, str]:
    """Run the saved skill file in a fresh subprocess. Returns (ok_to_activate, note).

    LOAD_ERROR (syntax/import/not-defined) or a timeout block activation.
    A runtime CALL_RAISED with empty/sample args is fine (skills often need real args).

    Pillars 1.2: when `strict_contract` (the killable-skills path), the harness is CALLED with
    `_sample_args(args_schema)` and a return that is NOT a ToolResult is REJECTED at authoring time
    (the dict-vs-ToolResult bug used to be caught only at dispatch, after it already crash-looped the
    creature — tick 14066). Flag-OFF keeps the lenient behavior (CALL_BADRESULT normalized, not
    rejected) so nothing changes for the current path.
    """
    skill_file = _skill_file(config, name, version)
    harness = HARNESS_TEMPLATE.format(
        kairos=repr(str(KAIROS_DIR)),
        # The dry-run CALLS the skill, so it runs where the skill will LIVE: the creature's world
        # (_creature_root — its home burrow, or the full workspace for the house-AI), the same cwd
        # tool_bash stands on. Running here instead of the repo root is what makes the smoke call
        # honest: a relative path that works now works live, and its droppings land in the world,
        # not the source tree (the repo used to collect boss_habits.log / snapshot_*.jpg). Threaded
        # from the caller's config for the same reason as `compose` below.
        home=repr(str(_creature_root(config))),
        skill_file=repr(str(skill_file)),
        func=f"tool_{name}",
        sample_args=repr(json.dumps(_sample_args(args_schema) if strict_contract else {})),
        # 3.3: the dry-run's atom namespace must match the RUNTIME's — so a composition validates with
        # `call` in scope exactly when it would have it live. The harness reloads config from disk (which
        # may not carry the caller's in-memory flag), so we thread the effective composition flag in.
        compose=repr(bool(getattr(config, "pillars_skill_composition_enabled", False))),
    )
    harness_path = _skills_dir(config) / ".dryrun.py"
    harness_path.write_text(harness, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(harness_path)],
            cwd=str(_creature_root(config)), capture_output=True, text=True, timeout=25,
        )
    except subprocess.TimeoutExpired:
        return False, "dry-run timed out (>25s) — possible hang"
    finally:
        try:
            harness_path.unlink()
        except OSError:
            pass

    out = (proc.stdout or "") + (proc.stderr or "")
    line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
    if line.startswith("LOAD_ERROR"):
        return False, line[:300]
    if line.startswith("CALL_OK"):
        return True, "dry-run clean"
    if line.startswith("CALL_BADRESULT"):
        # The skill loaded and ran but returned the WRONG TYPE (a dict/str/number, not a ToolResult).
        # Under the killable path we enforce the contract HERE (authoring time) so a mis-typed return
        # can never reach dispatch and KeyError the tick-loop display. Flag-OFF falls through to the
        # lenient handling below (normalized at dispatch, historical behavior).
        if strict_contract:
            return False, (f"{line[:200]} — a skill MUST `return ToolResult(output=..., "
                           f"full_output_path=None, success=True/False, duration_s=...)`. Returning a "
                           f"dict/str/number is rejected: build a ToolResult before returning.")
    if line.startswith("CALL_RAISED") or line.startswith("CALL_BADRESULT"):
        # A reference to something UNAVAILABLE (a missing import, an undefined name) means the skill can
        # NEVER work — reject it now. The predecessor MASKED these as "probably just needs args" and so
        # promoted 20 skills that did `import requests` (not installed) — the #1 source of dead weight.
        # An arg-shape error (called with empty {}) is genuinely fine: the skill needs real arguments.
        if any(t in line for t in ("ModuleNotFoundError", "ImportError", "NameError")):
            return False, (f"{line[:200]} — the package/name isn't available. Use the in-scope ATOMS "
                           f"(http_get, http_post, json_parse, sh, read, write, recall, memorize, note, "
                           f"look, net_scan, tcp_probe, http_probe) instead of importing.")
        # loaded fine; raised only because it was called with sample args — acceptable
        return True, f"loads OK; {line[:200]} (expected if it needs args)"
    return False, f"dry-run produced no verdict: {out[:200]}"


HARNESS_TEMPLATE = '''\
import sys, os, json as _json
sys.path.insert(0, {kairos})   # repo modules import from here — inserted BEFORE any chdir moves the world
from config import load_config, Config
from tools import ToolResult
cfg = load_config(os.path.join({kairos}, "config.toml"))
cfg.pillars_skill_composition_enabled = {compose}   # 3.3: match the authoring context's atom namespace
if not os.path.isabs(cfg.embedding_model_dir):
    # the embedding model is repo skeleton, not world — pin it before the chdir un-anchors it
    cfg.embedding_model_dir = os.path.join({kairos}, cfg.embedding_model_dir)
os.chdir({home})   # the creature's world: a skill's relative paths resolve in its home, never the repo
code = open({skill_file}, encoding="utf-8").read()
ns = {{"Config": Config, "ToolResult": ToolResult}}
try:
    from skill_atoms import build_atoms
    ns.update(build_atoms(cfg))   # same atom vocabulary as the live runner, so validation matches runtime
except Exception:
    pass
try:
    exec(compile(code, {skill_file}, "exec"), ns)
except Exception as e:
    print("LOAD_ERROR: " + type(e).__name__ + ": " + repr(e)); sys.exit(0)
fn = ns.get("{func}")
if fn is None:
    print("LOAD_ERROR: {func} not defined after exec"); sys.exit(0)
try:
    _sample = _json.loads({sample_args})
except Exception:
    _sample = {{}}
try:
    r = fn(_sample, cfg)
    print("CALL_OK" if isinstance(r, ToolResult) else "CALL_BADRESULT: returned " + type(r).__name__)
except SystemExit:
    print("CALL_RAISED: SystemExit")
except Exception as e:
    print("CALL_RAISED: " + type(e).__name__ + ": " + repr(e)[:200])
'''


# ---------------------------------------------------------------------------
# Pillars 1.2: killable subprocess execution
# ---------------------------------------------------------------------------

# A one-shot execution harness: load the skill (atoms + config injected, identical to _build_runner /
# the dry-run), call it with the REAL args handed in on argv, and print the ToolResult back as a single
# JSON line on a sentinel-prefixed stdout line. Any load/exec failure prints a typed marker. The parent
# HARD-KILLS this process on overrun, so a hang (while True / timeout-less socket) dies with the process
# — no orphan thread, no tick freeze (the tick-342 fix done right).
# It runs in the creature's world (cwd = _creature_root, the same ground tool_bash stands on): a skill's
# './journal.md' is the very file its bash and write_file see. It used to run at the repo root, so the
# creature was gaslit by its own body — write_file put the file in its home, the skill then opened the
# same relative path against the repo and FileNotFoundError'd on a file it had just made.
EXEC_HARNESS_TEMPLATE = '''\
import sys, os, json as _json
sys.path.insert(0, {kairos})   # repo modules import from here — inserted BEFORE any chdir moves the world
from config import load_config, Config
from tools import ToolResult
_SENT = "__EIDOS_SKILL_RESULT__"
def _emit(obj):
    sys.stdout.write(_SENT + _json.dumps(obj) + "\\n"); sys.stdout.flush()
try:
    cfg = load_config(os.path.join({kairos}, "config.toml"))
    cfg.pillars_skill_composition_enabled = {compose}   # 3.3: match the live atom namespace (call in scope)
    if not os.path.isabs(cfg.embedding_model_dir):
        # the embedding model is repo skeleton, not world — pin it before the chdir un-anchors it
        cfg.embedding_model_dir = os.path.join({kairos}, cfg.embedding_model_dir)
    os.chdir({home})   # the creature's world: a skill's relative paths resolve in its home, never the repo
    args = _json.loads(sys.argv[1]) if len(sys.argv) > 1 else {{}}
    code = open({skill_file}, encoding="utf-8").read()
    ns = {{"Config": Config, "ToolResult": ToolResult}}
    try:
        from skill_atoms import build_atoms
        ns.update(build_atoms(cfg))
    except Exception:
        pass
    exec(compile(code, {skill_file}, "exec"), ns)
    fn = ns.get({func!r})
    if fn is None:
        _emit({{"kind": "load_error", "msg": "{func} not defined"}}); sys.exit(0)
    r = fn(args, cfg)
    if isinstance(r, ToolResult):
        _emit({{"kind": "ok", "output": r.output, "success": bool(r.success),
                "duration_s": float(r.duration_s), "fail_kind": r.fail_kind}})
    else:
        _emit({{"kind": "bad_result", "type": type(r).__name__,
                "repr": repr(r)[:2000]}})
except SystemExit:
    raise
except Exception as e:
    import traceback
    _emit({{"kind": "raised", "type": type(e).__name__, "msg": str(e)[:500],
            "tb": traceback.format_exc()[-1000:]}})
'''

_EXEC_SENTINEL = "__EIDOS_SKILL_RESULT__"


def derived_timeout_s(config: Config, entry: Optional[dict]) -> float:
    """Per-skill timeout = measured p95 × multiplier, clamped to [floor, ceiling] (declared knobs).
    With no telemetry yet (a fresh skill) we start at the ceiling — generous until it has a record,
    then it tightens toward its own measured latency. This replaces the guessed 30s wall-clock (§0.4:
    a constant is a guess; derive it from data)."""
    floor = float(getattr(config, "pillars_skill_timeout_floor_s", 5.0))
    ceiling = float(getattr(config, "pillars_skill_timeout_ceiling_s", 60.0))
    p95 = _percentile((entry or {}).get("latency_samples") or [], 95.0)
    if p95 is None:
        return ceiling
    return max(floor, min(ceiling, p95 * _SKILL_TIMEOUT_MULTIPLIER))


def run_skill_killable(config: Config, name: str, args: dict, timeout_s: float) -> ToolResult:
    """Run one skill invocation in a fresh subprocess bounded by a HARD wall-clock kill.

    Kill is guaranteed because the work lives in a *separate OS process*: on overrun we terminate it and,
    after a short grace, SIGKILL its whole tree (`_kill_pid_tree`, the same routine tool_bash uses). No
    Python thread is abandoned, nothing keeps running detached, and the tick loop is freed the instant
    the timeout fires. Telemetry (latency, arg-shape success) is recorded by the caller.
    """
    skill_file = _skill_file(config, name, _active_version(config, name))
    harness = EXEC_HARNESS_TEMPLATE.format(
        kairos=repr(str(KAIROS_DIR)),
        home=repr(str(_creature_root(config))),   # the skill's world — the same cwd rule as tool_bash
        skill_file=repr(str(skill_file)),
        func=f"tool_{name}",
        compose=repr(bool(getattr(config, "pillars_skill_composition_enabled", False))),
    )
    # A per-invocation harness file (name-scoped) so concurrent skills don't clobber each other's harness.
    harness_path = _skills_dir(config) / f".exec_{name}.py"
    try:
        harness_path.write_text(harness, encoding="utf-8")
    except OSError as e:
        return ToolResult(output=f"[skill '{name}'] could not stage runner: {e}",
                          full_output_path=None, success=False, duration_s=0, fail_kind="crash")

    try:
        args_json = json.dumps(args, default=str)
    except Exception:  # noqa: BLE001
        args_json = "{}"

    t = time.monotonic()
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(harness_path), args_json],
            cwd=str(_creature_root(config)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            **({"start_new_session": True} if os.name != "nt"
               else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}),
        )
    except Exception as e:  # noqa: BLE001
        _unlink(harness_path)
        return ToolResult(output=f"[skill '{name}'] could not launch subprocess: {type(e).__name__}: {e}",
                          full_output_path=None, success=False, duration_s=time.monotonic() - t,
                          fail_kind="crash")

    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # HARD KILL: terminate, then after a grace SIGKILL the whole process tree so no orphan survives.
        _kill_pid_tree(proc.pid)
        try:
            proc.communicate(timeout=_SUBPROC_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.communicate(timeout=_SUBPROC_KILL_GRACE_S)
            except Exception:  # noqa: BLE001
                pass
        _unlink(harness_path)
        return ToolResult(
            output=(
                f"WATCHDOG: skill '{name}' ran past {timeout_s:.0f}s and was KILLED (its whole "
                f"subprocess tree was terminated — no orphan is left running, unlike the old thread "
                f"watchdog that could only abandon a hung thread). The tick loop was never blocked. FIX "
                f"THE SKILL with edit_skill: put an explicit timeout on EVERY network / HTTP / socket / "
                f"subprocess call, or compose the bounded primitives (tcp_probe / http_probe / net_scan / "
                f"udp_listen). For genuinely long work, dispatch it with bash/bg_run instead of inline."),
            full_output_path=None, success=False, duration_s=time.monotonic() - t, fail_kind="timeout")
    finally:
        _unlink(harness_path)

    dur = time.monotonic() - t
    verdict = _parse_exec_stdout(out)
    if verdict is None:
        detail = ((out or "").strip()[-300:] or (err or "").strip()[-300:])
        return ToolResult(output=f"[skill '{name}'] produced no result. {detail}",
                          full_output_path=None, success=False, duration_s=dur, fail_kind="crash")
    kind = verdict.get("kind")
    if kind == "ok":
        return ToolResult(output=verdict.get("output"), full_output_path=None,
                          success=bool(verdict.get("success")),
                          duration_s=float(verdict.get("duration_s") or dur),
                          fail_kind=verdict.get("fail_kind") or "")
    if kind == "bad_result":
        # Should be unreachable once the authoring contract check lands, but a pre-existing skill or one
        # authored while the flag was off can still be mis-typed — fail typed, never crash the tick loop.
        return ToolResult(output=f"[skill '{name}' returned {verdict.get('type')}, not a ToolResult] "
                                 f"{verdict.get('repr','')}",
                          full_output_path=None, success=False, duration_s=dur, fail_kind="crash")
    if kind == "load_error":
        return ToolResult(output=f"[skill '{name}' load error] {verdict.get('msg','')}",
                          full_output_path=None, success=False, duration_s=dur, fail_kind="crash")
    # raised
    return ToolResult(output=f"[skill '{name}' raised] {verdict.get('type')}: {verdict.get('msg','')}",
                      full_output_path=None, success=False, duration_s=dur, fail_kind="crash")


def _parse_exec_stdout(out: Optional[str]) -> Optional[dict]:
    """Pull the one sentinel-prefixed JSON verdict line out of the subprocess stdout (the skill may have
    printed its own noise; the sentinel isolates our line)."""
    for ln in reversed((out or "").splitlines()):
        if ln.startswith(_EXEC_SENTINEL):
            try:
                return json.loads(ln[len(_EXEC_SENTINEL):])
            except Exception:  # noqa: BLE001
                return None
    return None


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _active_version(config: Config, name: str) -> str:
    ent = _load_manifest(config).get("skills", {}).get(name) or {}
    return str(ent.get("active_version") or "1.0.0")


# ---------------------------------------------------------------------------
# Telemetry helpers (Pillars 1.2)
# ---------------------------------------------------------------------------

def _percentile(samples: list, pct: float) -> Optional[float]:
    """Nearest-rank percentile over a numeric sample list. Returns None on an empty list."""
    xs = sorted(float(x) for x in samples if isinstance(x, (int, float)))
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, math.ceil(pct / 100.0 * len(xs)) - 1))
    return xs[k]


def _arg_shape(args: Optional[dict]) -> str:
    """A stable signature of an invocation's ARG SHAPE (the sorted set of keys present) so success can be
    tracked per shape — a skill that works when called with {url} but not {ip} is a different story than
    a flat success rate. Not the values (unbounded), just which args were supplied."""
    if not isinstance(args, dict) or not args:
        return "()"
    return "(" + ",".join(sorted(str(k) for k in args)) + ")"


# ---------------------------------------------------------------------------
# Pillars 3.1/3.2: skill economy — embedding similarity, trust, affordances,
# similarity-priced authoring, reuse-favouring XP, auto-retire. All the callers
# are dark behind config flags; these helpers are inert until a flag turns them on.
# ---------------------------------------------------------------------------

def _skill_text(name: str, ent: dict) -> str:
    """The text an existing skill is embedded from: its name + description + arg-shape keys. This is
    what a situation is matched AGAINST (3.1) and what a candidate new skill is compared to for the
    novelty price (3.2). Name is included because the model's names are meaningful (check_mqtt_port)."""
    parts = [name.replace("_", " ")]
    desc = (ent.get("description") or "").strip()
    if desc:
        parts.append(desc)
    schema = ent.get("args_schema") or {}
    if isinstance(schema, dict) and schema:
        parts.append(" ".join(str(k) for k in schema))
    return " ".join(parts).strip()


def _skill_trust(ent: dict) -> float:
    """Trust ∈ [0,1] from the manifest: a status prior × the observed success rate (floored so a live
    skill with no record yet is still visible). A quarantined/disabled/retired skill scores 0 — it is
    not an affordance. This is the deterministic 'how much do I lean on this' the ranker multiplies by."""
    prior = _TRUST_STATUS_WEIGHT.get(str(ent.get("status") or ""), 0.0)
    if prior <= 0.0:
        return 0.0
    inv = int(ent.get("invocations", 0) or 0)
    suc = int(ent.get("successes", 0) or 0)
    rate = (suc / inv) if inv > 0 else _TRUST_RATE_FLOOR
    rate = max(_TRUST_RATE_FLOOR, min(1.0, rate))
    return prior * rate


def _embed_or_none(config: Config, text: str):
    """One-text embedding via embedding.embed_query (mock-aware, fail-open). Returns a (D,) vector or
    None — None means 'no semantic signal available', and every caller degrades gracefully to that."""
    if not (text or "").strip():
        return None
    try:
        import embedding
        return embedding.embed_query(config, text)
    except Exception:  # noqa: BLE001 - embeddings are optional; never let them break a skill path
        return None


def _cosine(a, b) -> float:
    """Cosine similarity of two vectors (they arrive L2-normalised from embed_query, so this is a dot;
    we normalise defensively anyway). Returns 0.0 on any failure."""
    try:
        import numpy as np
        a = np.asarray(a, dtype=np.float64)
        b = np.asarray(b, dtype=np.float64)
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
    except Exception:  # noqa: BLE001
        return 0.0


def _max_similarity_to_existing(config: Config, description: str,
                                name: str = "", skills: Optional[dict] = None) -> float:
    """Max cosine similarity of a candidate skill (name + description) to any EXISTING live skill,
    ∈ [0,1] (clamped; embeddings can dip slightly negative). This is the novelty signal that prices
    authoring: ~0 = fully novel (cheap), ~1 = near-duplicate (expensive). Returns 0.0 when there is
    no semantic signal (no model / no existing skills) so a missing embedder never inflates the price."""
    if skills is None:
        skills = _load_manifest(config).get("skills", {})
    cand = _embed_or_none(config, (name.replace("_", " ") + " " + (description or "")).strip())
    if cand is None:
        return 0.0
    best = 0.0
    for ex, ent in skills.items():
        if ex == name or ent.get("status") not in _LIVE_STATUSES:
            continue
        vec = _embed_or_none(config, _skill_text(ex, ent))
        if vec is None:
            continue
        best = max(best, _cosine(cand, vec))
    return max(0.0, min(1.0, best))


def _author_energy_cost(config: Config, similarity: float) -> float:
    """Similarity-priced authoring cost: base × (1 + (max_mult - 1) × similarity). A fully-novel skill
    (similarity 0) costs the base; a near-duplicate (similarity → 1) costs base × _AUTHOR_DUP_MAX_MULTIPLIER.
    The price rises monotonically with similarity — THAT is the dedup pressure (the economics replace the
    old hard veto with a cost the creature feels in its energy reserve)."""
    base = float(getattr(config, "pillars_skill_author_energy_cost", 0.02))
    s = max(0.0, min(1.0, float(similarity)))
    return base * (1.0 + (_AUTHOR_DUP_MAX_MULTIPLIER - 1.0) * s)


def _charge_energy(config: Config, cost: float) -> None:
    """Drain `cost` from the metabolic reserve (feed a negative amount). Bound to config so the drain
    PERSISTS to workspace/state/metabolism.json — the same reserve the tick loop charges. Best-effort:
    a missing/short reserve never blocks authoring (the price is a pressure, not a gate)."""
    if cost <= 0.0:
        return
    try:
        from nervous.metabolism import Metabolism
        config.state_dir.mkdir(parents=True, exist_ok=True)   # so the reserve file has a home
        met = Metabolism(config=config)
        met.feed(-abs(float(cost)))
        met._save()   # feed() only persists on its own save cadence; force it so the drain survives
    except Exception as e:  # noqa: BLE001
        logger.warning("could not charge authoring energy (%.4f): %s", cost, e)


def _award_persona_xp(config: Config, amount: int, reason: str) -> None:
    """Award XP through persona.award_xp, persisted to workspace/persona.json (load → award → save).
    Best-effort — XP is an incentive signal, never load-bearing for the skill mechanics themselves."""
    if amount <= 0:
        return
    try:
        import persona
        p = persona.load_persona(config.workspace)
        persona.award_xp(p, int(amount), reason)
        persona.save_persona(config.workspace, p)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not award skill XP (%s): %s", reason, e)


def retire_unused_skills(config: Config, now_s: Optional[float] = None) -> list[str]:
    """3.2(c) Auto-retire: archive any LIVE skill whose last use is older than
    `pillars_skill_retire_unused_days`. Archived = status 'retired', disabled, popped from the live
    registry — so it vanishes from both `skills_brief` and the affordance list. It is NOT deleted:
    the versioned file and manifest entry remain, so `rollback_skill` revives it (recoverable). A
    skill that was never used is dated from its creation. Returns the names retired. No-op when the
    economy flag is off. Idempotent — an already-retired skill is skipped."""
    if not getattr(config, "pillars_skill_economy_enabled", False):
        return []
    days = float(getattr(config, "pillars_skill_retire_unused_days", 30.0))
    if days <= 0:
        return []
    cutoff = (now_s if now_s is not None else time.time()) - days * 86400.0
    out: list[str] = []
    try:
        m = _load_manifest(config)
        for name, ent in m.get("skills", {}).items():
            if ent.get("status") not in _LIVE_STATUSES:
                continue
            stamp = ent.get("last_used") or ent.get("created")
            last = _parse_iso(stamp)
            if last is None or last >= cutoff:
                continue
            ent["status"] = _RETIRED_STATUS
            ent["enabled"] = False
            ent["retired_at"] = _now()
            ent["updated"] = _now()
            TOOLS.pop(name, None)
            out.append(name)
        if out:
            _save_manifest(config, m)
            logger.info("auto-retired %d unused skill(s): %s", len(out), ", ".join(out))
    except Exception as e:  # noqa: BLE001
        logger.warning("retire_unused_skills failed: %s", e)
    return out


def _parse_iso(stamp: Optional[str]) -> Optional[float]:
    """Parse a manifest UTC timestamp ('%Y-%m-%dT%H:%M:%SZ', as _now() writes) to epoch seconds, or
    None on anything unparseable (so a missing/garbage stamp never accidentally retires a skill)."""
    if not stamp:
        return None
    try:
        return time.mktime(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return None


def skill_affordances(config: Config, situation: str,
                      k: Optional[int] = None, _rng=None) -> list[dict]:
    """3.1 Affordances: the top-K existing skills most relevant to the CURRENT situation, ranked by
    similarity × trust × birth-episode-strength (the last STUBBED at 1.0, TODO S-6). Similarity is
    cosine over MiniLM embeddings of each skill's text vs `situation`; trust is `_skill_trust`.

    Exploration ε (anti-Matthew, §6): with probability `_AFFORDANCE_EXPLORE_EPS` the LAST slot is given
    to a COLD skill (never used, or untrusted) drawn at random from those not already selected — so a
    low-strength skill can still surface and earn instead of the list ossifying into the same top few.

    Returns a list of {name, score, similarity, trust, explore} dicts (score-descending; the explore
    entry, when present, carries explore=True and sits last). Empty when the flag is off, there are no
    live skills, or no semantic signal is available. Never raises."""
    if not getattr(config, "pillars_skill_affordances_enabled", False):
        return []
    if k is None:
        k = int(getattr(config, "pillars_skill_affordance_k", 3))
    if k <= 0:
        return []
    try:
        import random
        rng = _rng or random
        m = _load_manifest(config)
        skills = {n: e for n, e in m.get("skills", {}).items()
                  if e.get("status") in _LIVE_STATUSES}
        if not skills:
            return []
        qv = _embed_or_none(config, situation)
        if qv is None:
            return []
        scored: list[dict] = []
        for name, ent in skills.items():
            vec = _embed_or_none(config, _skill_text(name, ent))
            if vec is None:
                continue
            sim = max(0.0, _cosine(qv, vec))
            trust = _skill_trust(ent)
            score = sim * trust * _BIRTH_EPISODE_STRENGTH
            scored.append({"name": name, "score": round(score, 6),
                           "similarity": round(sim, 6), "trust": round(trust, 6),
                           "explore": False})
        if not scored:
            return []
        scored.sort(key=lambda d: d["score"], reverse=True)

        # Exploration ε: occasionally hand the last slot to a COLD skill so it can earn. A cold skill =
        # never used (invocations 0) or untrusted (status 'active', not yet 'trusted'). We only spend the
        # slot if such a skill exists OUTSIDE the exploit top-(k-1) — otherwise there's nothing to explore.
        top = scored[:k]
        if k >= 1 and rng.random() < _AFFORDANCE_EXPLORE_EPS:
            keep = scored[:max(0, k - 1)]
            keep_names = {d["name"] for d in keep}
            cold = [d for d in scored
                    if d["name"] not in keep_names
                    and (int(skills[d["name"]].get("invocations", 0) or 0) == 0
                         or skills[d["name"]].get("status") != "trusted")]
            if cold:
                pick = dict(rng.choice(cold))
                pick["explore"] = True
                top = keep + [pick]
        return top
    except Exception as e:  # noqa: BLE001 - affordances are additive; never break the context build
        logger.warning("skill_affordances failed: %s", e)
        return []


def render_affordances(affordances: list[dict]) -> str:
    """Render affordances as a compact 'tools at hand' block for the decision point (context.py). Empty
    string for an empty list (so the caller can `if block:`). Distinct from `skills_brief` — this is the
    situation-ranked shortlist, not the full alphabet."""
    if not affordances:
        return ""
    lines = ["## Tools at hand — these existing skills fit what you're doing RIGHT NOW. "
             "CALL one (e.g. <tool>{}</tool>) before authoring anything new.".format(affordances[0]["name"])]
    for a in affordances:
        tag = " (untried — give it a shot)" if a.get("explore") else ""
        lines.append(f"- {a['name']}{tag}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hot-load & invocation tracking
# ---------------------------------------------------------------------------

def _build_runner(config: Config, name: str, code: str):
    """Compile skill source and return a registry-compatible callable.

    Flag-OFF (default): the skill runs IN-PROCESS (so it can do real work: network, subprocess,
    devices) under the caller's thread-watchdog — byte-for-byte the historical path.
    Flag-ON (`pillars_killable_skills_enabled`): each call runs in a fresh, HARD-KILLABLE subprocess
    (`run_skill_killable`) so a hang dies with the process instead of freezing the tick loop.
    Either way, exceptions become a failed ToolResult and usage + latency + arg-shape are recorded.
    """
    ns = {"Config": Config, "ToolResult": ToolResult}
    try:
        ns.update(build_atoms(config))   # M2.1: atoms in scope so skills compose them, never `import requests`
    except Exception as e:  # noqa: BLE001 - atoms are additive; never block a skill from loading
        logger.warning("could not build atoms for skill '%s': %s", name, e)
    exec(compile(code, f"<skill:{name}>", "exec"), ns)  # noqa: S102 - trusted local agent code
    fn = ns[f"tool_{name}"]

    def runner(args: dict, cfg: Config) -> ToolResult:
        t = time.monotonic()
        if getattr(cfg, "pillars_killable_skills_enabled", False):
            # Killable path: run in a subprocess we can hard-kill. The derived per-skill timeout
            # (p95×3, clamped) replaces the guessed 30s wall-clock.
            timeout_s = derived_timeout_s(cfg, _load_manifest(cfg).get("skills", {}).get(name))
            res = run_skill_killable(cfg, name, args or {}, timeout_s)
            _record_invocation(cfg, name, res.success,
                               latency_s=res.duration_s, args=args)
            return res
        try:
            res = fn(args, cfg)
            if not isinstance(res, ToolResult):
                res = ToolResult(output=str(res), full_output_path=None,
                                 success=True, duration_s=time.monotonic() - t)
        except Exception as e:  # noqa: BLE001 - skills must never crash the loop
            res = ToolResult(output=f"[skill '{name}' raised] {type(e).__name__}: {e}",
                             full_output_path=None, success=False,
                             duration_s=time.monotonic() - t)
        _record_invocation(cfg, name, res.success,
                           latency_s=res.duration_s, args=args)
        return res

    runner.__name__ = f"skill_{name}"
    return runner


def _record_invocation(config: Config, name: str, ok: bool,
                       latency_s: Optional[float] = None, args: Optional[dict] = None) -> None:
    try:
        m = _load_manifest(config)
        ent = m["skills"].get(name)
        if not ent:
            return
        ent["invocations"] = ent.get("invocations", 0) + 1
        if ok:
            ent["successes"] = ent.get("successes", 0) + 1
        # Pillars 1.2 telemetry: rolling latency window (feeds p50/p95 + the derived timeout), the
        # last-used timestamp, and per-arg-shape success (which call shapes actually work). Bounded so
        # the manifest can't grow without limit. Kept alongside the existing invocation/success stats.
        if latency_s is not None:
            samples = ent.setdefault("latency_samples", [])
            samples.append(round(float(latency_s), 4))
            if len(samples) > _TELEMETRY_LATENCY_SAMPLES:
                del samples[:-_TELEMETRY_LATENCY_SAMPLES]
        ent["last_used"] = _now()
        shape = _arg_shape(args)
        by_shape = ent.setdefault("arg_shapes", {}).setdefault(shape, {"invocations": 0, "successes": 0})
        by_shape["invocations"] += 1
        if ok:
            by_shape["successes"] += 1
        # Per-VERSION stats: trust/quarantine judge the code that is actually running. Lifetime
        # totals survive edits, so a once-good skill kept its trusted badge through a broken
        # rewrite, and a fixed skill kept dragging its broken ancestors' zeros.
        ver = str(ent.get("active_version") or "?")
        vs = ent.setdefault("version_stats", {}).setdefault(ver, {"invocations": 0, "successes": 0})
        vs["invocations"] += 1
        if ok:
            vs["successes"] += 1
        inv, suc = vs["invocations"], vs["successes"]
        status = ent.get("status")
        if status in _LIVE_STATUSES and inv >= _QUARANTINE_MIN_USES and suc == 0:
            # Dead on arrival: N uses, zero successes — stop offering it as a tool. The model
            # can revive it with edit_skill (which re-activates) or rollback_skill.
            ent["status"] = "quarantined"
            ent["enabled"] = False
            TOOLS.pop(name, None)
            logger.warning("skill '%s' v%s quarantined (0/%d successes)", name, ver, inv)
        elif status == "active" and inv >= _TRUST_MIN_USES and (suc / inv) >= _TRUST_MIN_RATE:
            ent["status"] = "trusted"
            logger.info("skill '%s' promoted to trusted (%d/%d on v%s)", name, suc, inv, ver)
        elif status == "trusted" and inv >= _TRUST_MIN_USES and (suc / inv) < _DEMOTE_RATE:
            ent["status"] = "active"
            logger.info("skill '%s' demoted from trusted (%d/%d on v%s)", name, suc, inv, ver)
        _save_manifest(config, m)
        # 3.2(b) Reuse pays: a SUCCESSFUL call of an existing skill grants _XP_REUSE (> _XP_CREATE), so
        # the settled economy rewards reuse over creation. Fires once per successful invocation, only
        # under the economy flag (flag-off = no XP side effect, historical behaviour).
        if ok and getattr(config, "pillars_skill_economy_enabled", False):
            _award_persona_xp(config, _XP_REUSE, f"reuse:{name}")
    except Exception as e:  # noqa: BLE001
        logger.warning("could not record invocation for %s: %s", name, e)


def _activate(config: Config, name: str, version: str) -> tuple[bool, str]:
    """Load the given version into the live TOOLS registry."""
    try:
        code = _skill_file(config, name, version).read_text(encoding="utf-8")
    except OSError as e:
        return False, f"cannot read skill file: {e}"
    try:
        TOOLS[name] = _build_runner(config, name, code)
    except Exception as e:  # noqa: BLE001 - import/exec failure
        return False, f"activation failed: {type(e).__name__}: {e}"
    return True, "active"


def prune_dead_skills(config: Config) -> list[str]:
    """Quarantine skills whose LIFETIME record is N+ uses with zero successes (catches
    pre-existing dead weight from before per-version tracking). Returns names quarantined."""
    out: list[str] = []
    try:
        m = _load_manifest(config)
        for name, ent in m.get("skills", {}).items():
            if (ent.get("status") in _LIVE_STATUSES
                    and ent.get("invocations", 0) >= _QUARANTINE_MIN_USES
                    and ent.get("successes", 0) == 0):
                ent["status"] = "quarantined"
                ent["enabled"] = False
                ent["updated"] = _now()
                TOOLS.pop(name, None)
                out.append(name)
        if out:
            _save_manifest(config, m)
            logger.warning("quarantined %d dead skill(s): %s", len(out), ", ".join(out))
    except Exception as e:  # noqa: BLE001
        logger.warning("prune_dead_skills failed: %s", e)
    return out


def load_active_skills(config: Config) -> list[str]:
    """Hot-load every enabled skill at startup. Returns names loaded."""
    prune_dead_skills(config)   # sweep dead weight first so it never reaches the registry
    loaded: list[str] = []
    m = _load_manifest(config)
    for name, ent in m.get("skills", {}).items():
        if not ent.get("enabled"):
            continue
        ver = ent.get("active_version")
        if not ver:
            continue
        ok, msg = _activate(config, name, ver)
        if ok:
            loaded.append(name)
        else:
            logger.warning("skip skill '%s': %s", name, msg)
    if loaded:
        logger.info("loaded %d skill(s): %s", len(loaded), ", ".join(loaded))
    return loaded


# ---------------------------------------------------------------------------
# Public API (called by the create_skill / edit_skill / ... tools)
# ---------------------------------------------------------------------------

# Generic verb/qualifier tokens that don't identify a skill's DOMAIN.
_SKILL_STOPWORDS = {
    "check", "test", "get", "poll", "scan", "probe", "update", "watch", "verify", "wait",
    "save", "register", "identify", "resolve", "list", "read", "write", "run", "make", "set",
    "fetch", "query", "monitor", "detect", "find", "discover", "connect", "status", "state",
    "info", "data", "map", "value", "result", "active", "readiness", "conn", "connection",
    "port", "ports", "the", "for", "and", "with", "from", "into",
}


def _skill_subject(name: str) -> set:
    """Domain nouns in a skill name (mqtt, octoprint, cert, network…), minus generic verbs."""
    return {t for t in re.split(r"[_\W]+", (name or "").lower())
            if len(t) >= 4 and t not in _SKILL_STOPWORDS}


def _similar_skills(name: str, skills: dict) -> list:
    """Existing active skills that share a domain noun with `name` (likely duplicates)."""
    subj = _skill_subject(name)
    if not subj:
        return []
    out = []
    for ex, ent in skills.items():
        if ex == name or ent.get("status") not in _LIVE_STATUSES:
            continue
        if _skill_subject(ex) & subj:
            out.append(ex)
    return out


def skills_brief(config: Config, max_n: int = 45) -> str:
    """Compact, recency-sorted list of active skill names for per-tick context awareness."""
    m = _load_manifest(config)
    sk = m.get("skills", {})
    # Both active AND trusted — filtering on just "active" made a skill DISAPPEAR from the
    # per-tick brief the moment it earned trust (so the best skills were the invisible ones).
    names = [n for n, e in sk.items() if e.get("status") in _LIVE_STATUSES]
    if not names:
        return ""
    names.sort(key=lambda n: sk[n].get("updated", ""), reverse=True)
    shown = names[:max_n]
    s = ", ".join(shown)
    if len(names) > max_n:
        s += f", …(+{len(names) - max_n} more)"
    return s


def create_skill(config: Config, name: str, code: str,
                 description: str = "", args_schema: Optional[dict] = None) -> dict:
    """Author a brand-new skill. Validates, dry-runs, saves, and hot-loads it."""
    name = (name or "").strip()
    m = _load_manifest(config)
    if name in m["skills"]:
        return {"success": False, "errors": [f"skill '{name}' already exists — use edit_skill"]}

    _economy = getattr(config, "pillars_skill_economy_enabled", False)
    # Near-duplicate guard — the #1 source of wasted motion was authoring a 7th MQTT/OctoPrint
    # skill instead of calling the one already built. Block domain-duplicates; redirect to reuse.
    dup = _similar_skills(name, m["skills"])
    warnings: list[str] = []
    if dup:
        dup_msg = (
            f"You ALREADY have skill(s) for this domain: {', '.join(sorted(dup)[:6])}. "
            f"CALL one as a tool (e.g. <tool>{sorted(dup)[0]}</tool>), or use edit_skill to improve "
            f"it. Do NOT author a near-duplicate — reuse what you built.")
        if not _economy:
            # 3.2-OFF (historical): the domain-name overlap is a hard VETO.
            return {"success": False, "errors": [dup_msg]}
        # 3.2-ON: the veto becomes a WARNING — the ECONOMIC price (below) is now the dedup pressure,
        # so a genuinely-distinct skill that merely shares a domain noun is no longer blocked outright.
        warnings.append(dup_msg)

    errs = _validate_source(name, code, sandbox=getattr(config, "skill_sandbox_enabled", True))
    if errs:
        return {"success": False, "errors": errs}
    # 3.3: reject a cyclic composition (or a `call` used with the flag off) at authoring — before runtime.
    cerrs = _validate_composition(config, name, code)
    if cerrs:
        return {"success": False, "errors": cerrs}

    # 3.2(a) Similarity-priced authoring: charge metabolic energy scaling with how close this new skill
    # is to an existing one (embedding cosine). Novel ≈ base cost; near-duplicate ≈ base × up to _AUTHOR_
    # DUP_MAX_MULTIPLIER. The PRICE is the dedup pressure. Charged only once the source validates (so a
    # rejected skill isn't billed) and only under the economy flag. Similarity 0 when no embedder → base.
    sim_to_existing = 0.0
    author_cost = 0.0
    if _economy:
        sim_to_existing = _max_similarity_to_existing(config, description, name=name, skills=m["skills"])
        author_cost = _author_energy_cost(config, sim_to_existing)
        _charge_energy(config, author_cost)

    version = "1.0.0"
    _skill_file(config, name, version).write_text(code, encoding="utf-8")

    # Pillars 1.2: under the killable path, dry-run CALLS the skill with schema-shaped sample args and
    # REJECTS a non-ToolResult return at authoring time (flag-off keeps the lenient historical dry-run).
    _strict = getattr(config, "pillars_killable_skills_enabled", False)
    ok, note = _dry_run(config, name, version, args_schema=args_schema, strict_contract=_strict)
    if not ok:
        _skill_file(config, name, version).unlink(missing_ok=True)
        return {"success": False, "errors": [f"dry-run rejected: {note}"]}

    act_ok, act_msg = _activate(config, name, version)
    m["skills"][name] = {
        "description": description, "args_schema": args_schema or {},
        "author": "agent", "active_version": version, "versions": [version],
        "enabled": act_ok, "status": "active" if act_ok else "disabled",
        "created": _now(), "updated": _now(), "invocations": 0, "successes": 0,
    }
    _save_manifest(config, m)

    if not act_ok:
        return {"success": False, "errors": [f"saved but activation failed: {act_msg}"],
                "skill": name, "version": version}
    # 3.2(b): authoring grants _XP_CREATE — deliberately LESS than a reuse (_XP_REUSE), so farming XP by
    # authoring is a poor strategy vs. calling what already exists. Only under the economy flag.
    if _economy:
        _award_persona_xp(config, _XP_CREATE, f"create:{name}")
    out = {"success": True, "skill": name, "version": version,
           "status": "active", "note": note,
           "message": f"Skill '{name}' v{version} is live — call it as <tool>{name}</tool>. ({note})"}
    if _economy:
        out["author_energy_cost"] = round(author_cost, 5)
        out["max_similarity"] = round(sim_to_existing, 4)
        if warnings:
            out["warnings"] = warnings
    return out


def edit_skill(config: Config, name: str, code: str,
               description: Optional[str] = None, args_schema: Optional[dict] = None) -> dict:
    """Replace a skill's implementation with a new version (old versions kept for rollback)."""
    name = (name or "").strip()
    m = _load_manifest(config)
    ent = m["skills"].get(name)
    if not ent:
        return {"success": False, "errors": [f"no skill '{name}' — use create_skill"]}

    errs = _validate_source(name, code, sandbox=getattr(config, "skill_sandbox_enabled", True))
    if errs:
        return {"success": False, "errors": errs}
    # 3.3: an edit that turns a skill into a cyclic composition is rejected here (the good version stays).
    cerrs = _validate_composition(config, name, code)
    if cerrs:
        return {"success": False, "errors": cerrs, "kept_active": ent.get("active_version")}

    version = _next_version(ent)
    _skill_file(config, name, version).write_text(code, encoding="utf-8")

    # Pillars 1.2 contract check (see create_skill). An edited args_schema (or the stored one) shapes
    # the sample call; only the killable path enforces the ToolResult return.
    _strict = getattr(config, "pillars_killable_skills_enabled", False)
    _schema = args_schema if args_schema is not None else ent.get("args_schema")
    ok, note = _dry_run(config, name, version, args_schema=_schema, strict_contract=_strict)
    if not ok:
        _skill_file(config, name, version).unlink(missing_ok=True)
        return {"success": False, "errors": [f"dry-run rejected: {note}"],
                "kept_active": ent.get("active_version")}

    act_ok, act_msg = _activate(config, name, version)
    if not act_ok:
        _skill_file(config, name, version).unlink(missing_ok=True)
        return {"success": False, "errors": [f"activation failed: {act_msg}"],
                "kept_active": ent.get("active_version")}

    ent["versions"] = sorted(set(ent.get("versions", []) + [version]))
    ent["active_version"] = version
    ent["enabled"] = True
    # New code = clean slate: a quarantined skill comes back to life, and a trusted one
    # must RE-EARN trust on the new version (trust used to survive broken rewrites).
    ent["status"] = "active"
    ent["updated"] = _now()
    if description is not None:
        ent["description"] = description
    if args_schema is not None:
        ent["args_schema"] = args_schema
    _save_manifest(config, m)
    return {"success": True, "skill": name, "version": version, "status": ent.get("status"),
            "message": f"Skill '{name}' updated to v{version} and live. ({note})"}


def rollback_skill(config: Config, name: str, version: str) -> dict:
    """Make a previously-saved version the active one again."""
    name = (name or "").strip()
    m = _load_manifest(config)
    ent = m["skills"].get(name)
    if not ent:
        return {"success": False, "errors": [f"no skill '{name}'"]}
    if version not in ent.get("versions", []):
        return {"success": False, "errors": [f"version {version} not found; have {ent.get('versions')}"]}
    ok, msg = _activate(config, name, version)
    if not ok:
        return {"success": False, "errors": [msg]}
    ent["active_version"] = version
    ent["enabled"] = True
    if ent.get("status") == "quarantined":
        ent["status"] = "active"     # rolling back to a known-better version revives it
    ent["updated"] = _now()
    _save_manifest(config, m)
    return {"success": True, "skill": name, "version": version,
            "message": f"Rolled '{name}' back to v{version} (live)."}


def list_skills(config: Config) -> dict:
    """Summarize built-in tools and all authored skills."""
    m = _load_manifest(config)
    skills = {}
    for name, ent in m.get("skills", {}).items():
        inv, suc = ent.get("invocations", 0), ent.get("successes", 0)
        ver = str(ent.get("active_version") or "?")
        vs = (ent.get("version_stats") or {}).get(ver, {})
        v_inv, v_suc = vs.get("invocations", 0), vs.get("successes", 0)
        # Pillars 1.2 telemetry surfacing: latency p50/p95, per-arg-shape success, last-used, and the
        # derived per-skill timeout (p95×3 clamped) — so `list_skills` shows the measured record.
        samples = ent.get("latency_samples") or []
        p50 = _percentile(samples, 50.0)
        p95 = _percentile(samples, 95.0)
        by_shape = {
            shape: {"uses": s.get("invocations", 0),
                    "success_rate": round(s["successes"] / s["invocations"], 2) if s.get("invocations") else None}
            for shape, s in (ent.get("arg_shapes") or {}).items()
        }
        skills[name] = {
            "description": ent.get("description", ""),
            "version": ent.get("active_version"),
            "status": ent.get("status"),
            "enabled": ent.get("enabled"),
            "uses": inv, "success_rate": round(suc / inv, 2) if inv else None,
            "active_version_uses": v_inv,
            "active_version_success_rate": round(v_suc / v_inv, 2) if v_inv else None,
            "latency_p50_s": round(p50, 3) if p50 is not None else None,
            "latency_p95_s": round(p95, 3) if p95 is not None else None,
            "success_by_arg_shape": by_shape,
            "last_used": ent.get("last_used"),
            "derived_timeout_s": round(derived_timeout_s(config, ent), 1),
        }
    builtins = sorted(n for n in RESERVED_NAMES if n not in {
        "create_skill", "edit_skill", "list_skills", "rollback_skill"})
    try:
        from tools import visible_tools
        vis = visible_tools(config)
        if vis is not TOOLS:    # the creature ladder is active: a locked name does not exist (§0)
            builtins = [n for n in builtins if n in vis]
    except Exception:  # noqa: BLE001 - fail to the unfiltered (house) listing
        pass
    return {"builtin_tools": builtins, "skills": skills,
            "loaded_in_registry": sorted(n for n in m.get("skills", {}) if n in TOOLS)}


# ---------------------------------------------------------------------------
# Pillars 3.3: the promotion pipeline (composition → candidate queue → atom)
#
# Shape mirrors selfedit.propose()/apply(): eiDOS (or glue) PROPOSES a promotion; the
# operator-controlled dashboard APPLIES it. propose_promotion() only stages a candidate; the actual
# compile-into-the-vocabulary is apply_promotion(), the clean seam the dashboard calls. Everything is
# dark behind pillars_skill_composition_enabled and never mutates skill_atoms.py source — the promoted
# atom lives in a data store (`_promoted_atoms.json`) that build_atoms reads at namespace-build time.
# ---------------------------------------------------------------------------

def _promoted_atoms_path(config: Config) -> Path:
    return _skills_dir(config) / _PROMOTED_ATOMS_NAME


def _load_promoted_atoms(config: Config) -> dict:
    """The compiled promoted-atom store: {name: {source, from_skill, from_version, promoted_at}}. This
    is the mutable extension of the atom vocabulary; build_atoms injects each entry as a callable."""
    try:
        return json.loads(_promoted_atoms_path(config).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_promoted_atoms(config: Config, store: dict) -> None:
    p = _promoted_atoms_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    tmp.replace(p)


def promoted_atom_names(config: Config) -> list[str]:
    """Names currently in the promoted-atom vocabulary (what build_atoms injects on top of ATOM_NAMES)."""
    return sorted(_load_promoted_atoms(config).keys())


def _promotion_candidates_key(m: dict) -> dict:
    return m.setdefault("promotion_candidates", {})


def _is_composition(config: Config, name: str, ent: dict) -> bool:
    """True iff this skill's active source statically calls another skill (i.e. it IS a composition)."""
    try:
        ver = str(ent.get("active_version") or "1.0.0")
        code = _skill_file(config, name, ver).read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(static_calls_in_source(code))


def propose_promotion(config: Config, name: str, rationale: str = "") -> dict:
    """PROPOSE that a trusted, repeatedly-reused composition become an atom. Stages a candidate into the
    manifest's promotion queue (status 'pending'); never compiles anything. The eligibility bar — trusted
    + a composition + ≥ _PROMOTION_MIN_REUSES successful uses — is the automatization bar (a move proven
    by repetition), read from the manifest's typed stats, never self-report. Returns the staged candidate
    or an error. No-op when composition is disabled."""
    if not getattr(config, "pillars_skill_composition_enabled", False):
        return {"success": False, "errors": ["composition is disabled (pillars_skill_composition_enabled=false)"]}
    name = (name or "").strip()
    m = _load_manifest(config)
    ent = m.get("skills", {}).get(name)
    if not ent:
        return {"success": False, "errors": [f"no skill '{name}'"]}
    if ent.get("status") != "trusted":
        return {"success": False, "errors": [f"'{name}' is '{ent.get('status')}', not trusted — "
                                             f"only a trusted composition can be promoted to an atom"]}
    if not _is_composition(config, name, ent):
        return {"success": False, "errors": [f"'{name}' is not a composition (it calls no other skill) — "
                                             f"there is nothing to chunk into an atom"]}
    reuses = int(ent.get("successes", 0) or 0)
    if reuses < _PROMOTION_MIN_REUSES:
        return {"success": False, "errors": [f"'{name}' has {reuses} successful reuse(s); "
                                             f"needs ≥ {_PROMOTION_MIN_REUSES} to be an atom candidate"]}
    cands = _promotion_candidates_key(m)
    cands[name] = {
        "skill": name, "version": str(ent.get("active_version") or "1.0.0"),
        "reuses": reuses, "rationale": (rationale or "")[:500],
        "status": "pending", "proposed_at": _now(),
    }
    _save_manifest(config, m)
    return {"success": True, "candidate": name, "status": "pending",
            "message": f"Promotion of composition '{name}' staged for Dean to approve."}


def list_promotion_candidates(config: Config, status: Optional[str] = None) -> list[dict]:
    """The promotion queue (newest first). `status` filters (e.g. 'pending'). Dashboard-facing read."""
    cands = list(_load_manifest(config).get("promotion_candidates", {}).values())
    if status:
        cands = [c for c in cands if c.get("status") == status]
    cands.sort(key=lambda c: c.get("proposed_at", ""), reverse=True)
    return cands


def apply_promotion(config: Config, name: str) -> dict:
    """DASHBOARD-ONLY (operator approval): compile a pending composition candidate into the promoted-atom
    vocabulary. Reads the candidate's active-version source, stores it in `_promoted_atoms.json` under the
    skill's name, and marks the candidate 'applied'. From now on build_atoms injects `name` as a callable
    atom — a chunk has become a single unit (the promotion-to-atom of the striatal design). The clean seam
    the dashboard calls; mirrors selfedit.apply (propose stages, apply compiles). Re-validates eligibility
    at apply time so a candidate that lost trust/was retired since proposal can't sneak through."""
    if not getattr(config, "pillars_skill_composition_enabled", False):
        return {"success": False, "errors": ["composition is disabled (pillars_skill_composition_enabled=false)"]}
    name = (name or "").strip()
    m = _load_manifest(config)
    cand = _promotion_candidates_key(m).get(name)
    if not cand:
        return {"success": False, "errors": [f"no promotion candidate '{name}' — propose it first"]}
    if cand.get("status") != "pending":
        return {"success": False, "errors": [f"candidate '{name}' is '{cand.get('status')}', not pending"]}
    ent = m.get("skills", {}).get(name)
    if not ent or ent.get("status") != "trusted":
        return {"success": False, "errors": [f"'{name}' is no longer a trusted skill — cannot promote"]}
    ver = str(ent.get("active_version") or "1.0.0")
    try:
        source = _skill_file(config, name, ver).read_text(encoding="utf-8")
    except OSError as e:
        return {"success": False, "errors": [f"cannot read '{name}' v{ver}: {e}"]}

    store = _load_promoted_atoms(config)
    store[name] = {"source": source, "from_skill": name, "from_version": ver,
                   "promoted_at": _now()}
    _save_promoted_atoms(config, store)

    cand["status"] = "applied"
    cand["applied_at"] = _now()
    cand["applied_version"] = ver
    _save_manifest(config, m)
    return {"success": True, "atom": name, "from_version": ver,
            "in_vocabulary": name in _load_promoted_atoms(config),
            "message": f"Composition '{name}' v{ver} compiled into the atom vocabulary."}
