# eiDOS — Next Release Plan (2026-07-18)

The full scope from the deep analysis + SOTA review ([ANALYSIS_2026-07-18.md](ANALYSIS_2026-07-18.md)):
**10 pillars**, **47 findings** (todos), plus the SOTA "wheels" folded in as feature-todos. Everything is
implemented on branch `analysis/fixes-2026-07-18`, one small tested commit per item (rollback =
`git checkout main`). `[x]` = landed + tested; `[ ]` = pending. Each todo notes its severity and the
finding id / file.

Judged against the pillar: **autonomy — persist toward a goal without derailing** — and the bar: **efficacy**.

---

## Pillar 1 — Autonomy & anti-derailment
- [x] **H1** exposure-cap weak-progress immortality → strong-progress stall clock `objectives.py` (HIGH)
- [x] **H2** exposure-cap `objective_block` bypass → death check at the `_thaw` choke point `objectives.py` (HIGH)
- [x] reward "varying-output freebie" → `normalize_result` before the novelty hash `reward.py`/`eidos.py` (MED)
- [x] loop-detector arg-vary-defeatable → normalize non-bash tool sigs (arg-varied loops now caught) `eidos.py` (LOW)
- [x] level_gates stale DARK docstring → corrected `level_gates.py` (LOW)
- [ ] backlog-exhaustion soft-stall → mint a bounded exploratory objective on full-park (behavior-changing; **FEATURE**) `objectives.py:517` (LOW)
- [ ] **SOTA#1** per-domain learning-progress → curiosity cutover (critical anti-farming already handled live; **FEATURE**, Pillar 6) (feature)
- [ ] **SOTA#3** ReasoningBank-style strategy memory (distill each quest → retrievable guardrail) — **FEATURE EPIC** (feature)
- [ ] **SOTA#5** durable externalized per-objective PLAN artifact injected every tick — **FEATURE EPIC** (feature)
- [ ] **SOTA#7** AgentDebug-style error-attribution post-mortem on park/loop → strategy memory — **FEATURE EPIC** (feature)

## Pillar 2 — Context & recall efficacy
- [x] BM25 cache invalidates on content change, not just length `knowledge.py` (LOW)
- [ ] **H3** post-boot facts never recalled by relevance → route memorize/dream writes through `manager.encode` (or periodic idempotent re-import) `eidos.py:755` (HIGH)
- [ ] est_tokens telemetry is digit-length nonsense → real token estimate `context.py:1380` (LOW)
- [ ] compaction gate byte-inflation → count content tokens, not JSONL bytes `compaction.py:47` (LOW)
- [ ] stable-head cache defeated every tick → sign on semantic inputs, not persona/creature mtime `context.py:1102` (LOW)
- [ ] **SOTA#4** novelty/prediction-error store-admission gate over lexical dedup (feature)
- [ ] **SOTA#10** bi-temporal "invalidate-not-delete" fact model in the supersede path (feature)

