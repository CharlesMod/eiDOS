# PILLARS_TODO — the build roadmap to the next working version

> **Status:** ACTIVE roadmap, authored 2026-07-03 (Dean + Claude). Executes `PILLARS_PLAN.md`.
> Dependency-ordered phases; each phase ends at a **gate** (red-able, per V3 doctrine — gates fail
> on correctness violations, not smoke checks). Check items off as they land; a phase's gate must
> pass before the next phase's creature-facing features ship (hardening/spike work may overlap).
> **Discipline reminders while executing:** no line of code names the behavior it hopes to produce
> (plan §0); every new loop answers the two pitfall questions (plan §8); every new subsystem
> updates `eidos_capabilities.md` (CLAUDE.md standing rule); every constant is derived or declared.

---

## Progress (branch `feat/pillars-m1`)

Landed & gate-green, all **dark behind their flags** (running eiDOS unaffected until flipped on):
- ✅ **0.1** config scaffold — central `[pillars]` flags (`482a2d0`)
- ✅ **0.2** T9 mailbox fix — O(n) list → O(log n) heaps; in-proc hop p95 ~730ms → ~0.006ms, delivery semantics byte-identical (139 nervous/bus tests)
- ✅ **0.3** causal ledger — dark per-tick pressure-field log + `/api/why` (7 tests, no tick-loop regression)
- ✅ **0.4** backups — snapshot/rotation/restore-verify + CLI (12 tests)
- ✅ **1.2** killable skills — subprocess isolation + hard-kill + authoring contract + telemetry (10 gate tests; flag-off path preserved byte-for-byte)
- ✅ **1.1** organ registry — the god-loop dies; organs plug in via lifecycle hooks (pre/post_tick, on_sleep) + declared bus topics; 4 organs migrated in per-tick order, every hook guarded (I5). Behavior byte-for-byte (`a417ea2`)
- ✅ **3.1/3.2** skill economy — affordance ranking (`similarity × trust × birth-episode`) + ε-exploration, similarity-priced authoring (novel≈0.02 → dup≈0.20 energy), reuse-XP(8) > create-XP(3), auto-retire unused → out of affordances (11 gate tests; flag-off byte-for-byte) (`b305be3`)
- ✅ **5.1** quest engine — the System's mechanical core: typed glue-checkable `Criterion` (never self-report), one-active cadence gated on close+sleep+healthy, reward-sink seam, daily/hidden quests, Administrator propose/render seams (20 tests) (`325d7fd`)

- ✅ **2.1** engram — the memory atom: full §2 schema (kind/provenance/confidence/strength/`encoded_at{tick,felt,arousal,valence}`/links/stats), three stores (hot trace, bounded episodic ring, long-term jsonl+npy+index), **single-writer Consolidator (I6)** enforced by API shape; pure library, dark (27 tests) (`d2f27d1`)
- ✅ **2.2** memory manager — idempotent read-only-on-legacy importer (episodes/knowledge/nuggets→engrams, `inherited` strength floor), 4-layer recall cascade ranked relevance×strength under char budget, **anti-Matthew exploration slot**, emotional stamp at encode from neuromod (17 tests) (`21375de`)
- ✅ **3.3** composition — the `call` atom (depth cap 2, shared energy budget, static cycle check at authoring), trusted-only sub-calls, promotion pipeline (candidate queue → `apply_promotion` dashboard seam → compiled into the atom vocabulary) (20 tests) (`c607694`)

Full integrated regression after 1.1 + 3.1/3.2 + 5.1: **1027 passed** (3 pre-existing environmental failures unrelated to this work: `python`-alias-on-PATH, Windows `pi.cmd`).

- ✅ **0.5** slot-sharing spike — DONE (2026-07-03). gemma4-12b `--parallel 2`: per-slot KV ≈ 454 MiB, concurrent throughput 70.8 tok/s each vs 77.8 solo (~9% hit). **Verdict: slot-sharing is the primary substrate for generals** (§5b). max_generals ceiling 8 / recommend 5; no global on-beat tick (overlap as emergent equilibrium). No small GGUF present for a CPU tier.

