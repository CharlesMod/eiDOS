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
- [ ] loop-detector arg-vary-defeatable + toothless → normalize all tool sigs; give `loop_detected` a structural consequence `eidos.py:2596` (LOW)
- [ ] backlog-exhaustion soft-stall → mint a bounded exploratory objective on full-park `objectives.py:517` (LOW)
- [ ] level_gates stale DARK docstring → correct it `level_gates.py:23` (LOW)
- [ ] **SOTA#1** wire the built-but-dark learning-progress signal into curiosity + enable LP-XP `learning_progress.py`/`nervous/curiosity.py` (feature)
- [ ] **SOTA#3** ReasoningBank-style strategy memory (distill each quest, success+failure → retrievable guardrail) (feature)
- [ ] **SOTA#5** durable externalized per-objective PLAN artifact injected every tick (feature)
- [ ] **SOTA#7** AgentDebug-style error-attribution post-mortem on park/loop → strategy memory (feature)

## Pillar 2 — Context & recall efficacy
- [x] BM25 cache invalidates on content change, not just length `knowledge.py` (LOW)
- [ ] **H3** post-boot facts never recalled by relevance → route memorize/dream writes through `manager.encode` (or periodic idempotent re-import) `eidos.py:755` (HIGH)
- [ ] est_tokens telemetry is digit-length nonsense → real token estimate `context.py:1380` (LOW)
- [ ] compaction gate byte-inflation → count content tokens, not JSONL bytes `compaction.py:47` (LOW)
- [ ] stable-head cache defeated every tick → sign on semantic inputs, not persona/creature mtime `context.py:1102` (LOW)
- [ ] **SOTA#4** novelty/prediction-error store-admission gate over lexical dedup (feature)
- [ ] **SOTA#10** bi-temporal "invalidate-not-delete" fact model in the supersede path (feature)

## Pillar 3 — Memory scale & longevity (runs-forever)
- [ ] engram-commit full-store rewrite per acting tick (O(n²) at import) → append-mostly + compact on sleep `engram.py:615` (MED)
- [ ] knowledge store unbounded; most_similar/BM25 O(n) on live path → budget + sleep-time prune `knowledge.py:48` (LOW)
- [ ] manager.recall scans whole store each tick → vector shortcut / within-tick cache `memory_manager.py:321` (LOW)
- [ ] read_recent_observations slurps whole file per call → bounded tail read `memory.py:303` (LOW)
- [ ] rotate_if_needed readlines whole file → stat size pre-check `rotation.py:22` (LOW)
- [ ] **SOTA#2** decay + bounded eviction on the engram strength field (MemoryBank/Ebbinghaus) (feature)
- [ ] **SOTA#6** embedded incremental vector store (sqlite-vec / LanceDB) (feature)
- [ ] **SOTA#14** retire dual bookkeeping — engram economy as single source of truth (feature)

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
- [ ] restlessness_signal + its genome gene dark → do the 4.2 cutover into curiosity (pairs with SOTA#1) `learning_progress.py:237` (MED)
- [ ] 3 GENOME_V3 genes (`operator_pull`/`levity`/`press_scale`) read by nobody → wire at their intended press sites `genome.py:138` (MED)

## Pillar 7 — Correctness & concurrency
- [x] jobs.json read-modify-write under one lock `tools.py` (MED)
- [x] flag-registered builtins classified correctly at dispatch `tools.py` (LOW)
- [x] uniform WAL tick numbering on LLM-failure paths `eidos.py` (LOW)
- [x] atomic chat-merge write `memory.py` (LOW)
- [ ] first-boot seed disunity (dashboard mints its own seed) → eidos is sole seed authority `dashboard.py:394` (LOW)

## Pillar 8 — Portability & host-truth (model-swappable, Linux)
- [ ] free-vram STOP is a Linux no-op naming Windows services → `systemctl` path or remove the false guarantee `dashboard.py:1844` (LOW)
- [ ] SYSTEM_PROMPT_BRIEFING still says Windows/PowerShell/:8081 → Linux/bash reality or parameterize `prompts.py:5` (LOW)
- [ ] ~200 lines of PowerShell/WSL lint-translate dead on POSIX → move behind a win32-only module `tools.py:749` (LOW)
- [ ] committed config.toml aims the mind at the dead :8088 tap → canonical gemma@:8080 base `config.toml:16` (LOW)
- [ ] embedding route unreachable via committed config; model/dim drift → one source of truth `embedding.py:217` (LOW)
- [ ] deploy/ field-node units would run two eidos + wrong user/interpreter → one topology `deploy/eidos.service:10` (MED)

## Pillar 9 — Safety & self-modification
- [x] PROTECT_PATHS: fence reward.py + settlement organs + prompts.py `git_safety.py` (MED)
- [ ] **H5** dashboard control plane unauth on 0.0.0.0 → default-deny + auto-token + `/api/control/*` gated `dashboard.py:2518` (HIGH, operator-posture)
- [ ] autorollback reverts ALL non-protected source → `restore_file_to` the applied file only `selfedit.py:322` (MED)
- [ ] creature bash firewall is literal-regex ($HOME escapes) → resolve/expand before matching `tools.py:840` (MED)
- [ ] crash-loop auto-rollback `time.sleep(3)` → event-driven wait `dashboard.py:2274` (LOW)

## Pillar 10 — Measurement & test integrity
- [ ] **SOTA#9** METR-style coherent-goal-pursuit horizon KPI (the missing yardstick) (feature)
- [ ] simdays non-determinism + boundary springs damper → seed fully / widen bound / SKIP until seam lands (`test_report` ~50% flaky on main) (MED test-quality)
- [ ] `test_skills_killable` 1s timing watchdog → load-robust or generous bound (LOW test-quality)

---

### Legend / decisions deferred to operator (implemented with a safe default, flagged in-commit)
- **H5 dashboard bind** and the **config.local live reconciliation** carry lockout / operational risk; implemented as a
  mechanism with a behavior-preserving default + loud note, not a silent posture change.
- **Big features** (SOTA wheels) land behind flags where they change live behavior, defaulting off until you enable them.
