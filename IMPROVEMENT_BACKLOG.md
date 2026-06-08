# eiDOS — Systemic Improvement Backlog

Running log of candidate improvements to how eiDOS performs *within its harness/system*.
Compiled by Claude on hourly check-ins. **These are hypotheses to review with Boss — NOT yet acted on.**

Each entry: `[severity] title — what / why / how to verify / rough cost`.
Severity: 🔴 high-leverage · 🟡 worth doing · 🟢 nice-to-have · 🔵 needs-observation (unverified hunch)

Legend for status: `CANDIDATE` (logged, unverified) · `OBSERVED` (saw it in a live run) · `REVIEWED` (Boss decided).

---

## Check-in 2026-06-08 ~13:00 — initial brainstorm (pre-observation)

These are first-principles candidates from knowing the architecture. Most are **🔵 unverified** —
the hourly observation passes will confirm/kill them against the live run.

### Context & token efficiency
- 🔵🟡 **Static-context caching** — `context.py` reassembles the briefing every tick. If large static
  blocks (persona, platform plumbing, skill list) are re-tokenized each tick, that's wasted prefill.
  *Verify:* measure tokens/tick + how much is invariant. *How:* hoist invariants into the llama.cpp
  prompt-cache prefix (stable ordering so KV-cache hits). *Cost:* M.
- 🔵🟡 **Adaptive tick cadence** — fixed tick interval wastes cycles when idle and adds latency when
  active. *Verify:* look at tick log for back-to-back no-op ticks. *How:* back off interval when N
  ticks produce no action/observation; snap to fast when chat arrives or a job returns. *Cost:* S.

### Memory & retrieval
- 🔵🔴 **Semantic recall (embeddings) alongside BM25** — BM25 misses synonyms ("printer" vs
  "octoprint", "plug" vs "tuya"). A small local embed model or llama.cpp embedding endpoint would
  raise recall hit-rate, which directly reduces rediscovery loops. *Verify:* sample recalls that
  returned nothing useful. *Cost:* M.
- 🔵🟡 **Observation salience gating** — if every tick writes observations, the store fills with
  low-value entries that dilute recall and bloat compaction. *Verify:* count obs/tick and eyeball
  signal ratio. *How:* a cheap salience score (did anything change? new fact? error?) before persist.
  *Cost:* S.
- 🔵🟡 **Recency-weighted recall** — pure BM25 ignores time; a device re-scanned today should outrank
  a stale note. *How:* blend BM25 score with exponential-decay recency. *Cost:* S.

### Skills lifecycle
- 🔵🔴 **Skill scoring + auto-retire** — skills accrete; 0-use and repeatedly-failing skills are noise
  in `skills_brief` and the dedup space. *How:* track invocations + success rate per skill; surface a
  "stale skills" list; auto-archive after K ticks of 0 use. *Verify:* re-check the 56-skills-0-uses
  pattern from the prior run. *Cost:* M.
- 🔵🟡 **Skill self-test on create** — `create_skill` could dry-run the new tool once and reject if it
  throws, instead of letting a broken skill sit until first real use. *Cost:* S.

### Loop robustness
- 🔵🔴 **Rumination/loop breaker (generalized)** — detect when the last N ticks repeat the same
  intent/command with the same failure and force a strategy change or escalate to Boss. Partial guard
  exists; make it a first-class harness feature with a counter surfaced in context. *Cost:* M.
- 🔵🟡 **Faster fast-path result delivery** — async results land "a few ticks later." For sub-second
  commands, deliver in the very next tick (poll finished jobs at tick head, already partly done via
  `collect_finished_jobs`) so it doesn't re-plan around a result that's actually ready. *Verify:*
  measure dispatch→delivery tick gap. *Cost:* S.

### Model / decoding
- 🔵🔴 **Constrained/grammar tool-call decoding** — malformed tool JSON wastes whole ticks. A GBNF
  grammar (llama.cpp supports it) forcing valid tool-call structure would cut parse-failure ticks to
  zero. *Verify:* grep thoughts for tool-parse failures. *Cost:* M, high leverage.
