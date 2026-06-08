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

### ✓ RESOLVED (was flagged as anomaly) — "two `eidos.py` processes" is benign
Saw two `eidos.py` PIDs per run and initially suspected a watchdog double-spawn. Investigated:
`_spawn_eidos` issues exactly ONE `Popen([sys.executable, eidos.py, ...])`, yet two processes appear
with a **parent(stub)→child(real) relationship and identical argv**, created the same instant. That's
the venv `python.exe` **launcher-trampoline**: `.venv\Scripts\python.exe` is a stub that re-execs the
real interpreter as its child. Only ONE tick loop runs (observations are cleanly sequential, single
stream); the recorded pid is the stub and a tree-kill takes both down. **Not a bug — no action.**
(The `28848` seen during reset was the same stub being tree-killed; the watchdog respawn path is fine.)

---

## Check-in 2026-06-08 ~14:15 (:07 cadence, run-on) — post-fix live observation, tick ~98 / Lv.2

Observed the fresh run after the fixes. **The targeted fixes are confirmed working in the wild:**
- ✅ **No PowerShell spiral.** Commands are clean (`Test-NetConnection -ComputerName … -Port …`), no
  `powershell -Command` wrapping, no `$ip:$p` flailing. arp-first ran at tick 3. The linter has not
  needed to fire — the model is writing correct PS the first time (nuggets + linter deterrent working).
- ✅ **jobs.json holding at 15** (was 25+ and unbounded) — `_prune_jobs` working.
- ✅ Methodical real work: discovered ~25 LAN hosts, probing 80/443/8883(MQTT) on candidates,
  reasoning about IoT vs PCs. This is the right *kind* of work.

### 🔴 NEW OBSERVED — "thought (no action)" rumination (analysis-paralysis)
Console shows ticks 93,94,96,97,98 all `thought (no action)` — ~5 of the last 6 ticks were pure
narration ("these are probably IoT devices", "high concentration of dedicated IoT hardware", "I should
prioritize identifying the manufacturer") with **no concrete action taken**. It discovered ~25 devices
but is now musing *about* them instead of probing/identifying/memorizing/skill-building. **Critical
gap:** the loop-breaker I just added explicitly SKIPS `thought` in `_skip`, so a run of consecutive
no-action thoughts is INVISIBLE to it. *Candidate (🔴, cheap):* detect K consecutive `thought
(no-action)` ticks and inject "you've thought enough — take a CONCRETE action now (probe, memorize a
fact, build a skill) or you're burning cycles"; or enforce an action at least every K ticks. This is
now the #1 time-sink (different failure mode than the port-scan spiral, but same net effect: stalled).

### 🟡 OBSERVED — memorizing intentions, not facts; discoveries not captured
At tick 91 it memorized `planning_to_categorize_discovered_devices…` — that's an *intention*, not a
durable fact. Meanwhile the actual valuable data (the ~25 IP/MAC device inventory from arp) was NOT
memorized, and 0 skills authored by tick 98. *Candidate:* nudge "memorize the FACTS you discovered
(the device list), not your plans" + "you found N devices — capture them / act, don't narrate."

### 🟢 OBSERVED — minor: context logs `est_tokens=1`
`eidos.context INFO tick=N … est_tokens=1` is clearly a broken estimate (real ~5k tokens logged
elsewhere on the same tick). Cosmetic logging bug; fix when convenient.

**Takeaway:** the syntax/spiral class of problems is fixed; the live bottleneck has shifted UP a level
to *decision* quality — thinking instead of acting, and capturing intentions instead of facts. The
consecutive-no-action-thought detector is the natural next 🔴 (and complements the loop-breaker, which
deliberately ignores thoughts). NOT acting on it now — logging only per Boss's instruction.

---

## Check-in 2026-06-08 — ROOT-CAUSE: stuck ~hours on a 1-2h LAN task (tick 969, Lv.6, paused by Boss)
Boss flagged the AI has been stuck for HOURS on the trivial task of characterizing the LAN. Forensics:

