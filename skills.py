"""Self-authored skills: let Nexus write, validate, hot-load, version, and reuse its own tools.

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
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from config import Config
from tools import ToolResult, TOOLS

logger = logging.getLogger("eidos.skills")

KAIROS_DIR = Path(__file__).resolve().parent
SKILLS_SUBDIR = "skills"
MANIFEST_NAME = "_index.json"

# Names the agent may NOT use for a skill (built-in tools + skill-admin tools).
RESERVED_NAMES = {
    "bash", "write_file", "read_file", "bg_run", "bg_check", "http_get",
    "update_plan", "memorize", "recall", "goal_complete",
    "ask_supervisor",
    "create_skill", "edit_skill", "list_skills", "rollback_skill",
}

# Meta-execution builtins forbidden inside skill source (they bypass review).
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_TRUST_MIN_USES = 5
_TRUST_MIN_RATE = 0.8


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

def _validate_source(name: str, code: str) -> list[str]:
    """Static checks. Returns a list of error strings (empty == valid)."""
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


def _dry_run(config: Config, name: str, version: str) -> tuple[bool, str]:
    """Run the saved skill file in a fresh subprocess. Returns (ok_to_activate, note).

    LOAD_ERROR (syntax/import/not-defined) or a timeout block activation.
    A runtime CALL_RAISED with empty args is fine (skills often need real args).
    """
    skill_file = _skill_file(config, name, version)
    harness = HARNESS_TEMPLATE.format(
        kairos=repr(str(KAIROS_DIR)),
        skill_file=repr(str(skill_file)),
        func=f"tool_{name}",
    )
    harness_path = _skills_dir(config) / ".dryrun.py"
    harness_path.write_text(harness, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(harness_path)],
            cwd=str(KAIROS_DIR), capture_output=True, text=True, timeout=25,
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
    if line.startswith("CALL_RAISED") or line.startswith("CALL_BADRESULT"):
        # loaded fine; raised only because it was called with empty args — acceptable
        return True, f"loads OK; {line[:200]} (expected if it needs args)"
    return False, f"dry-run produced no verdict: {out[:200]}"


HARNESS_TEMPLATE = '''\
import sys, os
os.chdir({kairos})
sys.path.insert(0, {kairos})
from config import load_config, Config
from tools import ToolResult
cfg = load_config("config.toml")
code = open({skill_file}, encoding="utf-8").read()
ns = {{"Config": Config, "ToolResult": ToolResult}}
try:
    exec(compile(code, {skill_file}, "exec"), ns)
except Exception as e:
    print("LOAD_ERROR: " + repr(e)); sys.exit(0)
fn = ns.get("{func}")
if fn is None:
    print("LOAD_ERROR: {func} not defined after exec"); sys.exit(0)
try:
    r = fn({{}}, cfg)
    print("CALL_OK" if isinstance(r, ToolResult) else "CALL_BADRESULT: returned " + type(r).__name__)
except SystemExit:
    print("CALL_RAISED: SystemExit")
except Exception as e:
    print("CALL_RAISED: " + repr(e)[:200])
'''


# ---------------------------------------------------------------------------
# Hot-load & invocation tracking
# ---------------------------------------------------------------------------

def _build_runner(config: Config, name: str, code: str):
    """Compile skill source and return a registry-compatible callable.

    The skill runs IN-PROCESS (so it can do real work: network, subprocess, devices).
    Exceptions are caught and converted to a failed ToolResult; usage is recorded.
    """
    ns = {"Config": Config, "ToolResult": ToolResult}
    exec(compile(code, f"<skill:{name}>", "exec"), ns)  # noqa: S102 - trusted local agent code
    fn = ns[f"tool_{name}"]

    def runner(args: dict, cfg: Config) -> ToolResult:
        t = time.monotonic()
        try:
            res = fn(args, cfg)
            if not isinstance(res, ToolResult):
                res = ToolResult(output=str(res), full_output_path=None,
                                 success=True, duration_s=time.monotonic() - t)
        except Exception as e:  # noqa: BLE001 - skills must never crash the loop
            res = ToolResult(output=f"[skill '{name}' raised] {type(e).__name__}: {e}",
                             full_output_path=None, success=False,
                             duration_s=time.monotonic() - t)
        _record_invocation(cfg, name, res.success)
        return res

    runner.__name__ = f"skill_{name}"
    return runner


def _record_invocation(config: Config, name: str, ok: bool) -> None:
    try:
        m = _load_manifest(config)
        ent = m["skills"].get(name)
        if not ent:
            return
        ent["invocations"] = ent.get("invocations", 0) + 1
        if ok:
            ent["successes"] = ent.get("successes", 0) + 1
        inv, suc = ent["invocations"], ent.get("successes", 0)
        if ent.get("status") == "active" and inv >= _TRUST_MIN_USES and (suc / inv) >= _TRUST_MIN_RATE:
            ent["status"] = "trusted"
            logger.info("skill '%s' promoted to trusted (%d/%d)", name, suc, inv)
        _save_manifest(config, m)
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


def load_active_skills(config: Config) -> list[str]:
    """Hot-load every enabled skill at startup. Returns names loaded."""
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
        if ex == name or ent.get("status") != "active":
            continue
        if _skill_subject(ex) & subj:
            out.append(ex)
    return out


def skills_brief(config: Config, max_n: int = 45) -> str:
    """Compact, recency-sorted list of active skill names for per-tick context awareness."""
    m = _load_manifest(config)
    sk = m.get("skills", {})
    names = [n for n, e in sk.items() if e.get("status") == "active"]
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
    # Near-duplicate guard — the #1 source of wasted motion was authoring a 7th MQTT/OctoPrint
    # skill instead of calling the one already built. Block domain-duplicates; redirect to reuse.
    dup = _similar_skills(name, m["skills"])
    if dup:
        return {"success": False, "errors": [
            f"You ALREADY have skill(s) for this domain: {', '.join(sorted(dup)[:6])}. "
            f"CALL one as a tool (e.g. <tool>{sorted(dup)[0]}</tool>), or use edit_skill to improve "
            f"it. Do NOT author a near-duplicate — reuse what you built."]}

    errs = _validate_source(name, code)
    if errs:
        return {"success": False, "errors": errs}

    version = "1.0.0"
    _skill_file(config, name, version).write_text(code, encoding="utf-8")

    ok, note = _dry_run(config, name, version)
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
    return {"success": True, "skill": name, "version": version,
            "status": "active", "note": note,
            "message": f"Skill '{name}' v{version} is live — call it as <tool>{name}</tool>. ({note})"}


def edit_skill(config: Config, name: str, code: str,
               description: Optional[str] = None, args_schema: Optional[dict] = None) -> dict:
    """Replace a skill's implementation with a new version (old versions kept for rollback)."""
    name = (name or "").strip()
    m = _load_manifest(config)
    ent = m["skills"].get(name)
    if not ent:
        return {"success": False, "errors": [f"no skill '{name}' — use create_skill"]}

    errs = _validate_source(name, code)
    if errs:
        return {"success": False, "errors": errs}

    version = _next_version(ent)
    _skill_file(config, name, version).write_text(code, encoding="utf-8")

    ok, note = _dry_run(config, name, version)
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
        skills[name] = {
            "description": ent.get("description", ""),
            "version": ent.get("active_version"),
            "status": ent.get("status"),
            "enabled": ent.get("enabled"),
            "uses": inv, "success_rate": round(suc / inv, 2) if inv else None,
        }
    builtins = sorted(n for n in RESERVED_NAMES if n not in {
        "create_skill", "edit_skill", "list_skills", "rollback_skill"})
    return {"builtin_tools": builtins, "skills": skills,
            "loaded_in_registry": sorted(n for n in m.get("skills", {}) if n in TOOLS)}
