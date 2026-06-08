# eiDOS context-model redesign — empower the AI at its core

Driven by the ghost-in-the-machine sessions. The model isn't weak; the *context it's handed each tick*
is incoherent: it over-supplies static identity/rules and under-supplies live state, sometimes lies, and
points at four conflicting "current tasks." This redesign fixes the **cognitive substrate**, not symptoms.

Guiding principle: **every tick, the model should be able to answer — without a tool call — "what do I
know, what am I doing, what just changed, and is anything blocked?"** Today it can answer none of those
from what's in front of it.

---

## Point 1 — Cure write-only memory: a persistent World-State panel

**Problem (measured):** `_build_intelligence_section` runs BM25 with the *static goal text* as the query,
so the "What you already know" panel returns generic bootstrap nuggets every tick and **never** the
devices the agent discovered. It can store facts but can't see them again → it re-scans → the loop has
amnesia. (Also: 265 entries, heavy dupes, recall is noise.)

**Better architecture:**
- Distinguish **seed** knowledge (bootstrap, rarely needs surfacing) from **learned** knowledge (the
  agent's discoveries). Mark seeds explicitly (`source_goal="seed"`); everything memorized at runtime is
  "learned."
- New deterministic **`## What you've learned (your world model)`** panel: the N most-recent *learned*
  facts, deduped, newest-first, rendered verbatim — ALWAYS present, never BM25-gated. This is the agent's
  always-visible map of the house (devices, IPs, roles, ports, Boss facts).
- Keep a small secondary **`## Possibly relevant`** BM25 slice, but query it with the agent's CURRENT
  STEP (its `update_plan` next-action), not the static goal — so it surfaces step-relevant older/seed facts.
- Dedup: render-time dedup now (safe); store-time near-dup rejection in the redundancy pass.

**Verify (ghost):** after memorizing 5 device facts, the panel shows those 5 devices verbatim, not
bootstrap nuggets — even when the raw scan has scrolled out of the history thread.

## Point 2 — One trustworthy objective (kill the drift)

**Problem (measured):** four conflicting "current task" sources — presence's "Right now you are working
on:" (drifted auto-subgoal: *build a chat listener*), `## Plan`, mission `Immediate focus`, and the actual
history. The loudest (drifted subgoal) tells it to build the exact thing the platform forbids. The drift
comes from `plan_goal` auto-generating platform-contradicting subgoals (eidos.py:568).

**Better architecture:**
- ONE **`## Current focus`** block = {objective · next step · done-when}, the single anchor, derived from
  goal.md's immediate-focus + the agent's own `update_plan` next step. Everything else (history, world
  model) is context *for* it.
- Stop auto-generating drifting subgoals: either disable the `plan_goal` subgoal generator or constrain it
  with the platform's "never build agent infrastructure" rules so it can't emit chat-listener/memory-DB
  goals. Remove the prominent drifted-subtask line; remove the `## Subgoals` durable block.
- Reconcile self-guide ⇄ platform: delete the self-guide line that says "build the infrastructure the next
  phase needs (memory profiles, skill schemas, device registry)" — it directly contradicts check_system.
- goal.md immediate-focus must not assert false prior progress ("you have found the network") to a fresh
  Lv.0 newborn; phrase it state-neutrally or derive from world-state.

**Verify (ghost):** top-of-context shows ONE coherent objective consistent with mission + platform; no
"chat listener" anywhere; a newborn isn't told it already mapped the LAN.

## Point 3 — A salience / "what's new" channel

**Problem (measured):** Boss's message lands at the BOTTOM of a 6 KB durable blob, and the tick prompt that
actually directs action never mentions a message arrived → easy to talk past Boss. Async results, Boss
messages, and system events all arrive at flat priority. Nothing says "THIS changed since last tick."

**Better architecture:**
- A **`## New since last tick`** block placed immediately BEFORE the tick prompt (the freshest, most
  action-proximal slot): new Boss message(s) verbatim + "answer in <reply> this tick", async results that
  just landed, and system events — each tagged NEW.
- Tick prompt branches explicitly: *if Boss just messaged → reply first; else advance Current focus.*
- Boss messages also get a high, not buried, placement in the durable section.

**Verify (ghost):** in the Boss-message replay, the question appears in a NEW block right above the tick
prompt, and the tick prompt instructs answering it.

## Point 4 — Guardrails that don't lie + cut redundancy

**Problem (measured):** the linter false-blocked valid commands for hours (regex matched `$_` across quote
boundaries); presence "Still running" can contradict a delivered result; the system prompt + recalled
nuggets repeat "memorize is your DB / use PowerShell" 2-3×.

**Better architecture:**
- **Linter quote-aware + non-lying contract:** tokenize the command into quoted/unquoted spans; only flag
  `$_`/`${` genuinely INSIDE a single-quoted span. For that narrow real case, AUTO-CORRECT to double quotes
  and RUN, telling the model what was fixed — never a bare "NOT RUN" wall. Hard-block only the unambiguous
  cases (`for…do…done`, `powershell -Command "…nested…"`).
- **Dedup the knowledge store** (kills the duplicate powershell_syntax nuggets) + stop recalling the
  bootstrap PowerShell nugget every tick (it's already in the system prompt).
- **Trim the system prompt** to the essentials; move how-to detail behind `check_system` (already on demand).
- **Fix presence/history desync:** never list a job as "Still running" once its async_result is delivered.

**Verify (ghost + unit):** the tick-969 command passes; each fact appears once; no duplicated PS advice;
a delivered job is not also "still running."

---

## Build order (each step ghost-tested before the next; 1-hour in-situ run at the end)
1. ✅ DONE — Linter quote-aware + auto-correct (removes the active lie). Unit-tested 8/8.
2. ✅ DONE — World-model panel + seed/learned split (P1). Ghost: panel shows the 4 learned facts, 0 seeds.
3. ✅ DONE — Current-focus collapse + kill drift + self-guide/goal fixes (P2). Ghost: one objective, no Subgoals, no "chat listener".
4. ✅ DONE — "New since last tick" salience block + tick-prompt branch (P3). Ghost: Boss msg elevated + reply-first.
5. ✅ DONE (partial) — knowledge store-time dedup (kills the 265-entry bloat at source). Full tick ~3310 tok (−30%).
   ⏭ DEFERRED — KV-stable prefix reorder (move volatile time/tick/presence to the bottom so the stable
   prefix — system+self-guide+mission+skills — is byte-identical across ticks and its KV is reused). Bigger
   blast radius; do carefully AFTER observing the run. Verify llama.cpp prompt caching is on for house-ai.
6. ⏭ Wipe → reseed (new scheme) → 1-hour supervised in-situ run; observe via the hourly check-in.

## Still-divergent from the doc (structural-agency items — bigger, do after the run validates 1-5)
- **Goal-Tension (Ventral Striatum):** replace the prompt-instruction "have an inner life when idle" with a
  glue-computed incompletion/regret pressure that nominates the highest-tension task when idle.
- **ACC teeth + Insula strain:** the loop-breaker is still partly advisory; make repeated-failure accumulate a
  strain signal that *mechanically* lowers the retry budget / forces a method switch (not a prose plea).
- **Condition/temperament label** (STABLE/STRAINED/RECOVERY/FOCUSED) from recent success/failure, replacing
  XP-only mood. These are the doc's "behavior from glue signals, not prompt text" — the deepest non-divergence.

---

## Anchor: alignment with the "LLM Embodiment" design doc (Boss's north star)

Re-read `LLM Embodiment.pdf` (Boss's 100-turn research dialogue). Its thesis IS this redesign's thesis:
*a chat-tuned LLM dropped in a control loop defaults to dialogue, not action; the fix is **system
architecture**, not hoping the model "develops agency."* The LLM is only the **Prefrontal Cortex /
Deliberative Planner** (goals, decomposition, constraints, selective narration). Everything that shapes
behavior — salience, gating, conflict-monitoring, strain, episodic recall, goal-tension, temperament —
lives in deterministic **glue OUTSIDE the LLM**, feeding compact symbolic signals into a **minimal,
KV-stable context**. eiDOS is the "right brain" only (no VLA/body); its skills are software tools.

**The doc VALIDATES the 4 points (we have not diverged on the core):**
- P1 World-state panel ↔ doc's *"world state is a first-class artifact… a compact state packet, not raw
  sensor dumps"* + the *"compiled Episode Digest / what I've learned"*.
- P2 One objective ↔ doc's *"CurrentGoal (1 short object) + current subgoal + why-it-exists in one line
  (prevents thrash)"*.
- P3 Salience / what's new ↔ doc's **Amygdala** *"salient deltas since last step / Active Concerns (≤7)"*.
- P4a Linter that auto-corrects ↔ doc's *"tool calls validated before execution; on failure enter a repair
  path, not attempt anyway"* + the *"type-checker that auto-rewrites common failure patterns"*. (Our
  auto-rewriting linter IS this.)

**Where the doc says go FURTHER — fold in so we don't diverge:**
- **A. Stable KV-cached prefix + delta prompting** (the doc's single biggest efficiency lever). eiDOS
  badly violates it: it rebuilds the full 8.6 KB system prompt + entire durable blob EVERY tick. Make the
  identity/tools/skills/constraints a **byte-stable prefix that never changes across ticks** (so llama.cpp
  prefix-cache hits) and append **only deltas**. This elevates P4's "reduce redundancy" from cleanup to
  architecture. *(Verify llama.cpp prompt caching is on.)*
- **B. Agency & anti-rumination must be STRUCTURAL, not prompt instructions** — the doc's central warning:
  *"agency is not a vibe; RLHF rewards compliance theater."* eiDOS currently pleads in prose ("be curious",
  "act don't wait", "don't ruminate"). Replace with glue signals:
  - **Goal-Tension (Ventral Striatum):** track open commitments + regret; when idle, surface the
    highest-tension item — instead of "have an inner life when idle."
  - **Conflict Monitor (ACC) + Condition/Strain (Insula):** stagnation / repeated-failure / no-progress →
    accumulate "strain" that *mechanically* lowers the retry budget and forces a tactic switch — instead of
    an advisory "you seem stuck" line the model ignores (exactly what we watched fail at tick 969).
- **C. Memory → state-triggered episodic recall, not query-BM25.** P1's deterministic recent-learned panel
  is the right first step; the doc's end-state is recall *triggered by the current situation* ("this is like
  last time → do X"), injected unasked — not BM25 over the static goal (the bug we found).
- **D. Discrete condition/temperament labels** (STABLE/STRAINED/RECOVERY/FOCUSED) computed from recent
  success/failure, replacing the XP-only persona mood with something behaviorally load-bearing.

**Necessary divergences (eiDOS ≠ navigating robot, per Boss):** no VLA/cerebellum/motor/affordance-field/
prospection-rollout/docking/nightmare-training. Skip heavy RAG and anthropomorphic "relationship/curiosity"
drives (Boss cut both). The relevant brain-map for eiDOS = **Prefrontal (LLM) + cognitive glue**: Thalamus
(context routing), Amygdala (salience), ACC (loop-break), Insula (strain), Hippocampus (episodic recall),
Ventral Striatum (goal tension), DMN (temperament).

**North star:** eiDOS = a deliberative planner wrapped in deterministic glue that computes salience, strain,
tension, and recall, and feeds **compact symbolic signals** into a **minimal, KV-stable** context. Every
redesign decision should move toward that and away from "more prose rules in a rebuilt-every-tick prompt."
