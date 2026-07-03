"""Skill atoms — the reliable, always-in-scope vocabulary authored skills compose (M2.1).

The predecessor's skills brick-walled because authored code reached for `import requests` (not
installed) or called `http_request` as if it were already in scope — 20 of 49 skills were broken by
construction, and `create_skill` succeeded only 26% of the time. The reliable built-in tools ARE the
atoms; the skills just couldn't reach them.

`build_atoms(config)` returns those tools as clean, in-scope callables (unwrapped to plain values, not
ToolResult), plus a stdlib HTTP that NEVER needs `requests`. It is injected into every skill's
namespace — live AND in the author-time dry-run — so a skill calls `http_get(...)` / `recall(...)` /
`sh(...)` directly. Atoms degrade instead of detonating: on an expected failure they return a value or
an {"ok": False, ...} dict, they don't raise — so a composition of atoms fails soft.

This is the foundation of the skill-language (METABOLISM_PLAN.md M2): atoms → compositions → promoted
atoms. Start minimal-sufficient (~13 atoms covering ~95% of observed predecessor behavior), grow by
promotion.

Pillars 3.3 — composition (dark behind `pillars_skill_composition_enabled`). One more atom, `call`,
lets a skill invoke ANOTHER trusted skill — vocabulary becoming a language (the basal-ganglia chunk).
Three guardrails keep a composition bounded and honest:
  * depth cap `COMPOSITION_MAX_DEPTH` — a call chain can nest at most this deep (runtime);
  * ONE shared energy budget threaded down the whole chain (a `_Budget`), not a per-call allowance —
    when it's exhausted the composition aborts (raising `CompositionBudgetError`, which the killable
    runner reports as an aborted composition, never a hang);
  * a STATIC cycle check at authoring time (`check_composition_cycle`) — an A→B→A composition is
    rejected before it can ever run, so cyclic recursion never reaches the runtime at all.
With the flag off, `call` is absent from the atom namespace and nothing here changes.
"""
import ast as _ast
import json as _json
import re as _re
import urllib.error as _urlerr
import urllib.request as _urlreq
from pathlib import Path as _Path

# The atom names — reserved so a skill can't shadow one, and surfaced to the author as the vocabulary.
# `call` (3.3 composition) is intentionally NOT in this tuple: it is injected into the namespace only
# when the composition flag is on. It IS a name a skill must never take (a skill literally named `call`
# would shadow the atom); RESERVED_NAMES in skills.py adds it to the forbidden set separately.
ATOM_NAMES = (
    "http_get", "http_post", "json_parse",        # the #1 need: HTTP + parsing (no `requests`)
    "sh", "read", "write",                          # shell + files
    "recall", "memorize", "note",                   # memory
    "look",                                          # vision
    "net_scan", "tcp_probe", "http_probe",          # network discovery (the working built-in probes)
)

# --- Pillars 3.3: composition constants (each declared + justified; §0 no silent guesses) ------------
COMPOSITION_MAX_DEPTH = 2            # declared: a composed skill may call skills, but the call chain
                                     # cannot nest deeper than this. 2 = "a sentence of one clause" —
                                     # deep enough to chunk (A calls B), shallow enough that a runaway
                                     # tree can't explode before the budget catches it. The keystone
                                     # bound that keeps composition a language, not an interpreter.
COMPOSITION_BUDGET_UNITS = 32.0      # declared: default energy units for ONE whole composition (the
                                     # shared budget). Each skill invocation in the chain spends
                                     # COMPOSITION_CALL_COST; 32 / 1 = up to 32 sub-calls before the
                                     # composition aborts — generous for a real chunk, finite so an
                                     # accidental fan-out starves instead of running unbounded.
COMPOSITION_CALL_COST = 1.0         # declared: energy a single `call(skill, args)` draws from the
                                     # shared budget. One unit per hop makes the budget a plain hop
                                     # counter today; a future phase can price hops by measured cost
                                     # without touching the threading, so it stays a named factor now.
