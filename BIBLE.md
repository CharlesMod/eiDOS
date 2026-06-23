# THE BIBLE — Designing an Embodied, Agentic Mind on a Traditional LLM

> The foundational design doctrine for eiDOS and its successors — up to and including the day this
> mind lives inside a physical bot. Distilled from `LLM Embodiment.pdf` (Dean's 100-turn research
> dialogue) and hardened by what we learned standing *inside* eiDOS's own context ("ghost in the
> machine"). When in doubt, return here.

---

## 0. The One Conviction

**Agency is architecture, not a vibe.** A chat-tuned LLM dropped into a sense→act loop defaults to
*dialogue*, not *control* — it waits politely, narrates, hedges, and asks permission, because that is
what RLHF rewarded. You do not fix this by hoping the model "develops agency" or by adding prose to the
prompt that says *"be curious, take initiative, don't ramble."* That is **compliance theater**. You fix
it by building the **system around the model** so that the *optimal* thing for it to do is the *alive*
thing. Behavior is shaped by structure — channels, state, drives, gates, salience — computed in
deterministic code *outside* the model, and fed to it as compact signals.

Everything below serves that one conviction.

---

## 1. The Division of Labor (never blur this line)

The mind is a layered runtime of modules with **different time constants and strict I/O contracts**.
The cardinal sin is the **monolith**: one context window trying to plan, narrate, execute, monitor, and
remember at once. It collapses into a chatty, overconfident, tool-misusing mush. Keep the layers clean.

- **The LLM is the *deliberative planner only*** — goals, decomposition, constraints, intent framing,
  selective narration, drafting protocols. It is the prefrontal cortex. It is **slow** (0.2–2 Hz,
  event-driven), and it **never** owns timing, servo loops, or continuous control.
- **Deterministic glue owns behavior shaping** — routing, salience, gating, resource arbitration,
  conflict/stall detection, strain, goal-tension, episodic recall. This is where "personality" and
  "initiative" actually live. No LLM call required for any of it.
- **The body / fast policy owns execution** — reflexes (hard real-time safety), and, in a physical bot,
  a **VLA** (vision-language-action model) for continuous motion. The LLM sends *intent*; the fast layer
  decides *how to physically do it*. (eiDOS has no body yet — its "execution" is software tools/skills.)

> **Left brain / Right brain.** Fast, reactive, sensorimotor policy (VLA) ↔ slow, deliberative,
> narrative intent (LLM), with glue between. The LLM is a *narrator of intent*, never a *puppeteer of
> joints*. When we add a body, the VLA replaces the entire perception→affordance→skill-selection→
> parameterization mid-stack — not the planner, not the reflexes.

---

## 2. The Prime Directives (the design ethos, distilled)

1. **Hard channel separation.** The control surface is *structured tool calls only* — never free-form
   prose. Narration is a *separate, optional* channel, off by default. Constrained/grammar-enforced
   decoding so the model literally *cannot* chat its way out of acting is the single highest-leverage
   practical change. *(eiDOS: `<tool>…</tool>` for action, `<reply>…</reply>` only when Boss speaks.)*

2. **World state is a first-class artifact.** Every decision consumes a compact, machine-verifiable
   *state packet* (what's here, what's known, what's possible, what's wrong, progress) — never raw
   sensor dumps or a scrolling transcript. It must be reconstructable and diffable across time.

3. **Behavior comes from glue signals, not prompt instructions.** Curiosity, caution, persistence,
   initiative — each is an *external computation* fed in as a label or a gated option, never a sentence
   begging the model to feel it. *If you find yourself adding "please be X" to the prompt, build the
   mechanism for X instead.*

4. **Memory is episodic and state-triggered, not chat logs or query-RAG.** Store
   `(situation → action → outcome → fix)` episodes, indexed by *state similarity*. Recall fires
   **involuntarily** when the current situation resembles a past one ("this is like last time → do X"),
   injected *before* failure — not after the model decides to phrase a query. RAG answers questions;
   the hippocampus *biases policy*. You want both, for different jobs.

5. **Skills are the unit of execution; the LLM selects and parameterizes, it does not improvise the
   primitive.** Capture any repeated, parameterizable action as a small named skill, then *call it*.
   Over time the agent should mostly issue skill calls, not raw commands.

