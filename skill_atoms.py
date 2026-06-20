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
"""
import json as _json
import urllib.error as _urlerr
import urllib.request as _urlreq

# The atom names — reserved so a skill can't shadow one, and surfaced to the author as the vocabulary.
ATOM_NAMES = (
    "http_get", "http_post", "json_parse",        # the #1 need: HTTP + parsing (no `requests`)
    "sh", "read", "write",                          # shell + files
    "recall", "memorize", "note",                   # memory
    "look",                                          # vision
    "net_scan", "tcp_probe", "http_probe",          # network discovery (the working built-in probes)
)


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

    return {
        "http_get": http_get, "http_post": http_post, "json_parse": json_parse,
        "sh": sh, "read": read, "write": write,
        "recall": recall, "memorize": memorize, "note": note, "look": look,
        "net_scan": net_scan, "tcp_probe": tcp_probe, "http_probe": http_probe,
    }


def atoms_reference() -> str:
    """A compact stdlib-style reference of the atom vocabulary, for the skill-author's context."""
    return (
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