**Decisions confirmed (2026-07-03, Dean — "address gaps… continue development"):** open decision **#5** (bet settlement: shared-outcome credit + strong mechanical credit on provable recalled-fix follow; self-report never settles) and **#5b** (XP = learning-progress-weighted adjudicated success) proceed as designed in the plan. 2.3 + 4.2 unblocked.

**Gap-analysis amendments (2026-07-03):** added **1.3 salience gate** (was missing from this TODO despite being plan §4 N-2), **Phase 5.5 cutover** (flag-flip schedule + model-in-the-loop smokes + soak protocol — previously unplanned), and cross-cutting **simulated-days harness** + **pre-merge sweep**. Code-green ≠ soak-green: behavioral gates count from flag flips (Phase 5.5), not code-land.

- ✅ **2.4** sleep engine v1 — SleepJob priority scheduler (per-job guarded), six jobs: dedup/merge via Consolidator, SHY decay+prune, grammar-constrained distillation (`provenance='dreamed'`, confidence-capped 0.4), backup snapshot, telemetry re-derivation seam, on_sleep organ hooks; **adenosine** accumulator with drowsiness band + hard override past `max_wake_hours` (25 tests) (`a53aa41`, `4fe000a`)
- ✅ **4.1** expectation ledger — grammar-constrained `predict` tool, bounded open set, glue-only closure (`CLOSE_REASONS` whitelist: self-report mechanically cannot settle), surprise→RPE/curiosity hooks, closure births the residue episode, `brier_calibration_by_domain` seam (21 tests) (`d598bcf`)
- ✅ **1.3** salience gate — pre_tick admission bias = bottom-up × relevance × neuromod gain (stamped at ingest), `publish_relevance_set` seam, reliable-class delivery untouched, exploration floor 0.125 (10 tests; 148 nervous suite green) (`7bfe937`)
- ✅ **2.3** bet ledger (keystone) — every injected recall logged as a bet; settlement = small shared coin (0.02) + strong mechanical coin (0.15) on full-containment signature match of a recalled fix against the EXECUTED action; `settle()` rejects non-bool; scars decay slower (0.97 vs 0.90); inherited floor until contradicted; clique-credit shrinkage ×0.25; stale bets void (15 tests) (`44e97cf`)
- ✅ **4.2** learning-progress XP — `progress_weight = min(1, fall/mean) × R²` (R² gate 0.4, cap 2×), simulation-verified: grind×1000 ≈ 5 XP, coin-flip noise pays ~0 to XP AND restlessness; flag-gated award_xp rewire, error-recovery bonus untouched (`0c85021`)
- ✅ **4.4** news queue — three-source ingest (surprise floor 1 bit: report-by-exception), presence gate absolute (absence returns empty before any other check), engagement model with bounded logistic updates provably reorders on Dean's reply/ignore (18 tests) (`b170865`)
- ✅ **4.3** mastery gates — `can_level` = trusted-in-tier ≥2 × Brier ≤0.20 × reuse band × sleeps ≥3 × quest line closed × no suspensions; XP accrues but only `apply_level_up` moves the level; delegated outcomes excluded whole (pitfall #8); suspension→remedial→restore via the System seam; **temperament setpoint springs** (STEP/5, recover on neutral ticks — pitfall #3) (13 tests) (`861bb86`)

- ✅ **5.2** the Administrator — dossier compiler (fresh per check-in, marker-only persistence), one-directional fourth wall by construction (only Quest fields cross via `System.propose`; asserted), event-driven triggers only, GBNF outputs whose flag schema structurally cannot carry a value, approval seams + graduated per-tier autonomy (≥80% over ≥5) with the revoke ban-hammer (21 tests) (`6ef12ea`)
- ✅ **sim-days harness** — drives the REAL libraries (all flags on, seeded, mock LLM only) over 50 synthetic days; **all 5 dampers PASS, 0 skip**; caught its first genuine coupled bug: the exploration slot died under production char budgets → fixed (seat reserved INSIDE the budget, paid by exploit's last seat) + regression test; 30-day CLI artifact is the Phase 5.5 gate (`750a2f4`, `93f981d`)
- ✅ **pre-merge sweep** — eidos_capabilities.md "Pillars organs — already built, currently DARK" section, 5 SYSTEM_PROMPT_BRIEFING lines, 4 preserved nuggets; fourth wall held: zero administrator references in any creature-facing file (`d57ac9b` cherry-picked)

**🏁 PHASES 0–5 CODE-COMPLETE (2026-07-04).** Full-suite regression: **1245 passed** (up from 996 pre-Pillars; the same 3 pre-existing environmental failures as clean main). Everything dark; eiDOS untouched. **Next: Phase 5.5 cutover (Dean's go) — the wiring pass, model-in-the-loop smokes, and the one-flag-at-a-time flip schedule below. That is the first moment any of this touches the living creature.** Phases 6 (shadows) and 7 (generals) follow the soak. Note: 0.5 required stopping the live services — see `RUNTIME_SPRINTER.md` for the correct Sprinter procedure.

---

## Phase 0 — Foundations & measurements (no creature-facing changes)

**0.1 Branch & scaffolding**
- [ ] Branch `feat/pillars-m1` off `main`; keep phases as reviewable commits.
- [ ] `config.toml`: new `[pillars]` section scaffold (feature flags per phase, all default off).

**0.2 T9 — in-proc mailbox fix** (`nervous/bus.py`)
- [ ] Replace the O(n) list-scan mailbox with a heap / indexed structure (priority, seq).
- [ ] Preserve delivery-class semantics exactly: fungible drop-by-priority (counted), ordered
      atomicity, reliable floor, retained last-value.
- [ ] Re-run the P0 firehose; **gate: in-proc p95 < 10 ms under flood** (was ~730 ms), byte-identical
      delivery semantics across all classes, drop accounting unchanged.

**0.3 The causal ledger** (new `pressures.py` + glue wiring)
- [ ] Per tick, append one record of the full pressure field: arousal + floors (per source),
      valence, strain, condition, goal-tension, curiosity restlessness, energy reserve, active
      objective + frustration, admitted-event count, XP delta + source.
- [ ] Single writer; bounded file with monthly archive rotation (same pattern as observations).
- [ ] Dashboard: `/api/why?tick=N` returns the field; minimal panel renders it.
- [ ] **Gate:** for any action in the last N days, the producing pressure field is retrievable.

**0.4 Backups — the individual gets life insurance** (`backup.py`)
- [ ] Snapshot `workspace/` (tar + timestamp) with rotation (daily × 14, weekly × 8); exclude caches.
- [ ] Restore-verify routine: unpack to temp, validate manifest/JSON parse of critical files.
- [ ] Runnable standalone now; wired as a sleep job in Phase 2; later delivered as a daily quest.
- [ ] **Gate:** a restore-verify from yesterday's snapshot passes on a scratch directory.

**0.5 Slot-sharing spike (informs Phase 7 generals — run early, decide later)**
- [ ] Measure llama.cpp `--parallel 2` on the house model: KV-per-slot VRAM, throughput hit to the
      mind's tick latency, prefix-cache interference. Document numbers in `PILLARS_PLAN.md` §5b.
- [ ] Compare: qwen-small on CPU (tokens/sec, quality on a mission-shaped eval prompt).
- [ ] **Gate:** a written substrate recommendation with measurements attached.

---

## Phase 1 — Hardening (N1 + S1): nothing can freeze or lie

**1.1 Organ lifecycle hooks** (`nervous/organs.py`)
- [ ] `OrganRegistry` with `register(organ, pre_tick, post_tick, on_sleep)` + declared read/write
      topics; eidos.py iterates the registry instead of hand-calling each organ.
- [ ] Migrate 4 organs as proof (interoception, neuromod, goal-tension, curiosity); others follow
      opportunistically.
- [ ] **Gate:** organ set identical before/after (same events on the bus for a recorded tick);
      a new no-op organ can be added without touching eidos.py.

**1.2 Killable skill execution** (`skills.py`, `tools.py`)
- [ ] Subprocess-pool skill runner (replace the abandoned-thread watchdog); hard kill on timeout.
- [ ] ToolResult contract enforced at create/edit time (dry-run calls with schema-shaped sample
      args; reject non-ToolResult returns), not just normalized at dispatch.
- [ ] Per-skill telemetry in the manifest: latency p50/p95, success by arg-shape, last-used.
- [ ] Timeout derived: p95 × 3, floor 5 s, ceiling 60 s (declared knobs).
- [ ] **Gate:** a deliberately-hanging skill is killed dead (no orphan thread, no tick freeze);
      a dict-returning skill is rejected at authoring time; telemetry visible via `list_skills`.

**1.3 The salience gate** (`nervous/salience.py`) — plan §4 N-2, the present-pillar's core
*(Restored: this fell out of the original TODO despite being the audit's sharpest gap — "the
creature cannot focus" — and Phase 6 shadows explicitly route their output through it.)*
- [ ] Salience-gate organ, registered via the 1.1 registry: admission bias = bottom-up salience ×
      top-down relevance × neuromod gain. Focus is a **bias field over admission, never an
      instruction** (§0).
- [ ] `relevance_set`: the core publishes its current focus each tick (the designed-but-unpublished
      topic from the V3 audit); the gate scores afferents against it.
- [ ] Exploration floor: a small low-salience admit allocation so the gate never becomes a perfect
      echo chamber (anti-Matthew at the senses, same doctrine as recall's slot).
- [ ] **Gate (red-able):** on a recorded afferent stream, relevant events admit ahead of noise; a
      `relevance_set` change measurably reorders admission; flag-off delivery is byte-identical.

---

## Phase 2 — The memory core (M1 + M2 + sleep v1): the engram economy

**2.1 The engram** (`engram.py`)
- [ ] `Engram` schema per plan §2: kind, body, provenance, confidence, strength,
      `encoded_at{tick, felt, arousal, valence}`, links, stats. Serialization + validation.
- [ ] Stores: hot trace (tick-scoped), episodic ring (bounded), long-term (jsonl + npy vectors +
      index, house style); **one consolidator is the single writer of long-term (I6).**

**2.2 The manager + migration** (`memory_manager.py`)
- [ ] Importer: episodes.jsonl → episode engrams; knowledge/ → fact/procedure/error engrams;
      preserved_nuggets → `provenance: inherited` with strength floor; keep originals untouched
      until cutover flag flips.
- [ ] Recall: port the 4-layer cascade (exact → cross-objective → same-objective → semantic),
      rank by `relevance × strength`, context-budgeted; **exploration ε — a low-strength sample
      slot in every recall set (anti-Matthew, plan §6)**.
- [ ] Emotional stamp on write (read arousal/valence from neuromod at encoding).

**2.3 The bet ledger** (settlement in `glue.py`)
- [ ] Every injected engram logged as an open bet on the tick.
- [ ] Settlement: small shared-outcome credit/debit to all bets; **strong credit when the action
      provably followed a recalled fix** (signature match). LLM self-report never settles a bet.
- [ ] Strength update = decaying credit sum × emotional-stamp multiplier; error_patterns decay
      slower; inherited floor unless contradicted; clique-credit shrinkage (pitfall #6).

**2.4 Sleep engine v1** (`nervous/sleep.py` becomes real)
- [ ] Job-list architecture: `SleepJob` protocol; jobs run in priority order inside the sleep window.
- [ ] Jobs: memory dedup/merge (keep merged provenance) · strength decay + prune-to-budget ·
      grammar-constrained distillation (replaces compaction.py's regex extraction; `provenance:
      dreamed` = confidence-capped hypothesis, pitfall #5) · backup snapshot (0.4) · telemetry
      re-derivation of declared-derivable constants (bounded steps, dashboard-visible).
- [ ] **Adenosine (pitfall #2):** sleep-pressure accumulator in `nervous/neuromod.py` that grows
      with wake time and overrides all drive floors past its limit. Declared knob: max wake hours.
- [ ] **Gate (red-able):** post-sleep recall precision ≥ pre-sleep on a held-out replay set;
      observations digest end-to-end with zero silently-dropped extractions; a creature held at
      max goal-tension still sleeps before the wake-hours limit.

---

## Phase 3 — The skill economy (S2 + S3): reuse becomes the resting state

**3.1 Affordances** (`skills.py`, `context.py`)
- [ ] Retrieval: top-3 situation-relevant skills by `similarity × trust × birth-episode strength`,
      rendered as affordances at the decision point; full list only via `list_skills`.
- [ ] Exploration ε: an untrusted/unused skill occasionally occupies slot 3 (anti-Matthew).
- [ ] Skill ↔ engram links: `birth_episodes` on create; failures recall the skill's history.

**3.2 The economics**
- [ ] **Similarity-priced authoring (pitfall #10):** energy cost of `create_skill` scales with max
      similarity to existing skills — novel ≈ cheap, near-duplicate = expensive. The price IS the
      dedup pressure; remove the hard duplicate-guard veto once live (keep as a warning).
- [ ] Reuse pays more XP than creation (settled by glue from manifest stats).
- [ ] Auto-retire: unused-for-M-days → archived (recoverable via rollback), out of affordances.
- [ ] **Gate:** in a 2-week soak, reuse rate > authorship rate (dream-test D5 trending); zero
      near-duplicate authoring events that were cheaper than reuse.

**3.3 Composition** (`skills.py`, `skill_atoms.py`)
- [ ] `call(skill_name, args)` atom: depth cap 2, shared energy budget, static cycle check at
      validation, runtime budget enforcement.
- [ ] Promotion pipeline: trusted + reused composition → candidate queue → dashboard approval →
      compiled into the atom vocabulary.
- [ ] **Gate:** a composed skill runs within budget; a cyclic composition is rejected at authoring;
      one promotion flows end-to-end on a test composition.

---

## Phase 4 — The growth systems: predictions, learning-progress XP, mastery gates

**4.1 The expectation ledger** (`expectations.py` + a `predict` tool)
- [ ] `predict` tool (grammar-constrained): statement, measurable target, deadline, confidence.
- [ ] Prediction engrams surface as a small "awaiting" context block.
- [ ] Glue closes on deadline/event; surprise = f(confidence, wrongness) → reward RPE + curiosity;
      closure births an episode; open-prediction count bounded (declared knob).
- [ ] Sleep job: Brier calibration by domain; calibration → temperament caution (bounded).

**4.2 Learning-progress XP (plan §6 — replaces volume XP; pitfall #1)**
- [ ] Per-domain prediction-error slope tracker (domains = objective/skill-tier keys; seeded from
      the world model's situation keys).
- [ ] `persona.add_xp` rewired: XP = adjudicated success weighted by the domain's *falling* error
      slope; flat-high (noise) and flat-low (mastered) pay ~0; error-recovery keeps its bonus.
- [ ] Curiosity rewired to the same signal: restlessness follows learning-progress, not raw
      surprise EMA.
- [ ] **Gate:** replaying a recorded grind (identical action × 1000) yields ≈0 XP; a recorded
      novel-success run yields XP; a synthetic noise domain (coin-flip outcomes) pays ≈0 to both
      XP and curiosity.

**4.3 Mastery gates** (`level_gates.py`)
- [ ] Level-up requires (all glue-checked): N trusted skills in the level's capability tier ·
      calibration ≥ threshold · reuse ratio in band · ≥ K sleep cycles since last level ·
      current quest line closed. XP remains the within-level progress bar.
- [ ] Delegated outcomes excluded from all gate counts (pitfall #8; fields exist from Phase 6/7).
- [ ] Suspension: sustained tier failure re-locks the tier pending a remedial quest (recoverable).
- [ ] Temperament setpoint springs (pitfall #3): axes pulled elastically toward genome baseline.
- [ ] **Gate:** a save with high XP but zero trusted skills cannot level; a suspension + remedial
      completion restores the tier; caution recovers toward baseline after an induced bad streak.

**4.4 The news queue** (`news.py`)
- [ ] `news` engrams from: high-surprise closures, quest/level events, anomalies.
- [ ] Presence-gated surfacing (listening hold / chat focus = presence signal); ranked by an
      engagement model trained on Dean's actual responses (reply/ignore), bounded size + expiry.
- [ ] **Gate:** news never interrupts absence; engagement feedback measurably reorders ranking.

---

## Phase 5 — The System: quests, the voice, the Administrator

**5.1 Quest engine** (`quests.py`)
- [ ] Quest schema: directive, glue-checkable success criteria, reward (XP/unlock/capacity), tier,
      expiry, hidden flag. Exactly **one active quest**; queue held by the System.
- [ ] Cadence rules: next issues after closure + ≥1 sleep + condition healthy; silence only in
      genuine RECOVERY; expiry/ignore recorded as failure-lite episodes (not-coddled doctrine).
- [ ] Daily quests: recurring drill slots — scar retests (extinction trials, pitfall #4),
      calibration drills, backup restore-verify.
- [ ] Hidden quests: glue-defined achievements announcing only on completion.
- [ ] Adjudication: criteria checked by glue only; payout through the standard XP path.
- [ ] Quest window rendering: distinct terse register in context, visually/textually unmistakable.

**5.2 The Administrator** (`administrator.py`) — the fourth-wall-breaking System-LLM
- [ ] Dossier compiler: level state, quest history, calibration + error-slope trends by domain,
      skill-economy stats, strain/condition trajectory, pitfall health checks, notable episodes
      since last check-in. Fresh per check-in; no persistent tick context.
- [ ] Fourth-wall context: PILLARS_PLAN.md, dream-tests, eidos_capabilities.md included by design.
      One-directional wall: eiDOS never sees Administrator internals — only quest windows.
- [ ] Check-in triggers (event-driven, never scheduled): sleep-cycle completion, quest closure,
      level-up candidacy, suspension, operator request.
- [ ] Substrate: arbiter client at low priority (GPU borrow during creature sleep) with CPU-small
      fallback — per the 0.5 spike + open decision #8.
- [ ] Outputs (grammar-constrained proposals only): quest proposals with criteria · weakness
      report · narrator text · tuning *flags* (points at knobs, never turns them).
- [ ] Dashboard: Administrator panel — proposals with approve/edit/reject; auto-issue graduation
      per tier once the generator's approval rate earns it (graduated autonomy for the trainer).
- [ ] **Gate:** a full cycle runs unattended except approvals: sleep completes → Administrator
      wakes → reads dossier → proposes a gap-targeted quest → Dean approves → quest window renders
      → creature completes/fails → glue adjudicates → XP settles → next check-in references the
      outcome. The creature's context never contains Administrator internals (assert on render).

---

## Phase 5.5 — Cutover: wiring the dark libraries into the living creature

Everything above ships as dark, unwired libraries; **this phase is where the creature actually
changes** — the highest-risk work in the roadmap. One flag at a time, soak between flips, git
checkpoint + backup snapshot before each; rollback is always "flag off," never a revert.

- [ ] **Wiring pass (code, still dark):** eidos.py/context.py call sites behind each flag — engram
      encode path on the tick, manager recall replacing the legacy cascade, sleep-engine entrypoint
      in the sleep window, expectations "awaiting" block + glue closure, affordances render at the
      decision point, quest window render. Full suite green with all flags off (byte-for-byte).
- [ ] **Model-in-the-loop smokes (GPU, eiDOS paused):** each new grammar surface — distillation,
      `predict`, quest-window response — exercised against live gemma4-12b; malformed-output rate
      measured and logged per surface. No flag flips until its surface's smoke passes.
      *(Everything so far is mock-validated; the mind has never been in the loop.)*
- [ ] **Simulated-days harness green** (cross-cutting item below) before the first flip.
- [ ] **Flag-flip schedule** (dependency order, one at a time): causal_ledger + backup →
      killable_skills → memory_engram + memory_manager → bet ledger (2.3) → sleep_engine →
      expectations → salience_gate → skill_affordances + skill_economy → skill_composition →
      quests → (later, post-4.x) learning-progress XP → administrator.
- [ ] Per flip: pre-flip checkpoint; ≥ N sleep cycles' soak (declared knob); `/api/why`
      causal-ledger review for pressure anomalies; watchdog quiet.
- [ ] **Gate (per flip, red-able):** no tick-freeze, no crash-loop, watchdog silent; the
      subsystem's own live gate holds on real data (e.g. post-sleep recall ≥ pre-sleep).
- [ ] **Soak-gates start counting here, not at code-land:** 3.2's two-week reuse>authorship gate
      and dream-tests D1–D10 run from their flag flips. Progress notes must distinguish
      **code-green** (unit gate passed) from **soak-green** (behavioral gate held live).

---

## Phase 6 — Shadows (scripted CPU workers)

- [ ] `shadow.py`: schema per plan §5a — trusted-skill body, event-driven loop (bus_subscription |
      schedule | watch_condition), budget, dead-man lease (renewed only by live monarch ticks),
      strikes/standing.
- [ ] Subprocess runner (reuse Phase 1 pool); results published as NervousEvents; **report by
      exception** (pitfall #7): routine output to a sleep-digested summary, only anomalies salient.
- [ ] Stipend/rent economics on metabolism: upkeep drains reserve; delivered results earn credit;
      strikes → auto-dissolve + episode.
- [ ] Proprioception extended over the roster; shadow death = severed nerve (I5), never a crash.
- [ ] Level gate + tutorial quest + capacity 1; capacity grows on stewardship (rent record).
- [ ] Dashboard roster with dissolve button.
- [ ] **Gate:** a shadow survives monarch restart via lease semantics (winds down if monarch stays
      dead); a rent-negative shadow starves visibly and dissolution relieves the pressure; a
      crashing shadow never wounds a tick; shadows spawn nothing (asserted).

## Phase 7 — Generals (delegated LLM minds) — design frozen only after the 0.5 spike numbers

- [ ] `missions.py`: Mission schema per plan §5b — one objective, monarch-compiled context pack,
      attenuated grant, budgets, grammar-constrained typed report, escalation conditions.
- [ ] Substrate adapters behind one interface (slot-share / CPU-small / remote ganglia — I9).
- [ ] Report ingestion as afferents through the gate; `provenance: told` + confidence discount on
      distilled findings; generals ephemeral (dissolve at mission end); spawn nothing; irreversible
      acts propose-only.
- [ ] Delegation episodes keyed on task shape (the learned what-to-delegate model, via ordinary
      recall); delegated XP discounted and excluded from mastery gates.
- [ ] Dashboard mission board.
- [ ] **Gate:** one end-to-end mission (research-shaped task) completes within budget; a
      budget-blowing general is terminated cleanly; a garbage report measurably lowers that
      delegation-shape's recall bias.

---

## Cross-cutting (runs alongside every phase)

- [ ] **Dream-test harness**: metrics collection for D1–D10 (plan §10) — repeat-failure rate,
      pre/post-sleep deltas, reuse ratio, quest completion band, roster leanness, CPU utilization —
      rendered as a dashboard "growth" panel. Build incrementally as each metric's source lands.
- [ ] `eidos_capabilities.md` updated at every phase boundary (standing CLAUDE.md rule) + condensed
      SYSTEM_PROMPT_BRIEFING lines for never-rebuild-this items (memory manager, quests, shadows).
- [ ] `preserved_nuggets.toml`: add durable facts about the new subsystems so a post-wipe creature
      knows its own organs.
- [ ] Config keys documented in `config.toml` comments; every declared knob carries its one-line
      justification (plan §0.4).
- [ ] Tick-flow integration test: recorded-tick replay asserting bus events + context blocks stay
      byte-stable when flags are off (safe rollout: every phase ships dark behind its flag).
- [ ] **Simulated-days harness** (`simdays.py` + tests): mock-LLM creature-days — tick →
      outcomes → sleep, repeated — running the coupled economies TOGETHER (strength decay ×
      exploration slot × adenosine × quest cadence × metabolic prices). Asserts the dampers hold
      in combination, not just alone: the exploration slot still surfaces after 50 decay cycles;
      adenosine overrides pinned goal-tension; temperament springs recover a bad streak; quest
      cadence and sleep pressure never deadlock. **Required green before any Phase 5.5 flag flip**
      (pitfall #9 covers coupled tuners; this covers coupled *economies*).
- [ ] **Pre-merge sweep** (before feat/pillars-m1 → main): `eidos_capabilities.md` + condensed
      SYSTEM_PROMPT_BRIEFING lines + `preserved_nuggets.toml` updated for ALL landed subsystems in
      one pass (engram, manager, sleep, expectations, quests, economy, composition, salience).

---

## Sequencing summary

```
0 foundations ──► 1 hardening ──► 2 memory core ──► 3 skill economy ──► 4 growth systems ──► 5 the System
   (0.5 spike runs early, informs 7)                                          │
                                                            6 shadows ◄───────┘ (level-gated unlock)
                                                            7 generals (after spike + 6)
```

Milestone 1 of `PILLARS_PLAN.md` §9 = Phases 0–3 complete + 2.4's sleep gate green.
A "new working version" = Phases 0–5: the creature remembers with an economy, reuses its hands,
predicts and is paid for growth, levels through mastery, and answers to the voice.