COMPOSITION_CALL_SENTINEL = "__EIDOS_COMPOSITION__"  # marker embedded in a composition-abort error's
                                     # message so the killable runner can tell a depth/budget abort
                                     # apart from an ordinary skill exception when it reports the result.


class CompositionError(RuntimeError):
    """Base for a composition being refused/aborted at RUNTIME (depth or budget). Carries the sentinel
    so the runner can tag it as a composition abort rather than an ordinary skill crash."""

    def __init__(self, msg: str):
        super().__init__(f"{COMPOSITION_CALL_SENTINEL} {msg}")


class CompositionDepthError(CompositionError):
    """Raised when a `call` would nest deeper than COMPOSITION_MAX_DEPTH."""


class CompositionBudgetError(CompositionError):
    """Raised when the shared energy budget is exhausted mid-composition (the abort signal the killable
    runner surfaces as a bounded composition, never a hang)."""


class _Budget:
    """The ONE shared energy allowance for a whole composition, threaded down the call chain by
    reference (every nested `call` spends from the SAME object — a per-composition budget, NOT a
    per-call one). `spend` deducts and raises CompositionBudgetError the instant it would go negative,
    so a runaway composition aborts deterministically instead of running until something else notices."""

    __slots__ = ("remaining", "spent_calls")

    def __init__(self, units: float = COMPOSITION_BUDGET_UNITS):
        self.remaining = float(units)
        self.spent_calls = 0

    def spend(self, cost: float, what: str) -> None:
        if self.remaining < cost:
            raise CompositionBudgetError(
                f"budget exhausted before calling '{what}' "
                f"({self.remaining:.1f} left, {cost:.1f} needed after {self.spent_calls} call(s))")
        self.remaining -= cost
        self.spent_calls += 1


def _http(url, *, method="GET", data=None, json=None, headers=None, timeout=15):
    """Stdlib HTTP — never needs the `requests` package. Returns {ok,status,text,json}; never raises."""
    h = dict(headers or {})
    body = None
    if json is not None:
        body = _json.dumps(json).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    elif data is not None:
        body = data.encode("utf-8") if isinstance(data, str) else data
    try:
        req = _urlreq.Request(url, data=body, headers=h, method=method)
        with _urlreq.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
            out = {"ok": True, "status": getattr(r, "status", 200), "text": text, "json": None}
            try:
                out["json"] = _json.loads(text)
            except Exception:  # noqa: BLE001 - body just isn't JSON
                pass
            return out
    except _urlerr.HTTPError as e:
        return {"ok": False, "status": e.code, "text": str(e), "json": None}
    except Exception as e:  # noqa: BLE001 - DNS/timeout/refused — a soft failure the skill can read
        return {"ok": False, "status": 0, "text": f"{type(e).__name__}: {e}", "json": None}


# --- Pillars 3.3: reading skill source for composition (no `import skills` — that would be a cycle,
# since skills.py imports this module; we read the manifest + versioned file directly). ---------------

def _skills_dir(config) -> _Path:
    return _Path(config.workspace_dir) / "skills"


