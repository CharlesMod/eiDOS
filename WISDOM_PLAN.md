# WISDOM_PLAN — lived experience as pre-done thinking

*(Charlie + Claude, 2026-07-20. Status: approved for build. Companion to WORLD_PLAN.md.)*

## §-1 The thesis

The weights are fixed — that is the creature's fluid intelligence, set at birth. But cognition
at runtime is **weights × context**, and context can compound. A 12B mind asked to reason from
scratch is a 12B mind; a 12B mind handed the already-solved shape of its situation — the
verified answer, the known failure, the executable procedure — is doing an easier task:
**verification and adaptation instead of generation**. Small models are weak at generation and
serviceable at verification; that asymmetry is the whole opportunity.

The claim under test: *a small, wise model — its lived experience stored decision-shaped,
retrieved at the moment of use, and compiled below the model where possible — can match or beat
a naive larger model on its home domain.* §4 builds the instrument that measures exactly this.

Everything here rides the adjudication substrate already built: every rung of wisdom settles on
glue, never self-report. That is what makes accumulated experience *earned* rather than
imagined — and it is the thing most memory-augmented agents cannot do because they have no
ground truth. We have nothing but ground truth.

## §0 The window — 32,768 ctx (prerequisite)

The 16k window forces wisdom to fight the living stream for room. CONTEXT_SPEC phase 2 (32k)
is hereby un-deferred. Two halves, which MUST land as a pair (the coherence rule:
`model n_ctx >= max_total_chars/chars_per_token + response_reserve`):