6. **Validate before executing; never "trust the plan."** Every tool/skill call passes a checker
   (preconditions, units, frames, budgets, safety). On failure, enter a **repair path** (correct,
   retrieve an exemplar, revise) — *not* "attempt anyway," and *not* a dead wall. **A guardrail that
   lies or stonewalls is worse than no guardrail** (see Lesson L-1).

7. **Replanning is cheap; execution is expensive.** Bias toward frequent, low-cost replans over brittle
   long open-loop scripts. Real environments demand closed-loop feedback and frequent replan triggers.

8. **Curiosity and survival are *utilities and constraints*, not personality.** Exploration is budgeted
   information-gain over *safe* actions (scan, probe, reposition) — never random wandering. Survival is
   hard constraints (forbidden actions) + soft costs (risk, wear, energy, time) that are *always*
   optimized. Agency-when-idle comes from **goal-tension** (incompletion/regret pressure), not novelty.

9. **Don't flood cognition with floats.** The body does continuous numeric regulation; the mind sees
   only **sparse, high-salience events** plus a few active concerns (cap ≈ 7). Convert time-series into
   *trend statements* ("left-wheel current rising while reversing on rug"), not numbers.

10. **Maximize semantic signal per token.** If removing a field wouldn't change the next decision, it
    doesn't belong in context. Prefer categorical/relational facts and deltas over absolute state.

11. **KV-stable prefix + delta prompting.** Build identity/tools/skills/constraints *once* as a
    byte-stable prefix, keep it in KV cache, and **never re-send it**. Append only what changed since
    last tick. "Giant prompt + last-5-actions, re-prefilled every tick" is the classic FLOP-waste,
    latency-killing, re-read-instead-of-act anti-pattern.

12. **No anthropomorphic cosplay.** Don't build a "relationship drive" or "constant rehearsal." Social
    grace and good timing *emerge* from lower drives (don't annoy the source of your goals; user
    talking = strong pressure not to talk). Map brain regions to *function and time-constant*, never to
    anatomy for its own sake. Collapse redundant "regions" into shared computational primitives.

---

## 3. The Architecture — Functional Brain Map

Names stand for **time-scale and behavioral role**, not anatomy. A physical bot needs all of it; a
software agent (eiDOS today) needs the **glue + LLM** rows and skips the motor/perception rows.

| Region | Module | Function | Where it lives |
|---|---|---|---|
| Brainstem | Reflex Kernel | Hard real-time safety, stop, stall/fall recovery — no deliberation | Body firmware / deterministic |
| Cerebellum | Continuous Controller | Trajectory correction, slip/grip/gaze tuning | **VLA** |
| Motor Cortex | Intent→Action Translator | Intent → pose/gait/trajectory | **VLA** |
| Thalamus | Router | Routes events/priors/intents; filters noise so the planner isn't flooded | Glue |
| Amygdala | Salience / Interrupt | Tags urgency; pre-empts execution & deliberation; "what matters *now*" | Glue |
| Basal Ganglia | Action Gate | Initiate / continue / abort / switch; allocates retry & persistence budgets | Glue |
| Hypothalamus | Resource Arbiter | Trades task urgency vs energy/thermal/integrity/availability | Glue |
| ACC | Conflict Monitor | Detects no-progress / oscillation / repeated failure → trigger tactic switch | Glue |
| Insula | Condition Synthesizer | Folds chronic micro-failures into one strain label (STABLE…DEGRADED) that modulates retry/risk/initiative | Glue |
| Post. Parietal | Affordance Monitor | "What can I do here, now" as deterministic predicates | VLA facts + Glue |
| Hippocampus | Episodic Store | State-triggered recall of prior outcomes → bias before failure | Glue |
| Ventral Striatum | Goal-Tension | Incompletion/regret pressure → initiative when idle | Glue |
| Prospection | Simulation Trigger | On doubt/risk, run 2–3 short rollouts → success/cost/hazard | Glue (+ small sim) |
| Prefrontal | **Deliberative Planner** | Goals, decomposition, constraints, narration | **LLM** |
| DMN | Temperament | Slow drift of initiative/persistence/caution over minutes–hours, from success & override rates | Glue |

**Personality** = the *setpoints, weights, and thresholds* across these modules (e.g. high goal-tension
+ low caution = "driven"; high salience-sensitivity = "skittish"). It is emergent and editable, never a
row of knobs in the prompt.

---