def _load_skill_manifest(config) -> dict:
    try:
        return _json.loads((_skills_dir(config) / "_index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"skills": {}}


# Statuses of a skill that is live enough to be CALLED by a composition. A composition may only call a
# TRUSTED skill (the automatized/habit tier) — the striatal design: you compose with fluent moves, not
# with a still-deliberate, unproven one. `active` is deliberately excluded here.
_CALLABLE_STATUSES = ("trusted",)


def _skill_source(config, name: str) -> tuple:
    """Return (source, error). Reads the skill's ACTIVE version file. error is '' on success."""
    ent = _load_skill_manifest(config).get("skills", {}).get(name)
    if not ent:
        return "", f"no skill '{name}'"
    if ent.get("status") not in _CALLABLE_STATUSES:
        return "", (f"skill '{name}' is '{ent.get('status')}', not trusted — a composition may only "
                    f"call a TRUSTED skill")
    ver = str(ent.get("active_version") or "1.0.0")
    try:
        return (_skills_dir(config) / f"{name}__{ver}.py").read_text(encoding="utf-8"), ""
    except OSError as e:
        return "", f"cannot read skill '{name}' v{ver}: {e}"


# --- Static call-graph extraction + cycle check (AUTHORING time, never runtime) ----------------------

_CALL_ARG_RE = _re.compile(r"""call\s*\(\s*['"]([a-z][a-z0-9_]{0,40})['"]""")


def static_calls_in_source(code: str) -> set:
    """The set of skill names a source statically `call(...)`s with a STRING-LITERAL first arg. We parse
    the AST and read the literal name off each `call("name", ...)` — a dynamic `call(var, ...)` is
    invisible to a static graph (accepted: the runtime depth+budget caps still bound it, and the cycle
    check is a best-effort authoring guard, not a sandbox). Falls back to a regex if the AST won't parse
    (a syntactically-broken skill is rejected elsewhere anyway)."""
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return set(m.group(1) for m in _CALL_ARG_RE.finditer(code or ""))
    names: set = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) and node.func.id == "call":
            if node.args and isinstance(node.args[0], _ast.Constant) \
                    and isinstance(node.args[0].value, str):
                names.add(node.args[0].value)
    return names


def check_composition_cycle(config, name: str, code: str) -> list:
    """Build the STATIC call graph among skills WITH this candidate skill's source substituted in, and
    return a list of error strings if authoring `name` would introduce a cycle (A→B→A, or a self-call
    A→A). Empty list == acyclic == safe to author. This runs at validation time so a cyclic composition
    is REJECTED before it can ever run — the runtime never has to defend against unbounded recursion,
    only against depth/budget overruns of an acyclic-but-large composition.

    Only trusted skills' calls form the graph (a composition can only call trusted skills), so an
    active/quarantined skill's stale `call` can't manufacture a phantom cycle."""
    m = _load_skill_manifest(config).get("skills", {})
    # edges: skill -> set of skills it (statically) calls. Start from every OTHER trusted skill's src.
    edges: dict = {}
    for other, ent in m.items():
        if other == name or ent.get("status") not in _CALLABLE_STATUSES:
            continue
        src, err = _skill_source(config, other)
        if err:
            continue
        edges[other] = static_calls_in_source(src)
    # Substitute the candidate's own edges (it may not exist yet, or be an edit of an existing one).
    edges[name] = static_calls_in_source(code)

    # DFS for any cycle reachable from `name` (the only node whose edges changed).
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}

    def visit(n: str, stack: list):
        color[n] = GREY
        stack.append(n)
        for nxt in edges.get(n, ()):  # a call to a non-graph (untrusted/absent) skill just dead-ends
            if nxt not in edges:
                continue
            if color.get(nxt) == GREY:
                return stack[stack.index(nxt):] + [nxt]
            if color.get(nxt) == WHITE:
                found = visit(nxt, stack)
                if found:
                    return found
        color[n] = BLACK
        stack.pop()
        return None

    cyc = visit(name, [])
    if cyc:
        return [f"composition cycle rejected: {' → '.join(cyc)} — a skill may not (directly or "
                f"transitively) call back into itself. Break the loop before authoring."]
    return []