- 🔴 **No definition-of-done → never converges.** "Characterize the LAN" has no completion test, so at
  Lv.6 "curious" it re-probes/re-categorizes forever. *Fix:* give the immediate task a crisp DONE
  criterion + `goal_complete`/report-to-Boss trigger ("memorize one inventory row per active host, then
  tell Boss and stop").
- 🔴 **Over-engineering: builds infra instead of doing the task.** Authored 8 skills (scan/probe/process/
  register pipeline) with cross-imports → ImportError → spent HUNDREDS of ticks debugging its own Python
  plumbing (plan.md item 2 = "Address the ImportError for skill imports"). For a ONE-TIME mapping it
  should just run `arp -a` + a few `Test-NetConnection` inline and memorize results. *Fix:* anti-over-
  engineering directive — "don't build scan/probe skills for one-off discovery; only build a skill for a
  REPEATED control action (toggle a plug)." Plus the dedup guard isn't catching `scan_*`/`probe_*` dupes.
- 🔴 **Linter-block rumination (NEW failure surfaced by the linter).** t969-972 are the SAME single-quoted
  command blocked 4× in a row — linter correctly stops the doomed job, but the model just resubmits it
  identically. The fuzzy loop-breaker fired but is ADVISORY ONLY and this model ignores it. *Fix:* give
  the loop-breaker TEETH — after K identical blocks/failures, either (a) auto-correct the mechanical case
  (single→double quotes) and run it, (b) hand back the exact corrected command, or (c) auto-pause + ping
  Boss ("I'm stuck on X"). For an autonomous buddy, escalating to Boss after N stuck ticks is right.
- 🔴 **Knowledge bloat: 265 entries to map one small LAN.** Dream compaction re-extracts ~2 entries every
  cycle and never dedups → recall returns noise, and it never consolidated a clean device inventory
  (0 entries marked source_tick>0; it captured intentions, not the arp facts). *Fix:* dedup on dream
  extraction (skip near-identical/seed-restating entries) + cap; consolidate device facts into one row
  per device.
- The earlier syntax/spiral fixes DID work (clean commands mid-run, jobs.json capped at 15). The failure
  moved UP to judgment/convergence — which is the harder, higher-value layer.

---

## GHOST-IN-THE-MACHINE session — Claude operating as the agent's POV (Boss-directed)
Boss: "build a logically sound product, don't patch leaks and hope the model follows… I have a feeling
the system is not intuitive to the llm." Reproduced the EXACT tick-973 prompt the model sees and read it
as the agent. Findings, in order of impact:

- 🔴🔴 **SMOKING GUN — my own linter false-positive caused the multi-hour stall.** `_RE_SQUOTE_INTERP`
  (`'[^']*(?:\$_|\$\{)[^']*'`) matches ACROSS quote boundaries. The model wrote a VALID arp-parse command
  (`… Select-String 'dynamic' | ForEach-Object { $_.ToString().Split(' ') } …`); the regex spanned from the
  closing `'` of `'dynamic'` across `$_` to the opening `'` of `' '` and falsely blocked it as "single
  quotes don't expand." Verified live. The model's reasoning was SOUND ([t]: "the regex was over-engineered,
  I should just use a simple split") — it diagnosed correctly every time, but the environment LIED to it and
  rejected every valid variant. It wasn't grasping at straws; it was gaslit by a buggy guardrail. **This is
  exactly the "patch that became a worse leak" Boss warned about.** Fix: make the linter quote-aware (tokenize
  single-quoted spans and only flag `$_`/`${` that are genuinely INSIDE one), or drop the regex for a real
  check. Better yet: a blocked command should AUTO-CORRECT or hand back a runnable fix, never just "NO."
- 🔴 **Subgoals/plan drifted into platform-CONTRADICTING nonsense — and they're the MOST salient line.** The
  top-of-context "Right now you are working on:" = *"Establish a persistent voice and chat listener"* and a
  subgoal = *"Create a persistent memory database"* — the EXACT things the system prompt says NEVER to build
  (chat handled; `memorize` IS the DB). `plan_goal` auto-generated subgoals that fight the platform. The real
  immediate focus (map LAN) is buried far below. The single most prominent instruction misdirects every tick.
- 🔴 **The self-guide actively instructs over-engineering.** "## How to work: when waiting on a job, don't
  ruminate — proactively build the infrastructure the NEXT phase needs (memory profiles, skill schemas,
  device registry)." That self-authored rule directly produced the 8-skill scan/registry pipeline. It
  CONTRADICTS check_system's "never build agent infrastructure." Two of its own directives point opposite ways.
- 🟡 **Recall noise in-context.** "What you already know" shows two near-identical powershell_syntax
  procedures; the same PS gotchas appear 3× (system rules + recalled FACT + recalled PROCEDURE) — drowning in
  redundant advice while its actual command is wrongly blocked.
- ✅ **The tick prompt + loop-detector are actually GOOD.** Loop detection fired ("gone 5 ticks without real
  progress… try a DIFFERENT approach"); the prompt says "take ONE concrete step." But they're powerless when
  the blocker is a buggy guardrail, not the model's choice — it CANNOT comply because every valid command is
  rejected. Don't "fix" these; fix what they're pointing at.

**Reframe:** the model isn't dumb and the prompt isn't badly written. The system gives it (a) FALSE feedback
(linter FP) and (b) a contradictory, drifted set of top-priority directives (auto-subgoals vs platform rules;
self-guide vs check_system). It "follows the next logical step regardless" because the environment keeps
lying about what just happened and what matters now. The fix is COHERENCE (one source of truth for the
current objective; guardrails that never lie; self-guide consistent with the platform), not more rules.

### Ghost replays — 4 states (fresh boot / Boss-message / async-landed / idle). "Do I feel blind?" YES.
Rendered the real tick context for four states and operated as the agent. The context is RICH but it's the
WRONG richness — over-supplies static identity/rules, under-supplies live state. Specific blindness:
- 🔴 **Blind to my own accumulated knowledge (memory is write-only in practice).** In ALL four states, the
  "What you already know (recalled)" panel showed generic BOOTSTRAP nuggets (identity, PowerShell syntax) —
  NEVER the device inventory I supposedly built over 6h. To see what I learned I must `recall(query)` and hope
  lossy BM25 surfaces it from 265 noisy entries. So I re-scan because I can't SEE that I already scanned. This
  is likely THE root cause of going in circles: I can store facts but they don't reliably come back into view →
  the loop has amnesia. **Need: a persistent, structured world-state/knowledge panel always in context** (e.g.
  a compact device/registry view + "key facts" pinned), not pull-only recall.
- 🔴 **Blind to the ONE thing to do now — 4 sources fight.** "Right now you are working on:" (drifted subgoal:
  chat listener) vs "## Plan" (arp steps) vs mission "Immediate focus" (map LAN) vs actual history (port
  probing). The MOST prominent status line tells it to build the chat listener the platform forbids. Need ONE
  coherent current-objective line (+ next step + done-criterion) it can trust; kill the contradictors.
- 🔴 **Blind to salience/novelty.** Boss's message lands at the BOTTOM of a 6 KB blob and the action-directing
  tick prompt never mentions a message arrived → easy to talk past Boss. Async results arrive at the same flat
  priority as everything else. Nothing says "THIS changed since last tick — handle THIS." Need a "what's new"
  marker and the incoming Boss message surfaced AT the decision point (in/above the tick prompt).
- 🟡 **Presence can contradict history** (presence "⟳ Still running" while the result already appears in the
  thread) — partly a sandbox artifact, but a real desync risk between jobs.json and delivered async_results.
- 🟡 **Stale goal claim:** goal.md tells even a fresh Lv.0 newborn "you have found the 192.168.86.x network"
  (baked-in prior progress). The immediate-focus line should be state-derived, not hardcoded.
- ✅ NOT blind to: identity, tools, rules, encouragement — those are OVER-supplied (system prompt 8.6 KB +
  redundant recalled nuggets repeat "memorize is your DB / use PowerShell" 2-3×). Fix is REBALANCING, not more.

**Design implication:** the context layer needs (1) a persistent world-state panel (what I know + what's done/
left), (2) one trustworthy current-objective, (3) a salience/"what's new" channel, and (4) less static
boilerplate. That's a coherent context model, not another rule.