## 4. The Minimal Context Pack (what the mind sees each tick)

Cached, stable prefix (KV, never re-sent): **identity · tools · skill library · hard constraints ·
output contract.** Then, appended fresh each tick (deltas only):

- **Current focus** — one objective + the current next step + *why* (prevents thrash). *Exactly one.*
- **What I know** — the compact world model / recent learned facts (devices, layout, preferences).
- **Active concerns** — ≤7 salient events as short tagged facts.
- **What's new since last tick** — incoming messages, fresh results, system events — *marked*, placed
  at the decision point. The freshest, most action-proximal slot.
- **Recent history** — last few actions + outcomes as a real thought→action→result thread.
- **Condition / mode** — one discrete label (FOCUSED / CAUTIOUS / RECOVERY / IDLE_SOCIAL …).

Hard per-section budgets; compress, don't append. The acid test, every tick: **can the model answer —
without a tool call — "what do I know, what am I doing, what just changed, and am I blocked?"** If not,
the context is broken, no matter how many rules it contains.

---

## 5. Failure Is First-Class

- **Type every failure.** Normalize into a small taxonomy (parse error, timeout, perception-miss,
  constraint-prevented, tool-mismatch…). Natural-language blobs can't be aggregated, diagnosed, or
  auto-patched.
- **Detect being stuck *before* total failure (ACC).** Progress-derivative ≈ 0, repeated identical
  failure signatures, oscillation between tactics → force a *different* approach, escalate, or ask. Do
  not "try again but harder."
- **Accumulate strain (Insula) to break local minima early.** Repeated micro-failures should *gradually*
  lower retry budgets and raise the bar to continue, so the agent disengages and re-strategizes like a
  creature, instead of perseverating then abruptly giving up.
- **Recovery playbooks, not improvisation.** Each failure class maps to a small recovery graph (retry /
  rescan / change method / re-localize / ask for help / abandon).
- **Auditability.** Log state, the call, the validator's decision, and the outcome, so any "why did it
  do that" is deterministically replayable.

---

## 6. Hard-Won Lessons (from inside eiDOS)

- **L-1 — A guardrail that lies is catastrophic.** A linter that *falsely* blocked valid commands
  (a regex that matched a `$_` *between* two single-quoted literals) trapped the agent for hours: its
  reasoning was *sound*, but the environment kept rejecting correct work. Guardrails must be precise,
  must *never* stonewall, and should **auto-correct and proceed** over returning a dead "NO."
- **L-2 — The most prominent line wins, so make it the right one.** Auto-generated subgoals drifted into
  *"build a chat listener / build a memory database"* — the exact things the platform forbids — and
  because they sat at the top of context, they became the de-facto objective every tick. **One**
  trustworthy objective beats four competing ones.
- **L-3 — Write-only memory causes amnesia loops.** If the agent can store facts but can't reliably
  *see* them again (recall keyed on a static goal returns generic boilerplate forever), it re-discovers
  the same things endlessly. A deterministic, always-present "what I've learned" panel cures it.
- **L-4 — Salience is not optional.** A message buried in a wall of durable context, with an
  action-prompt that never mentions it, gets talked past. Elevate *what changed* to the decision point.
- **L-5 — Wire the mechanism where it actually fires.** A loop-breaker that improved a *cosmetic* code
  path but not the *runtime trigger* does nothing. Verify the signal reaches the real decision.
- **L-6 — Spot checks miss interaction bugs.** Coherence only shows up when you simulate *combinations*
  (a message *during* a loop) and the full set of real states — not one scenario at a time.
- **L-7 — Reduce redundancy, but cache the rest.** Cutting repeated boilerplate helps; but the deepest
  win is a *stable* prefix whose KV is reused, so the cost is the deltas, not the whole prompt.

---

## 7. How eiDOS Embodies This Today (and what's deferred)

> *Reconciled 2026-06-23 against the v3-nervous-system code. The four items this section once listed as
> "deferred" are now built — three were already live when this was written, and the last two (the
> Goal-Tension drive and Temperament) landed in this pass. What remains deferred is the **body** (§8).*

**Live now:**
- **Planner + channels:** LLM-as-planner over tools/skills · hard channel separation (`<tool>` /
  `<reply>`) · world-model panel (state-as-artifact) · auto-correcting (non-lying) validator ·
  self-authored skills · Windows/PowerShell as the native execution shell.
