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
- [~] **SOTA#5** durable plan artifact — **LARGELY EXISTS**: `plan.md` is a durable working-memory plan, injected in the stable head every tick, updatable via `update_plan`. The refinement (per-objective *structured* subgoal lists with done/blocked state) is an enhancement on top, observation-gated (feature)
- [~] **SOTA#7** error-attribution post-mortem — **PARTIALLY EXISTS**: a goal death writes a verified OBITUARY engram and a refuted block writes a verified correction (`eidos.py`). The structured wrong-belief/bad-action/stuck/missing-capability classification is the enhancement (feature)
- [x] **SOTA#3** strategy memory (distill each quest/objective → retrievable guardrail) — **BUILT** (2026-07-19, `ee4cfd0`). `strategy.py` distils a closed quest OR objective (success AND the doom-loop `_died` release) into a compact trigger→principle `strategy` engram via a bounded grammar-constrained local-LLM call with a deterministic template fallback; committed through the one Consolidator and surfaced by the live recall cascade as `- [strategy] …`. Event-driven at close (ARCH #1), behind `pillars_strategy_memory_enabled` (= true on cmod-s). Scars (failures) born stronger than wins so they persist. Registered in `eidos_capabilities.md` + the briefing. Remaining: **live tuning** of what's stored (observation-gated — watch the guardrails the running creature mints and adjust)

## Pillar 2 — Context & recall efficacy ✅
- [x] BM25 cache invalidates on content change, not just length `knowledge.py` (LOW)
- [x] **H3** post-boot facts recalled → re-import engrams each dream (idempotent) `eidos.py` (HIGH)
- [x] est_tokens → real token estimate `context.py` (LOW)
- [x] stable-head cache → sign on semantic inputs, not persona/creature mtime (KV no longer churns) `context.py` (LOW)
- [~] compaction byte-count — **KEPT**: it biases dreams to fire slightly EARLY, the safe direction vs amnesia; a discount factor risks late-dreaming (LOW)
- [x] **SOTA#10** bi-temporal non-destructive corrections (prior belief + superseded_at) `knowledge.py` (feature)
- [~] **SOTA#4** novelty store-gate — overlap-coefficient near-dup rejection **ALREADY EXISTS** (`knowledge.most_similar`, store-time). The upgrade to prediction-error / "already-implied" gating needs a semantic predictor (embeddings-on) or an LLM pass; scoped as an enhancement, observation-gated (feature)