def build_atoms(config) -> dict:
    """The atom vocabulary bound to this config — injected into every skill namespace."""
    import tools

    def _out(res):
        return res.output if hasattr(res, "output") else res

    def http_get(url, headers=None, timeout=15):
        return _http(url, method="GET", headers=headers, timeout=timeout)

    def http_post(url, json=None, data=None, headers=None, timeout=15):
        return _http(url, method="POST", json=json, data=data, headers=headers, timeout=timeout)

    def json_parse(text, default=None):
        try:
            return _json.loads(text)
        except Exception:  # noqa: BLE001
            return default

    def sh(cmd, timeout=20):
        return _out(tools.tool_bash({"cmd": cmd, "wait": True, "timeout": timeout}, config))

    def read(path):
        return _out(tools.tool_read_file({"path": path}, config))

    def write(path, content):
        return _out(tools.tool_write_file({"path": path, "content": content}, config))

    def recall(query, k=5):
        return _out(tools.tool_recall({"query": query, "k": k}, config))

    def memorize(fact, tags=None):
        return _out(tools.tool_memorize({"fact": fact, "tags": tags or []}, config))

    def note(text):
        return _out(tools.tool_note_append({"text": text}, config))

    def look(image, question="What is in this image?"):
        return _out(tools.tool_vision({"image": image, "question": question}, config))

    def net_scan(subnet, ports=None):
        a = {"subnet": subnet}
        if ports:
            a["ports"] = ports
        return _out(tools.tool_net_scan(a, config))

    def tcp_probe(host, port):
        return _out(tools.tool_tcp_probe({"ip": host, "port": port}, config))

    def http_probe(url):
        return _out(tools.tool_http_probe({"url": url}, config))

    atoms = {
        "http_get": http_get, "http_post": http_post, "json_parse": json_parse,
        "sh": sh, "read": read, "write": write,
        "recall": recall, "memorize": memorize, "note": note, "look": look,
        "net_scan": net_scan, "tcp_probe": tcp_probe, "http_probe": http_probe,
    }

    # --- Pillars 3.3: the `call` atom (dark behind the composition flag) ---------------------------
    # Injected ONLY when composition is enabled, so with the flag OFF `call` is simply not in scope and a
    # skill that reaches for it NameErrors at the dry-run (existing skills, which never call, are
    # byte-for-byte unaffected). `_make_call` closes over ONE shared budget + the current depth so every
    # nested hop spends the SAME budget and the depth cap is enforced.
    if getattr(config, "pillars_skill_composition_enabled", False):
        atoms["call"] = _make_call(config, atoms, budget=_Budget(), depth=0)
        # Promotion-to-atom (S-4): each operator-approved promoted composition is injected as its own
        # atom callable, so a proven chunk runs as a single vocabulary unit. Each runs with a FRESH shared
        # budget at depth 0 (it's a top-level move now), same as invoking a composed skill directly.
        for _pname, _rec in _load_promoted_atoms(config).items():
            atoms[_pname] = _make_promoted_atom(config, atoms, _pname, _rec.get("source") or "")

    return atoms


def _load_promoted_atoms(config) -> dict:
    """Read the promoted-atom store written by skills.apply_promotion (kept here so build_atoms has no
    dependency on skills.py — skills.py imports THIS module, not the reverse). Empty on any failure."""
    try:
        p = _skills_dir(config) / "_promoted_atoms.json"
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def promoted_atom_names(config) -> tuple:
    """The names in the promoted-atom vocabulary — the atoms build_atoms adds on top of ATOM_NAMES."""
    return tuple(sorted(_load_promoted_atoms(config).keys()))


def _make_promoted_atom(config, base_atoms: dict, atom_name: str, source: str):
    """Wrap a promoted composition's stored source as an atom callable `atom(args=None) -> result`. It
    compiles the source in a namespace carrying the atoms + a fresh-budget `call` at depth 0, runs its
    `tool_<name>`, and returns the unwrapped output — the same execution shape as a composed skill, now
    reachable by name as an atom."""
    import tools

    def promoted(args=None):
        ns = {"Config": type(config), "ToolResult": tools.ToolResult}
        ns.update(base_atoms)
        ns["call"] = _make_call(config, base_atoms, budget=_Budget(), depth=0)
        try:
            exec(compile(source, f"<promoted:{atom_name}>", "exec"), ns)  # noqa: S102 - trusted local code
            fn = ns.get(f"tool_{atom_name}")
            if fn is None:
                return {"ok": False, "error": f"promoted atom '{atom_name}' defines no tool_{atom_name}"}
            res = fn(args or {}, config)
            return res.output if hasattr(res, "output") else res
        except CompositionError:
            raise
        except Exception as e:  # noqa: BLE001 - a promoted atom fails soft, like any atom
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return promoted