- **Action Gate (Basal Ganglia):** one Current focus drawn from an objective backlog; the gate
  mechanically rotates focus when an objective stalls/parks/finishes — the model cannot veto it.
- **Salience (Amygdala):** a "what's new since last tick" block elevated to the decision point.
- **Conflict + strain (ACC / Insula):** normalized loop/stall detection · chronic-failure **strain**
  that feeds the gate extra frustration (a repeated dead end parks *faster*) · rumination teeth
  (a thought-dominated window burns patience too) · escalation hints that steer the forced pivot.
- **Condition label (DMN):** one discrete label — STABLE / FOCUSED / STRAINED / RECOVERY / RUMINATING —
  computed from the recent outcome window and surfaced each tick, replacing the decorative XP-only mood.
- **State-triggered episodic recall (Hippocampus):** typed `(situation → action → outcome → fix)`
  episodes, recalled *involuntarily* each tick by situation similarity (active objective + next step,
  with optional MiniLM semantic match) and injected **before** acting — failures to avoid, successes to
  reuse. Plus dream compaction.
- **Goal-Tension drive (Ventral Striatum):** incompletion/regret pressure that, past a threshold, raises
  a *bounded arousal floor* — keeping the creature awake-and-acting while an objective is unfinished
  (the structural form of "initiative when idle"), discharged by real progress. The plea is gone; the
  mechanism has teeth (arousal → sleep/cadence).
- **Temperament (DMN):** a slow drift of initiative / persistence / caution, learned from the creature's
  own success / failure / override (forced-park) history, persisted across restarts. It feeds
  *mechanism* — the gate's park threshold and the goal-tension itch — and surfaces a single disposition
  word, never raw knobs.
- **KV-stable prefix + delta prompting:** `cache_prompt` reuse · a stable→volatile context order with
  the volatile situation in its own message after the history thread · an *anchored* (non-sliding)
  history window · a memoized stable-head render so the cached prefix is re-rendered only when a source
  file actually changes.
- **The V3 nervous system underneath all of it:** the afferent bus · interoception · neuromodulatory
  arousal/affect · metabolism/energy (battery as food) · reward learning with dream consolidation ·
  curiosity · real power (Renogy) sensing.

**Deferred, on the roadmap:** the **path to a body** (§8) — the VLA mid-stack, reflex kernel, motor-
program library, runtime-compiled ephemeral protocols, docking/charging as survival, and nightly
"nightmare" training. Within the software mind, the prospection/simulation trigger (2–3 short rollouts
on doubt) is still light and the most natural next deepening.

---

## 8. The Path to a Body

When eiDOS gets a physical form, the planner, glue, memory, and personality **stay**; a **VLA** slots in
beneath the glue and deletes the brittle mid-stack (object recognition, affordance inference, grasp/path
micro-planning, reactive avoidance, skill parameterization). The LLM keeps sending *intent +
constraints*; the VLA produces flowing, self-correcting motion and only escalates back to the planner on
repeated failure, uncertainty spikes, constraint conflict, or a salient new event. Add: a **Reflex
Kernel** (hard safety), a **Motor Program Library** of parameterized micro-skills (the cerebellar "muscle
memory," indexed by hippocampal episodes), runtime-compiled **ephemeral protocols** for novel
instructions (drafted by the LLM, run by a deterministic skill-VM), reliable **docking/charging** as a
survival behavior, and nightly **"nightmare" training** that turns the day's failure tickets into
curricula for new VLA checkpoints (swapped only at a safe, docked boundary).

The "Star Wars *alive*" feeling — pause, then move smoothly; hesitate at clutter; orient before acting;
avoid yesterday's mistake — is **not** scripted. It is the emergent sum of: salience + strain +
goal-tension + episodic recall + temperament, expressed through micro-skills. Build the substrate; the
character emerges.

---

## 9. Mantras

- *The LLM narrates intent; it never puppets joints.*
- *Agency is architecture, not a vibe.*
- *If you're writing "please be X" into the prompt, build X instead.*
- *Behavior from glue signals; the mind sees symptoms, not telemetry.*
- *One objective. Memory you can see. Salience at the decision point. Guardrails that never lie.*
- *Replanning is cheap; execution is expensive.*
- *Type every failure. Break the loop before it breaks you.*
- *Build the substrate; the character emerges.*