## Pillar 3 — Memory scale & longevity (runs-forever) ✅ scaling addressed
- [x] engram-commit O(n²)-at-import → `commit_many()` (one load + one rewrite, kind-shortlisted dedup); importers batched `engram.py`/`memory_manager.py` (MED)
- [x] read_recent_observations whole-file slurp → bounded `deque(maxlen)` tail read `memory.py` (LOW)
- [x] rotate_if_needed whole-file readlines → O(1) stat size pre-gate `rotation.py` (LOW)
- [x] **SOTA#2** decay + bounded eviction — **ALREADY EXISTS + LIVE**: `StrengthDecayPruneJob` (SHY decay + prune lowest-strength to `LONGTERM_BUDGET=5000`) runs every sleep via `default_sleep_engine`/`run_sleep` (`sleep_engine_enabled=true`). Store bounded → per-tick commit O(n≤5000). Verified wired + tested.
- [~] manager.recall O(n) scan — **bounded** (n≤5000 via prune); vector shortcut fires once embeddings are ON (base config points at `:8082`; live overlay is the operator's call) `memory_manager.py:321` (LOW, mitigated)
- [~] knowledge store growth — **LOW/slow**: with memory_manager on, recall reads engrams; the BM25 store is append-only import-source (idempotent), a disk concern only `knowledge.py:48`
- [~] **SOTA#6** incremental vector store (sqlite-vec) — **not needed given the bounded store** (npy sidecar already incremental, trivial at ≤5000×768). Deferred.
- [~] **SOTA#14** retire dual bookkeeping — focused refactor, not a scaling blocker. Deferred.

## Pillar 4 — Sensory wiring / afferent completeness
- [x] felt body persists across idle ticks via the retained snapshot `nervous/afferent.py` (MED)
- [x] real CPU% on Linux so interoception isn't blind to CPU strain `telemetry.py` (MED)
- [ ] **H4** salience-gate ranked admission never reaches the core → route `drain_block` through `gate.admit` when enabled `nervous/salience.py:262` (HIGH)
- [ ] modulation/metabolism reach the prompt as raw JSON, not qualia → project felt language `nervous/afferent.py:71` (LOW)

## Pillar 5 — The efferent / agency half (dark)
- [ ] efferent half never constructed (Effector/ReflexArc/Proprioceptor/ChangeDetector) → wire ChangeDetector+Effector or mark deferred in caps doc `nervous/__init__.py:15` (MED)
- [ ] no exteroception wired → one real exteroceptor (file/device watch) behind a flag `nervous/exteroception.py` (MED)
- [ ] change-detection / reflex consumers library-only → wire ChangeDetector on the afferent path `nervous/change.py` (LOW)
- [ ] GpuArbiter constructed but never acquires → route leases or delete + document `eidos.py:1831` (MED)

## Pillar 6 — Reward & motivation integrity
- [x] reward freebie (Pillar 1) + PROTECT_PATHS fences `reward.py` (done)
- [x] express `levity`→curiosity floor + `press_scale`→goal-tension floor (bounded, fail-open) `eidos.py` — 2 of 3 dead genes now reach behavior (MED)
- [~] `operator_pull` (affiliation) — **DEFERRED**: no clean behavioral site yet; needs a creature-design decision on where operator-bond should express (not a mechanical wire) (MED)
- [~] restlessness per-domain cutover — **DEFERRED**: the live curiosity path already uses re-encounter-gated learning progress (not raw surprise), so the critical noisy-TV pitfall is already avoided; per-domain frontier-following + genome-shaped restlessness is a motivation-tuning increment best done with live observation (MED→LOW)

## Pillar 7 — Correctness & concurrency ✅
- [x] jobs.json read-modify-write under one lock `tools.py` (MED)
- [x] flag-registered builtins classified correctly at dispatch `tools.py` (LOW)
- [x] uniform WAL tick numbering on LLM-failure paths `eidos.py` (LOW)
- [x] atomic chat-merge write `memory.py` (LOW)
- [x] first-boot seed disunity → eidos sole seed authority, dashboard adopts-or-waits `dashboard.py:374` (LOW)

## Pillar 8 — Portability & host-truth (model-swappable, Linux) ✅
- [x] free-vram STOP Linux no-op → honest "separate llama-swap service" note (no dangerous auto-stop of the mind) `dashboard.py:1844` (LOW)
- [x] SYSTEM_PROMPT_BRIEFING Windows/PowerShell/:8081 → Linux/bash reality + guard test `prompts.py:5` (LOW)
- [~] ~200 lines of PowerShell/WSL lint — **KEEP**: it is guarded (`os.name != "nt"` early-returns) cross-platform Windows support; the README ships Windows/Pi, so removing it drops a supported OS. Not deadweight — intentional. `tools.py:749` (decision: keep)
- [x] committed config.toml dead :8088 tap → canonical gemma@:8080 base `config.toml:16` (LOW)
- [x] embedding route unreachable / model drift → committed `embedding_endpoint=:8082` + `embedding_model=nomic-embed` `config.toml` (LOW)
- [x] deploy/ field-node split-brain → systemd `Conflicts=` both ways + documented canonical topology `deploy/*.service` (MED)

## Pillar 9 — Safety & self-modification
- [x] PROTECT_PATHS: fence reward.py + settlement organs + prompts.py `git_safety.py` (MED)
- [x] autorollback reverts ALL non-protected source → `restore_file_to` the applied file only `selfedit.py` (MED)
- [x] crash-loop auto-rollback `time.sleep(3)` → event-driven `_child_died.wait` `dashboard.py` (LOW)
- [ ] **H5** dashboard control plane unauth on 0.0.0.0 → **OPERATOR DECISION** (lockout risk): default-deny+auto-token+`/api/control/*` gated `dashboard.py:2518` (HIGH)
- [ ] creature bash firewall literal-regex ($HOME escapes) → resolve/expand before match, or OS-jail `tools.py:840` (MED — needs care)

## Pillar 10 — Measurement & test integrity
- [ ] **SOTA#9** METR-style coherent-goal-pursuit horizon KPI (the missing yardstick) (feature)
- [ ] simdays non-determinism + boundary springs damper → seed fully / widen bound / SKIP until seam lands (`test_report` ~50% flaky on main) (MED test-quality)
- [ ] `test_skills_killable` 1s timing watchdog → load-robust or generous bound (LOW test-quality)

---

### Legend / decisions deferred to operator (implemented with a safe default, flagged in-commit)
- **H5 dashboard bind** and the **config.local live reconciliation** carry lockout / operational risk; implemented as a
  mechanism with a behavior-preserving default + loud note, not a silent posture change.
- **Big features** (SOTA wheels) land behind flags where they change live behavior, defaulting off until you enable them.