- 🔵🟡 **Sampler tuning for agentic reliability** — cross-reference the eval-suite findings (think-OFF,
  temp/sampler) and pin house-ai's runtime sampler to the agentic-best config. *Cost:* S.
- 🔵🟢 **Collapse double LLM call/tick** — if briefing-model + main-model are two calls per tick,
  evaluate merging or making the briefing call cheaper/cached. *Verify:* confirm call count per tick.
  *Cost:* M.

### Self-management
- 🔵🟡 **Self-guide compaction** — self_guide.md can grow stale/contradictory; periodic distill pass
  keeps it sharp and cheap to inject. *Cost:* S.
- 🔵🟡 **Working-objective tracker** — a single "current objective + last progress" line maintained
  across ticks would keep multi-tick tasks (LAN map → first device skill) from losing the thread.
  *Cost:* S.

### Measurement (meta — do this first; it de-risks everything above)
- 🔵🔴 **eidos self-telemetry** — without per-tick metrics (tokens, action taken y/n, tool success,
  recall hit, parse failures) every item above is a guess. Add a lightweight `tick_metrics.jsonl` and
  a tiny dashboard panel. This is the highest-leverage item: it turns hunches into measured wins.
  *Cost:* M. **Recommend doing this one first.**

---
<!-- New check-ins append below this line -->

## Check-in 2026-06-08 ~13:05 — FIRST LIVE OBSERVATION (early, by request)

Observed the fresh Lv.0 run (~tick 166, did one dream compaction already). The live run **confirms
several hunches hard** and surfaces new concrete ones. Dream journal works (✅ "Plan 530→578, 2
knowledge entries, cleared 13 obs"). Async pipeline works (✅ dispatch tick 165 → `[↩ job auto_23224]`
delivered tick 166; 10s auto-background firing correctly). But:

### 🔴 OBSERVED — Rumination on PowerShell string interpolation (the #1 time-sink right now)
Roughly **TEN near-identical port-scan retries** in ~3 min, all fighting the same `"$ip:$p"` colon
problem: `port_probe → _fixed → v3 → v4 → v5 → v6 → raw_iot_scan → refined_iot_scan → fixed_iot_scan
→ broad_web_scan`. It cycled `$ip:$p` → `${ip}:${p}` → `$($ip):$($p)` → `New-Object TcpClient` and
back. **The new "write PowerShell directly" nugget did NOT stop it.** Two compounding root causes:
  1. It keeps falling back to wrapping `powershell -Command "...Write-Output "${ip}:${p}"..."` — the
     **nested unescaped double-quotes** inside the outer `-Command "..."` are what actually break the
     parse, not the colon. The `_route_windows_command` unwrapper transforms the *outer* call but the
     model's own nested-quote payload is still malformed.
  2. Inside those wrapped calls it writes `$ip = '192.168.86.$_'` — **single quotes, so `$_` never
     expands.** Every one of those scans probed the literal host `192.168.86.$_` and failed. A syntax
     fix it can't reason its way out of by retrying.
  → *Candidates (review):* (a) **generalized loop-breaker** — after N≈3 retries of the same intent
  with the same failure class, force a strategy change / surface to Boss [promote earlier 🔴 item];
  (b) a **bash pre-flight linter** that rejects/auto-rewrites `powershell -Command "…"` wrapping and
  flags single-quoted `$_`/`$var` interpolation BEFORE dispatch, returning a one-line correction
  instead of a parse error; (c) seed a *known-good* port-scan one-liner as a nugget so it copies
  rather than re-derives.

### 🔴 OBSERVED — Skill created but never reused (skill-reuse gap confirmed)
It authored `port_probe__1.0.0.py` as a skill — then **hand-rolled 10+ raw port scans in bash anyway**
instead of calling it. Exactly the "makes skills, doesn't use them" pattern. *Candidate:* after
`create_skill`, inject a strong next-tick nudge ("you now HAVE `port_probe` — call it, don't re-derive")
and surface intent-matched skills in `## Right now`. Pairs with the earlier 🔴 skill-scoring item.

