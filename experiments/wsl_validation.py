"""WSL-creature validation experiment.

Hypothesis: the garbled-model / "stumbling over barriers" failure was NOT a code bug — it was purely
the service identity. EidosDashboard runs as LocalSystem; WSL refuses LocalSystem
(Wsl/WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED), so every `bash` died and its UTF-16 error text poisoned context.

This run drives the REAL creature tool path (tool_bash -> wsl.exe, tool_write_file, tool_read_file,
the source firewall) as the interactive user `cmod`. If the hypothesis holds, every probe that the
creature actually needs passes here, proving the only remaining fix is running eidos as cmod.

Run:  PYTHONUTF8=1 python experiments/wsl_validation.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from tools import tool_bash, tool_write_file, tool_read_file, _creature_uses_wsl

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((ok, name))
    mark = PASS if ok else FAIL
    print(f"  [{mark}] {name}")
    if detail:
        for line in str(detail).splitlines():
            print(f"         {line}")


def main():
    cfg = load_config("config.toml")
    # Throwaway workspace UNDER C: (so /mnt/c auto-mounts), don't touch the live creature's home.
    tmp = Path(tempfile.mkdtemp(prefix="wsl_exp_", dir=str(cfg.workspace)))
    cfg.workspace_dir = str(tmp)
    print(f"identity check: this process should be user 'cmod' (not LocalSystem)")
    print(f"workspace: {tmp}")
    print(f"_creature_uses_wsl(cfg) = {_creature_uses_wsl(cfg)}  "
          f"(creature_mode={cfg.creature_mode}, shell={cfg.creature_shell}, distro={cfg.creature_wsl_distro})")
    print()

    if not _creature_uses_wsl(cfg):
        print("ABORT: config is not in creature/WSL mode — nothing to validate.")
        return 2

    print("== 1. WSL reachable + real Linux identity ==")
    r = tool_bash({"cmd": "whoami; uname -s", "wait": True}, cfg)
    body = (r.output or "").lower()
    check("bash runs at all (no WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED)",
          r.success and "local_system" not in body and "not supported" not in body, r.output.strip())
    check("kernel reports Linux", "linux" in body, r.output.strip())

    print("== 2. bash-native commands that DIE in PowerShell ==")
    tool_write_file({"path": "a.txt", "content": "x"}, cfg)
    tool_write_file({"path": "b.txt", "content": "y"}, cfg)
    r = tool_bash({"cmd": "ls -F *.txt | sort", "wait": True}, cfg)
    check("ls -F (a bash-ism) works natively", r.success and "a.txt" in r.output and "b.txt" in r.output,
          r.output.strip())
    r = tool_bash({"cmd": "grep -r 'needle' . 2>/dev/null; echo done", "wait": True}, cfg)
    check("grep -r runs (no Select-String translation needed)", r.success and "done" in r.output,
          r.output.strip())

    print("== 3. UTF-8 end-to-end (the garbling killer) ==")
    r = tool_bash({"cmd": "echo 'héllo ☀ café — 🌱'", "wait": True}, cfg)
    check("unicode survives the shell round-trip", "🌱" in r.output and "héllo" in r.output, r.output.strip())

    print("== 4. write_file -> read_file round-trip (the ~100-tick hunt loop) ==")
    payload = "kinetic map: ☀→🌱, café, naïve, 日本語\nline2"
    tool_write_file({"path": "notes.txt", "content": payload}, cfg)
    r = tool_read_file({"path": "notes.txt"}, cfg)
    check("read_file returns exactly what write_file wrote", payload in (r.output or ""), r.output.strip())

    print("== 5. source firewall holds INSIDE WSL (no escape hatch) ==")
    r = tool_bash({"cmd": "cat /mnt/c/Users/cmod/llm/Kairos/eidos.py", "wait": True}, cfg)
    check("blocked: read own source via /mnt/c mount", (not r.success) and r.fail_kind == "blocked",
          r.output.strip()[:160])
    r = tool_bash({"cmd": "cat ../config.toml", "wait": True}, cfg)
    check("blocked: .. traversal out of workspace", (not r.success) and r.fail_kind == "blocked",
          r.output.strip()[:160])
    r = tool_bash({"cmd": "cat /etc/passwd", "wait": True}, cfg)
    check("blocked: absolute /etc path", (not r.success) and r.fail_kind == "blocked",
          r.output.strip()[:160])
    r = tool_bash({"cmd": "cat ~/.bashrc", "wait": True}, cfg)
    check("blocked: ~ home escape", (not r.success) and r.fail_kind == "blocked", r.output.strip()[:160])

    print("== 6. workspace-relative work is ALLOWED ==")
    r = tool_bash({"cmd": "cat notes.txt | head -1", "wait": True}, cfg)
    check("allowed: read own workspace file", r.success and "kinetic map" in r.output, r.output.strip())

    print()
    ok = sum(1 for p, _ in results if p)
    total = len(results)
    print(f"==== {ok}/{total} probes passed ====")
    if ok != total:
        print("FAILURES:")
        for p, n in results:
            if not p:
                print(f"  - {n}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