1. **Serving side (operator runbook, RUNTIME_SPRINTER.md):** raise the llama-swap model entry
   to `-c 32768` with KV-cache quantization (q8_0 KV halves the cost; verify with `nvidia-smi`
   that headroom holds on THIS box's actual GPU before leaving it). Verify which server/entry
   actually serves the mind first — the committed `[llm]` block and the local overlay have
   diverged before (the cmod-s/Sprinter split-brain; ANALYSIS appendix).
2. **Consumer side (config):** a documented 32k budget block for `[context]` /
   `[compaction]` — roughly doubling `max_total_chars` (→ ~76k chars), `obs_max_chars`,
   `memory_max_chars`, `intelligence_max_chars`, and the compaction `token_threshold` — applied
   via config.local.toml ONLY when the served window is confirmed at 32k. The budgets and the
   window move together or not at all.

The new room is SPENT deliberately: roughly half to the living stream (fewer dream-amnesia
cycles), half to wisdom (§3's decision block, richer recall, procedure texts). Not to more
prose for its own sake.

## §W Invariants (binding; each gets a test)

- **WIS1 — Adjudicated-only wisdom.** Every promotion up the ladder (§1), every replay score
  (§2), every curation verdict (§5) settles on glue-checked outcomes. The model's opinion of
  its own wisdom moves nothing.
- **WIS2 — Compiled cognition never farms the economy.** A reflex-handled tick pays NO XP, NO
  portfolio evidence, NO skill-trust movement (same exclusion shape as pitfall #8's
  `delegated` predicate; outcomes carry `"automated": true`). Reflexes free the mind; they
  must never impersonate it.
- **WIS3 — Reflex honesty.** A reflex firing is rendered in the observation stream AS a reflex
  ("[REFLEX] handled X via Y"), visible to creature and operator. A reflex whose action FAILS
  adjudication demotes immediately (disarmed + scarred as an error engram) — crystallized
  wisdom that stops working stops firing.
- **WIS4 — Replay is counterfactual, never causal.** Sleep replay NEVER executes actions; it
  scores the model's answer against RECORDED ground truth only (§2's scoring rules). No tool
  runs during a dream.
- **WIS5 — The platform decides when wisdom speaks.** Injection at the decision point is
  similarity-gated by the platform, budget-bounded, and every injected item carries provenance
  + the instruction to VERIFY transfer, not assume it. A wrong case confidently injected is
  worse than nothing.
- **WIS6 — The instrument never runs itself on the live mind.** The experience-curve harness
  (§4) is operator-invoked, refuses to start unless the eidos loop is paused/stopped, and
  restores the resident model when done (VRAM discipline per RUNTIME_SPRINTER).
- **WIS7 — Flag-dark.** Every behavior change ships behind `[wisdom]` flags, default false,
  flag-off byte-identical, soak-then-flip.
- **WIS8 — Bounded.** Reflex registry, replay logs, curve results: all capped stores, atomic
  writes, fail-open reads.

## §1 The crystallization ladder — compile thinking downward until it's free

**Today's ladder:** episode → lesson/guardrail (prose) → skill (code, model-invoked). Each rung
still costs the model a decision. **The new rung: the REFLEX — a trigger→action rule the
platform executes with no model call.**

- **Representation** (`reflexes.py`, new): a reflex is
  `{id, trigger: {situation_key, guard: Criterion-vocabulary predicate over typed stats},
  action: {tool, args}, provenance: {episode_ids, successes, last_adjudicated}, status:
  proposed | armed | demoted, fired_count, failed_count}`. Triggers key on the SAME
  situation-key machinery episodic memory uses; guards use the SAME `quests.Criterion`
  vocabulary (WIS1 — no new predicate language).
- **Promotion (mechanical):** when the ledger shows the same situation-key answered by the
  same action-signature with ≥ `reflex_promote_successes` (declared: 5) consecutive
  adjudicated successes and zero interleaved failures, the platform writes a PROPOSED reflex.
  Arming is operator-gated v1 (the propose/apply doctrine): proposals surface via a dashboard
  panel (wave-2) or `reflexes.arm(id)`; a `reflex_auto_arm` flag (default false) exists for
  the day the trust ladder earns it.
- **Execution:** in the tick loop, BEFORE context assembly: match armed reflexes against the
  current typed situation; on a guard-true match, execute the action through `execute_tool`
  (the same chokepoint — typed failures, output normalization), record the outcome marked
  `"automated": true` (WIS2), render `[REFLEX]` into the stream (WIS3), and — the payoff —
  **skip the LLM call for that tick entirely** when `reflex_saves_tick` is on (else run the
  model anyway with the reflex result in-stream; the conservative soak mode).
- **Demotion:** one adjudicated failure → status demoted, disarmed, error engram committed
  ("reflex R stopped working in situation S"), eligible for re-proposal only after fresh
  successes. Failure never loops silently.
- **Caps:** ≤ `reflex_max_armed` (declared: 12) armed reflexes; per-tick at most one reflex
  fires; a reflex that fires > N times without the situation changing trips the same loop
  detection the model is subject to (a reflex can rabbit-hole too).
- **The metric:** fraction of ticks resolved below-the-model, per day, alongside outcome
  quality — rising automation with held outcomes IS the system getting smarter per joule.
  Persisted for the growth panel (wave-2).

## §2 Counterfactual replay — deliberate practice during sleep

**The loop that makes memory improve decisions rather than merely accumulate.**

- **Material:** episodes with RECORDED GROUND TRUTH — failure episodes carrying a known
  `fix` (the episodic `{situation → action → outcome → fix}` shape), and closed predictions.
  Only episodes whose fix was later adjudicated to work are replayable (WIS1): we are testing
  whether memory teaches the *verified* answer, not an imagined one.
- **Mechanics** (`replay.py`, new; invoked from the sleep engine as one bounded job): sample K
  (declared: `replay_batch` = 4) replayable episodes, biased toward recent + high-strength +
  never-replayed. For each: reconstruct the decision context AS IT WAS (situation, the same
  recall the creature would get TODAY — that's the point), one LLM call, grammar-constrained
  to the action channel. NO execution (WIS4).
- **Scoring (mechanical, three-way):** the replayed action is compared by action-signature to
  (a) the recorded FAILING action and (b) the recorded verified FIX.
  Matches fix → **learned** (memories recalled into the replay gain strength through the bet
  ledger — they demonstrably teach). Matches the original failure → **unlearned** (the
  guardrail that should have fired is flagged for re-distillation; its strength takes the
  loss). Matches neither → **divergent** (recorded, no settlement — honesty about what we
  can't score).
- **The number D3 has been waiting for:** per-sleep replay report
  `{learned, unlearned, divergent}` appended to a bounded `state/replay_history.jsonl`;
  "wakes up smarter" = the learned-rate trend across sleeps. The growth panel's D3 row reads
  it (wave-2).
- **Budget:** replay costs K LLM calls per sleep — bounded, off the wake path, and skipped
  entirely when the metabolic reserve is low (sleep's existing job-budget discipline).

## §3 The wisdom calling convention — retrieval as answer, not reading material

**Restructure the decision-point injection from documents to decisions.**

- **The block** (rendered by `memory_manager.py`, placed by `context.py` in the volatile
  tail — it is per-tick decision support): `## Before you act` containing at most:
  1. **The case:** the single best situation-matched episode with a verified outcome, rendered
     as what-was-done: "Here before (sim 0.83): ran `X` → worked (fixed in 2 ticks)." —
     action-first, one line, with provenance.
  2. **The guardrail:** the matched strategy engram as an imperative, verbatim.
  3. **The offer:** the top skill affordance as a one-line invocation offer.
  Each item carries its provenance mark; the block closes with the fixed frame: "These are
  YOUR precedents, not orders — verify they transfer before leaning on them." (WIS5).
- **Platform-gated:** the block renders ONLY when best-match similarity ≥
  `wisdom_recall_min_sim` (declared: 0.55); an empty block does not render. Never exceeds
  `wisdom_block_max_chars` (declared: 700 at 16k, 1400 at 32k — read from config so §0's
  budget flip scales it).
- **Replaces, not adds:** the existing prose recall trims by the same chars this block
  consumes — the calling convention is a re-SHAPING of the recall budget, not new spend
  (until §0 lands; then both grow).
- **Settlement:** items injected here are bets (the existing recall-bet machinery) — the
  calling convention gives the bet ledger *denser, better-targeted* settlement data, which §5
  consumes.

## §4 The experience curve — the instrument

**Three arms, one frozen battery, weeks of creature life. The project's central claim becomes
a plotted curve.**

- **The battery** (`wisdom_curve.py`, new; fixtures under `exam_battery/`): ~20 frozen
  house-domain tasks in the exam.py mold — each a prompt + a MECHANICAL scorer (glue
  vocabulary: exact answer, checkable claim, action-signature match). Domains: this machine's
  ops, the creature's own subsystems, recall-dependent situational calls, procedure
  recollection. Versioned; a battery, once published to results, is IMMUTABLE (score
  comparability) — additions create battery v2.
- **The arms:** (a) **naive-12b** — the resident model, EMPTY workspace fixture;
  (b) **wise-12b** — the resident model + the LIVE creature's real memory (read-only copy),
  recall + §3 block active; (c) **naive-27b** — `qwen27b` by llama-swap name, empty fixture.
  Same battery, same sampler discipline, same grammar.
- **Discipline (WIS6):** operator-invoked
  (`PYTHONUTF8=1 .venv/bin/python wisdom_curve.py --run`), REFUSES unless the eidos loop is
  paused/stopped (checks the control API / pidfile), runs arms serially (llama-swap swaps
  models; 27b evicts gemma — the run ends by touching the resident model back in), writes
  `state/wisdom_curve.jsonl` (bounded), prints the three scores.
- **The curve:** score-vs-creature-age per arm. The hypothesis is falsifiable: if wise-12b
  never separates from naive-12b, wisdom isn't landing (fix §3/§5); if it separates but never
  approaches naive-27b, we've measured the ceiling of context-compounding at 12B — also a
  result. Crossing = the headline.

## §5 Utility-grounded curation — compounding dies without it

**The garden lesson, applied to memory: a store that grows by similarity and volume compounds
noise. Strength must track demonstrated decision-improvement.**

- **The signal:** §2's replay settlements and §3's dense recall bets give every load-bearing
  memory a genuine utility record: recalled-and-followed-into-success, recalled-and-ignored,
  recalled-into-failure, taught-in-replay, failed-to-teach.
- **The rule (extends the existing SHY prune in the sleep engine):** a memory whose utility
  record is empty-or-negative across `curation_grace_sleeps` (declared: 10) sleeps decays at
  an accelerated rate REGARDLESS of recall frequency (being surfaced a lot and never helping
  is the definition of noise — the garden failure mode); a memory with positive replay/bet
  utility gains the same protection scars get today. Never delete outright below the floor —
  demote to the archive tier (supersede-not-delete discipline).
- **Provenance-aware:** `inherited` memories (§6) that fail to earn utility within their grace
  window decay FASTER (an inherited claim this creature can't verify is a rumor); experienced
  scars keep their existing slow-decay privilege.
- **Bounded honesty:** curation writes a one-line sleep report ("curation: 3 demoted, 2
  reinforced, store 4,812") into the dream log — visible, never silent.

## §6 Lineage — the species gets smarter when the individual resets

**Each fresh slate currently resets wisdom to hand-curated nuggets. Make retirement a
publication event.**

- **The exporter** (`legacy.py`, new; invoked by `scripts/fresh_slate.sh` before the wipe):
  distill the retiring creature's REPLAY-VALIDATED corpus — strategy guardrails with positive
  utility, procedures, high-utility facts, the reflex registry with its provenance — into a
  versioned `heirlooms/<creature-name>-<date>.jsonl` (repo-side, committed; the creature
  lineage's bookshelf).
- **The import** (extends `seed_knowledge.py`): a newborn seeds from `preserved_nuggets.toml`
  (Charlie's letter) PLUS the latest heirloom volume(s) — every item stamped provenance
  `inherited`, strength-discounted (the existing told-engram discount), and subject to §5's
  faster-decay-until-verified rule. Inherited reflexes arrive DISARMED as proposals — a new
  body must re-earn its own automation (WIS1 across generations).
- **The world hook (wave-2):** heirloom volumes render as books in the library district —
  inheritance the creature can literally walk into, titled by ancestor.
- **The generational metric:** time-to-level-2, time-to-first-confirmed-commission, and the
  §4 wise-12b score at age N, plotted per generation. The species curve above the individual
  curves.

## §7 Build order & ownership (5 parallel workstreams + operator runbook)

| Stream | Owner files (disjoint) | Depends on |
|---|---|---|
| A. Reflexes (§1) | `reflexes.py`, `eidos.py` (pre-LLM hook), tests | flags (pre-landed) |
| B. Replay + curation (§2+§5) | `replay.py`, `compaction.py` (sleep job), `bets.py`/`engram.py` (settlement+decay), tests | flags |
| C. Calling convention (§3) | `memory_manager.py`, `context.py`, tests | flags |
| D. Experience curve (§4) | `wisdom_curve.py`, `exam_battery/`, tests | nothing (operator-run) |
| E. Lineage (§6) | `legacy.py`, `seed_knowledge.py`, `scripts/fresh_slate.sh`, tests | flags |
| §0 window | RUNTIME_SPRINTER runbook + config.local snippet | operator applies |

Wave-2 (after soak): dashboard panels (reflex approvals, replay/curation reports, the curve
chart), growth-panel D3/automation rows, world-library heirlooms, `reflex_auto_arm` trust
ladder.

All `[wisdom]` flags are PRE-LANDED in config by the orchestrator before the streams fork —
no stream touches `config.py`/`config.toml`.