### 🔴 OBSERVED — Malformed tool-call / arg-bleed + Unix syntax on Windows
Job `j21660`'s stored cmd is corrupted: `for i in {40..50}; do ping -n 1 ... done", "name":
"ping_scan_range", "intent": ...` — the JSON tool args **leaked into the `cmd` string**, and it emitted
**Unix bash** `for i in {…}; do … done` on Windows. Strong evidence for **GBNF grammar-constrained
tool-call decoding** (promote earlier 🔴) + a "you are NOT in bash, no `for/do/done`" reminder.

### 🟡 OBSERVED — Slow /24 sweeps repeatedly time out
`1..254 | Test-Connection -Count 1` is sequential (~minutes) and hit the 180s async ceiling **3+ times**
(`lan_scan_v2`, `ping_sweep_lan` ×2 timed_out), each time re-launched fresh. The arp-first nugget
exists but the model defaults to slow ping sweeps. *Candidate:* seed a **fast parallel-scan skill**
(RunspacePool / `-AsJob` batches) or push `arp -a` harder as the *first* discovery step (instant), and
have the loop-breaker catch "same slow sweep timed out twice → switch method."

### 🟢 OBSERVED — jobs.json hygiene
25+ job records, nearly all `notified:true, completed` but never pruned (size-cap exists but doesn't
prune *delivered* jobs). Also **duplicate job names** (`ping_sweep_lan` ×2, `port_probe`-family).
*Candidates:* prune notified+completed after delivery; auto-suffix colliding names.

### 🔵 Note — its own IP is 192.168.86.34; it also sees Docker bridge 172.17.0.1 + APIPA 169.254.x
Minor: the non-routable interfaces could mislead subnet reasoning. Low priority.

**Takeaway for review:** the top three 🔴 (loop-breaker, skill-reuse nudge, grammar-constrained tool
calls) would each independently eliminate a large fraction of the wasted ticks seen in this 3-minute
window. The pre-flight bash linter is a cheap high-impact addition specific to the PowerShell flailing.

### ✅ ADDRESSED this session (before re-wipe) — shipped, unit-tested, reseeded
Boss said "address the current issues, wipe, and retry." Implemented + AST/unit-verified:
1. **Pre-flight command linter** (`tools._lint_windows_command`, blocks before dispatch) — catches the
   3 observed killers: Unix `for…do…done`/`fi`, `powershell -Command "…nested quotes…"`, and
   single-quoted `'…$_…'` interpolation. Returns a one-line fix → 1 corrected tick instead of a doomed
   8–180s job. Tested: 3/3 bad caught, 5/5 good pass (incl. literal `'$5.00'` not flagged).
2. **Fuzzy rumination detector** (`context._norm_cmd` + frequency-based loop warning) — normalizes bash
   commands (collapses digits/ports/versions/quoting) so v3→v4→v5 share ONE signature; warns when any
   action recurs ≥4× even interleaved. Tested: 3 variants → 1 signature.
3. **Skill-reuse nudge** — `create_skill` success now returns a strong "you HAVE this tool, CALL it,
   don't re-derive in bash" directive with the exact call form.
4. **jobs.json pruning** (`_prune_jobs`) — caps delivered jobs to most-recent 15; keeps all live.
5. **Seed nuggets strengthened** — arp-FIRST (no slow `/24` ping sweeps), and PowerShell single-vs-double
   quote + range-pipeline (no bash loops) gotchas, so it writes commands right the first time.

Still OPEN (logged for end-of-day, NOT yet built): grammar-constrained tool-call decoding (GBNF),
skill scoring/auto-retire, semantic recall, self-telemetry, fast parallel-scan skill.

### 🔵 NEW anomaly observed — TWO `eidos.py` processes running at once
PIDs 7260 (control-reported) + 15956 (unaccounted) both alive. Startup `reap_jobs` clears bg *jobs*
but not a stray eidos *process*. Possible double-spawn (watchdog respawn without reaping the prior
eidos) → two tick loops racing on the same workspace = doubled LLM load + interleaved writes. *Candidate:*
on spawn, watchdog should kill any pre-existing eidos.py before starting a new one (single-instance lock).
*Priority:* 🟡 (correctness — two loops corrupting shared state is worse than wasted ticks).