def _make_call(config, base_atoms: dict, budget: "_Budget", depth: int):
    """Build the `call(skill_name, args=None)` atom bound to a shared `budget` and the current `depth`.

    Executing a sub-skill: read its (trusted) active source, compile it in a namespace that carries the
    atom vocabulary PLUS a nested `call` (depth+1, SAME budget object), invoke `tool_<name>(args, cfg)`,
    and return its ToolResult's `.output` (unwrapped, like every other atom). Guardrails, in order:
      1. depth: a call at COMPOSITION_MAX_DEPTH would create a child one deeper — refuse (raise).
      2. budget: spend COMPOSITION_CALL_COST from the shared budget BEFORE running; exhaustion raises.
      3. the sub-skill must be TRUSTED (checked in `_skill_source`).
    A CompositionError (depth/budget) PROPAGATES so the killable runner sees a bounded abort; an ordinary
    exception inside the sub-skill is caught and returned soft (a composition of atoms fails soft)."""
    import tools

    def call(skill_name, args=None):
        # 1. Depth cap. `depth` is how deep THIS namespace already is (0 = the top composed skill). A hop
        #    from here produces a child at depth+1; refuse once that would exceed the cap.
        if depth + 1 > COMPOSITION_MAX_DEPTH:
            raise CompositionDepthError(
                f"call('{skill_name}') would nest to depth {depth + 1} > cap {COMPOSITION_MAX_DEPTH}")
        # 2. Shared budget (spent before the work, so a runaway can't outrun its own accounting).
        budget.spend(COMPOSITION_CALL_COST, str(skill_name))
        # 3. Load the trusted sub-skill's source.
        src, err = _skill_source(config, str(skill_name))
        if err:
            return {"ok": False, "error": err}
        ns = {"Config": type(config), "ToolResult": tools.ToolResult}
        ns.update(base_atoms)
        # The nested call: one deeper, SAME budget — this is how the single budget threads down the chain.
        ns["call"] = _make_call(config, base_atoms, budget=budget, depth=depth + 1)
        try:
            exec(compile(src, f"<composed:{skill_name}>", "exec"), ns)  # noqa: S102 - trusted local code
            fn = ns.get(f"tool_{skill_name}")
            if fn is None:
                return {"ok": False, "error": f"skill '{skill_name}' defines no tool_{skill_name}"}
            res = fn(args or {}, config)
            return res.output if hasattr(res, "output") else res
        except CompositionError:
            raise                       # depth/budget aborts propagate to the runner (bounded, not soft)
        except Exception as e:          # noqa: BLE001 - a sub-skill's OWN failure fails SOFT
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return call


def atoms_reference(config=None) -> str:
    """A compact stdlib-style reference of the atom vocabulary, for the skill-author's context. When
    composition is enabled, the `call` atom is documented too (otherwise it isn't in scope)."""
    ref = (
        "Atoms available in scope when you author a skill (call them directly — do NOT `import requests`):\n"
        "- http_get(url, headers=None, timeout=15) -> {ok,status,text,json}\n"
        "- http_post(url, json=None, data=None, headers=None, timeout=15) -> {ok,status,text,json}\n"
        "- json_parse(text, default=None) -> obj\n"
        "- sh(cmd, timeout=20) -> str        # run a shell command, wait for output\n"
        "- read(path) -> str  /  write(path, content) -> str\n"
        "- recall(query, k=5) -> str  /  memorize(fact, tags=None) -> str  /  note(text) -> str\n"
        "- look(image, question) -> str      # vision\n"
        "- net_scan(subnet, ports=None) -> str  /  tcp_probe(host, port) -> str  /  http_probe(url) -> str\n"
    )
    if config is not None and getattr(config, "pillars_skill_composition_enabled", False):
        ref += (
            f"- call(skill_name, args=None) -> result   # invoke ANOTHER trusted skill (composition).\n"
            f"    A composition shares ONE energy budget and nests at most {COMPOSITION_MAX_DEPTH} deep; "
            f"a cycle (A calls B calls A) is rejected at authoring.\n")
    return ref