## Pillar 3 — Memory scale & longevity (runs-forever) ✅ scaling addressed
- [x] engram-commit O(n²)-at-import → `commit_many()` (one load + one rewrite, kind-shortlisted dedup); importers batched `engram.py`/`memory_manager.py` (MED)
- [x] read_recent_observations whole-file slurp → bounded `deque(maxlen)` tail read `memory.py` (LOW)
- [x] rotate_if_needed whole-file readlines → O(1) stat size pre-gate `rotation.py` (LOW)
- [x] **SOTA#2** decay + bounded eviction — **ALREADY EXISTS + LIVE**: `StrengthDecayPruneJob` (SHY decay + prune lowest-strength to `LONGTERM_BUDGET=5000`) runs every sleep via `default_sleep_engine`/`run_sleep` (`sleep_engine_enabled=true`). Store bounded → per-tick commit O(n≤5000). Verified wired + tested.
- [~] manager.recall O(n) scan — **bounded** (n≤5000 via prune); vector shortcut fires once embeddings are ON (base config points at `:8082`; live overlay is the operator's call) `memory_manager.py:321` (LOW, mitigated)
- [~] knowledge store growth — **LOW/slow**: with memory_manager on, recall reads engrams; the BM25 store is append-only import-source (idempotent), a disk concern only `knowledge.py:48`
- [~] **SOTA#6** incremental vector store (sqlite-vec) — **not needed given the bounded store** (npy sidecar already incremental, trivial at ≤5000×768). Deferred.
- [~] **SOTA#14** retire dual bookkeeping — focused refactor, not a scaling blocker. Deferred.

## Pillar 4 — Sensory wiring / afferent completeness ✅
- [x] felt body persists across idle ticks via the retained snapshot `nervous/afferent.py` (MED)
- [x] real CPU% on Linux so interoception isn't blind to CPU strain `telemetry.py` (MED)
- [x] **H4** salience-gate ranked admission → `AfferentContext.attach_gate` routes intake through `gate.admit` (flag-gated, own sub released, felt body preserved) `nervous/afferent.py`/`eidos.py` (HIGH)
- [x] modulation → felt language ("mind feels vigilant"); metabolism hunger already folded into the interoceptive line `nervous/afferent.py` (LOW)

## Pillar 5 — The efferent / agency half ✅ (disposition made)
- [x] **GpuArbiter** — documented as **monitor-only by design** on this single-model-residency host: the only real GPU contender is TTS, already event-driven-arbitrated by voice.py's speech-gate; routing the mind's decode through leases would add a round-trip for zero benefit and risk serializing the hot path. Leases land only under multi-model/escalated-perception (future-host manifest). `eidos.py:1829` (MED)
- [~] efferent half (Effector/efference-copy/ReflexArc/Proprioceptor) + exteroception + change-detection → **EMBODIMENT-GATED DEFERRAL** (honest disposition, not force-wired). Reasons: (1) there is **no exteroceptive source** (camera/mic/device stream) and **no actuator** on this desktop host, so an Effector/reflex/exteroceptor would gate/drive nothing; (2) a ChangeDetector ("only what changed rises") on the interoceptive stream would actively **fight the felt-body continuity** just fixed (it would suppress the unchanged body the creature should always feel), and there is no exteroceptive stream for it to usefully gate. This half is the **box→bot roadmap step**: it lands *with* real senses + effectors, where novelty-gating and efference-copy are meaningful. Wiring it blind now would be net-negative, not a fix. `nervous/{change,efferent,exteroception}.py` (MED, deferred with reason)

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

## Pillar 9 — Safety & self-modification ✅
- [x] PROTECT_PATHS: fence reward.py + settlement organs + prompts.py `git_safety.py` (MED)
- [x] autorollback reverts ALL non-protected source → `restore_file_to` the applied file only `selfedit.py` (MED)
- [x] crash-loop auto-rollback `time.sleep(3)` → event-driven `_child_died.wait` `dashboard.py` (LOW)
- [x] **H5** — control plane was ALREADY token-gated on every POST (phase 8.1; finding stale). Added `[dashboard] host` (bind 127.0.0.1 to restrict) + a loud open-control-plane boot warning — securable without lockout `dashboard.py`/`config.py` (HIGH)
- [x] firewall env-expansion escapes → deny `$HOME`/`$OLDPWD`/`$USERPROFILE`/… in creature mode (general `$vars`/`$(...)` still allowed) `tools.py` (MED)

## Pillar 10 — Measurement & test integrity ✅
- [x] **SOTA#9** coherent-goal-pursuit horizon KPI → `telemetry.record_goal_horizon` (bounded rolling summary; derail = loop/rotation/park/death/escalation) `eidos.py`/`telemetry.py` (feature)
- [x] simdays non-determinism → seed a fixed germline from the sim seed (congenital baselines were os.urandom); `test_report` now 8/8 (was ~50%) `simdays.py` (MED)
- [x] `test_skills_killable` 1s watchdog flake → generous 5s ceiling for the good-skill case `tests/test_skills_killable.py` (LOW)

---

### Legend / decisions deferred to operator (implemented with a safe default, flagged in-commit)
- **H5 dashboard bind** and the **config.local live reconciliation** carry lockout / operational risk; implemented as a
  mechanism with a behavior-preserving default + loud note, not a silent posture change.
- **Big features** (SOTA wheels) land behind flags where they change live behavior, defaulting off until you enable them.
