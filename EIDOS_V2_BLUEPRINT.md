# eiDOS v2 — the cohesion blueprint

> Synthesis of the 2026-06-09 full-system audit (6 subsystem deep-maps + live-run forensics),
> judged against BIBLE.md and ARCHITECTURE_PRINCIPLES.md. Status: **PROPOSAL — Dean has not
> approved anything here.** Nothing in this doc has been built.

---

## 0. The verdict

Dean's instinct ("functional, but patchwork") is correct and precisely diagnosable. The good
news: **the elegant architecture already exists — it's BIBLE.md.** v1 is a partial, patch-accreted
implementation of it. v2 is not a re-imagining; it is *implementing the doctrine faithfully* and
deleting two generations of vestiges (Pi-era Kairos, pre-briefing eiDOS) that run alongside the
live system.

The audit's single clearest pattern: **detection is deterministic, consequences are prose.**
Loop detection, tension, stalls — all correctly computed in glue, then *delivered as pleading
text the model can ignore*. Exactly one mechanism in the whole system is doctrine-complete
(the objectives rotation gate: external counter → threshold action the model cannot veto), and
exactly one transport is event-driven (the GPU speech-gate). Those two are the templates for
everything v2 builds.

Live economics (152-tick sample, 2026-06-09): main tick ≈ 2,400 prompt tokens **fully
re-prefilled every tick**, ~130–150 completion tokens, ~4 s median LLM time, ~39 % of ticks
pure thought. The mind spends most of its FLOPs re-reading what didn't change.

---

## 1. The patchwork, named (audit findings, condensed)

1. **Two generations alive everywhere.** `_assemble_legacy` vs `_assemble_briefing`;
   `SYSTEM_PROMPT` ("you are on a Raspberry Pi") vs `SYSTEM_PROMPT_BRIEFING`; `memory.md` vs
   `plan.md`; `compact()` vs `compact_briefing()`; `/api/pause` toggle vs `/api/control/pause`;
   legacy WAV speech endpoints vs SSE streaming; Linux safety probes (`pkill`, `/proc/stat`,
   `/sys/class/thermal`, `who`) running as no-ops on Windows. Every fix pays a parity tax or
   silently applies to only one twin.
2. **A file-sentinel polling nervous system.** Census: pause = file poll @5s; listening hold =
   file poll @2s; interventions = dir scan @≤2s; async jobs = `tasklist` subprocess **per job per
   tick** (exit codes lost — failing async commands report success, tools.py:1096); watchdog =
   5s poll though it *holds the child's waitable Popen handle*; browser = six independent poll
   loops. One channel is event-driven: `gpu_gate` → `/api/gpu/wait`. ARCH #1 is violated by
   nearly every channel the system owns both ends of.
3. **Unconstrained output channel.** ~85 % of parser.py (210/248 lines) is fallback cascade for
   sloppy free-text tool calls; malformed calls burn whole ticks on corrective prose. llama.cpp
   GBNF support is one request field away (llm.py already builds the raw payload) and BIBLE §2.1
   calls grammar-enforced decoding "the single highest-leverage practical change." Never done.
4. **KV-stability attempted, then defeated.** The stable→volatile reorder (e7c51c0) put the
   rotation banner, escalation note, and per-tick frustration gauge at the **top** of the durable
   message — any tension change invalidates the KV of everything below, precisely when ticks are
   most frequent. The deferred CONTEXT_REDESIGN item A was never finished.
5. **Memory: 10 live surfaces + 4 dead ones, and the BIBLE's centerpiece missing.** Live: goal,
   plan, self-guide, knowledge panel, step-recall, notebook, objectives, observations-as-thread,
   thoughts, conversation. Dead/write-only: `memory.md` (the briefing prompt still advertises
   `remember` — a live L-3 violation: the model can write what it can never see again),
   `subgoals.md`, the entire embedding stack, dream-journal. The episodic store
   (situation→action→outcome→fix, state-triggered recall) exists nowhere — its raw material is
   shredded across observations (truncated every dream), thoughts (unbounded, never recalled),
   the errors category (free prose), and dream records (write-only).
6. **Failures untyped.** `ToolResult` = `{output:str, success:bool}`; observations carry prose
   pseudo-types. No taxonomy → no aggregation → no recovery playbooks → every loop-breaker must
   re-derive "is this the same failure?" from text. BIBLE §5 violated end to end.
7. **Two monoliths.** `run_loop` = ~690 lines, ~30 interleaved concerns, gate order encoded only
   positionally. `dashboard.py` = 3,307 lines = four programs (supervisor/trust boundary, voice
   service, GPU arbiter, web UI — 53 % is one inline HTML string), with bolt-on stratigraphy and
   a module docstring that still claims it's a read-only viewer.
8. **State authority scattered.** Cross-process unlocked writes: dashboard appends into
   eidos-owned `observations.jsonl` and the knowledge store behind the agent's back; `jobs.json`
   is read-modify-written by both processes with no lock. Magic numbers everywhere (~40+
   uncentralized thresholds). Dead config knobs advertise unbuilt mechanisms
   (`git_self_branch`, `self_edit_health_probe_s`, `auto_subgoals`, phantom getattr keys).
9. **Self-improvement: a coherent accident-safety subset of an un-amended plan.** The propose→
   approve→apply boundary is the cleanest code in the stack — but the health-probe leg is
   unbuilt (a non-crashing bad self-edit is invisible; the watchdog never reads heartbeat.json),
   the token gate skips exactly the endpoints that matter (`/api/control/*`, `/api/chat`,
   `/api/speech/*` are open to any LAN host while the bind is 0.0.0.0), and
   SELF_IMPROVEMENT_PLAN.md still reads as a checklist of broken promises because the
   deliberate re-scope was never written back into it.
10. **Voice latency is a symptom of #2+#4.** The serial `complete()` → `parse_reply()` →
    `_auto_speak()` chain (the diagnosed ~12 s) exists because nothing streams between organs.
    v2's streaming cognition fixes it structurally (handoff PART 2, option A).

---

## 2. What v2 must preserve verbatim (paid tuition — do not re-derive)

- **The objectives rotation gate**, whole (objectives.py): asymmetric frustration
  charge/relief, park-at-8-and-rotate, thaw-with-credit, escalate-once-per-60-ticks,
  mandatory WHY. *This is v2's glue template.*
- **The GPU speech-gate** (gpu_gate.py + dashboard Condition wait): liveness-bounded blocking
  acquire, fails open. *This is v2's transport template.*
- **The quote-aware auto-correcting linter** + PowerShell list-form routing (tools.py:132-217):
  the L-1 "guardrails never lie, never stonewall" lesson as code. Becomes the flagship repair
  path of the v2 validate gate — its contract becomes the *whole gate's* contract.
- **Async-by-default bash + jobs ledger as a unit**, incl. stream-to-file-not-PIPE, the
  auto-background handoff, the `intent` echo, exactly-once delivery, prune caps.
- **WAL + recover()** (boot-as-crash-recovery IS the lifecycle), orphan reaping, atomicio
  discipline.
- **The skill pipeline**: AST validation incl. alias catching, dry-run LOAD vs CALL distinction,
  versioning/rollback, auto-promotion, the near-duplicate domain guard, the 30 s watchdog.
- **Trust-boundary mechanics**: propose-only verbs, anti-brick write_file guards, pre-apply
  checkpoint as rollback floor, per-file restore that never touches PROTECT_PATHS, boot-PAUSED
  on every spawn, `_restart_eidos_keep_armed` vs `_ctrl_stop`, up-front rollback attempt
  counting, stability re-arm, `_LIFECYCLE_LOCK`.
- **Listening-hold semantics** (fail-open, TTL, 300 s ceiling, intervention override) — replace
  only the polled-file transport.
- **Voice pipeline internals**: `_speech_segments` segmenter, raw-PCM splice, `-probesize 32`,
  fade-in, lazy say/stream contract, browser audio-unlock dance, bf16 service env.
- **Context wins**: history-as-real-thread with ×N collapse, `_norm_cmd` signatures, world-model
  panel + step-keyed recall, salience block at the decision point, per-section budgets,
  data-hygiene/prompt-injection contract.
- **Dream-cycle safety**: snapshot-before-rewrite, truncate-only-after-success, deterministic
  fallback, ReasoningExhausted retry ladder, combined single-call dream.
- **The no-tool branch taxonomy**: thought-only and reply-only ticks are valid, never nagged.

---

## 3. The v2 architecture — one organism

### 3.1 Process topology (three processes, one trust boundary)

| Process | Role | Notes |
|---|---|---|
| **supervisor** (operator-owned) | watchdog, lifecycle, git safety, self-edit apply, self-guide apply, auth, control API, thin UI server | The trust boundary. Stays on PROTECT_PATHS. Import-isolated from agent modules (today a broken tools.py can wound the watchdog). Holds the child's Popen handle and `wait()`s on it — the watchdog becomes an interrupt, not a 5 s poll. |
| **eidos** (the organism) | kernel + glue + cognition + action + expression | One process, but internally decomposed (below). |
| **voice** | speech registry, segmenter, ffmpeg FX, SSE, **GPU gate server** | Split out so a TTS bug can never take down the trust boundary. The gate moves with it (it stamps liveness from the synthesis stream). Could remain a supervisor module short-term — separation is the goal state. |

Sentinel files (`paused`, `should_run`, `chat_hold`) **stay as crash-survivable ground truth**
— the doctrine violation was never the files, it was the *polled consumption*. eidos gets ONE
long-poll/SSE control channel from the supervisor (the gpu_wait pattern in reverse): state
changes push instantly; files are read at boot and on channel loss.

### 3.2 Inside eidos — the brain map as actual modules

```
kernel/    — event bus + scheduler. ONE wake queue; every producer posts WakeEvents:
             control-channel push (pause/resume/hold), intervention ingress, job-completion
             (waiter threads holding Popen handles — exit codes recovered, tasklist polling
             deleted), timer heartbeat (adaptive cadence as just another event source),
             speech-gate release. The cognition cycle BLOCKS on the queue.
             Kills: _interruptible_sleep, pause/hold/intervention polling, per-tick jobs scans.

state/     — the StatePacket: one schema'd, diffable artifact (BIBLE §2.2) answering the §4
             acid test (what do I know / what am I doing / what changed / am I blocked) as
             FIELDS, not prose. One ownership table (who writes each surface — enforced, not
             commented). Typed Event + typed Failure taxonomy: ToolResult gains `kind`
             (parse|timeout|blocked|network|constraint|exec|...); observations become typed
             episodes (see memory/).

glue/      — the subcortex. Each module: deterministic input → signal in the StatePacket AND a
             mechanical consequence (the gate recipe: external counter + asymmetric
             charge/relief + threshold action the model cannot veto + one-shot legible banner +
             cooperative tools). No CAPS-lock pleas.
             · gate/       (Basal Ganglia)  — objectives rotation, as-is. The template.
             · salience/   (Amygdala)       — ≤7 concerns + new-since-last-tick; formalized,
                                              incl. supervisor crash-notes via an INBOX the
                                              agent ingests (no more behind-the-back writes).
             · strain/     (Insula)   NEW   — typed-failure accumulation mechanically lowers
                                              retry budgets and forces method switch. Replaces
                                              the tension-banner prose + loop-warning pleas.
             · conflict/   (ACC)            — loop detection (keep _norm_cmd) gains TEETH: at
                                              K identical signatures the validate gate REFUSES
                                              the repeated action with a repair path, instead
                                              of injecting "STOP re-reading…".
             · condition/  (DMN)      NEW   — discrete STABLE/FOCUSED/STRAINED/RECOVERY label
                                              from recent success/failure; replaces XP-mood in
                                              context. persona.py demotes to pure dashboard
                                              cosmetics (or deletion — Dean's call).
             · recall/     (Hippocampus) NEW— episodic store: observations + thoughts + errors
                                              + dream records unify into ONE typed episode log
                                              (situation→action→outcome→fix). Recall keyed on
                                              the StatePacket, injected involuntarily ("this is
                                              like last time → do X") BEFORE failure.

cognition/ — the context COMPILER + the model call.
             · ONE assembly path (legacy deleted).
             · True KV-stable prefix: byte-stable block [identity · output contract · tools ·
               skills · constraints · self-guide · mission] cached and never re-sent in
               effect; ALL volatile content (rotation banner, gauge, presence, chat, deltas)
               BELOW it. Finishes deferred CONTEXT_REDESIGN item A. Target: per-tick prefill
               drops from ~2,400 tok to the delta (~hundreds).
             · Output contract enforced by GBNF grammar at decode time: thought + (tool|reply|
               both). Deletes ~85 % of parser.py and the parse-error tick tax. The system
               prompt sheds its ~40 % behavioral-pleading share — pleas either become glue
               mechanisms or die.
             · STREAMING cognition: reply tokens detected as they stream; completed sentences
               pushed to voice immediately (subsumes TTS handoff PART 2, option A: first text
               <1 s, first audio ~2.5 s). The on_token plumbing + cache_prompt already exist.

action/    — ONE validate-before-execute gate (BIBLE §2.6) in front of a unified registry
             (tools + skills, schema'd):
             · name normalization/aliasing (today ad-hoc per tool)
             · args_schema validation (schemas are already stored — and never read!)
             · cross-cutting preconditions (disk/blocked-patterns/bounded-work — today
               copy-pasted ×5), platform-correct pattern set (PowerShell verbs, not apt/pkill)
             · repair paths, with the linter as flagship; contract: auto-correct and RUN,
               block only with a working alternative, never lie, never stonewall.
             · jobs ledger v2: waiter threads → exit codes + completion events; single-writer.
             · close the skill-shadowing hole: reservations derived from the live registry.

expression/— reply / speak / thought as one output module; auto-speak backstop kept; ONE
             dashboard client reading config.dashboard_port (today :8099 is hardcoded in three
             places, two of which fail silently).
```

### 3.3 Supervisor v2 (closing the incoherences, keeping the trust model)

The accident-safety model ("eiDOS proposes, operator applies"; git-reversible; NOT
adversary-proof) **stays — it's a deliberate, documented choice.** v2 fixes the parts that are
incoherent *within* that model:

1. **Uniform auth**: one `_require_auth` on EVERY state-changing POST — including
   `/api/control/*`, `/api/chat`, `/api/speech/*` (today the kill-switch and the agent's input
   channel are the unguarded ones). Bind 127.0.0.1 + Tailscale, or keep 0.0.0.0 with the token
   actually set — Dean's call, but no more half-gate.
2. **Build the missing health-probe leg**: `pending_apply` marker → `applied_ok` breadcrumb →
   heartbeat-newer-than-baseline within `self_edit_health_probe_s` (the knob already exists,
   read by nothing) → auto-rollback on timeout. Today a non-crashing bad self-edit is
   invisible; the watchdog supervises PID-exists only.
3. **Watchdog by event**: `proc.wait()` on the held handle; heartbeat staleness as the
   wedged-alive detector.
4. **Crash notes via inbox**, ingested through salience — the supervisor stops writing into the
   agent's memory files directly.
5. **Amend SELF_IMPROVEMENT_PLAN.md** to match the accident-safety reality (mark Group A
   "deferred by decision"), delete the leftover LLM preamble, fix the lying docstrings, and
   either implement or delete `git_self_branch`.

---

## 4. The deletion list (v2 phase 0 — no behavior change, instant cohesion)

Legacy assembly + SYSTEM_PROMPT + pyramid renderer + intelligence-section/recall-cache (incl.
its live writer `dream_prefetch`) · the `memory.md` generation (tool_remember, aliases,
fallbacks, legacy compact, its prompt lines) · subgoals tier (plan_goal, PLANNING_* prompts,
auto_subgoals tombstone, dashboard rendering) · embedding stack (or commit to it — decision
below) · SSH standby/session.py · Linux probes in safety.py/telemetry.py · attempt_llm_restart
+ [self_healing] · emit_flavor's second LLM call (fold into the dream) · legacy WAV speech
endpoints + `/api/pause` toggle · `_tension_note`/`_BREADTH`/breadth-menu · `goal_complete`
process-exit lifecycle + ask_supervisor/pending_questions · http_get (http_request wins) ·
dead knobs (git_self_branch*, self_edit_health_probe_s*, llm_local_only, phantom getattr keys
— *unless built per §3.3) · `_looks_like_powershell` · one of two tree-killers · Pi
docstrings/UI gauges/64 GB disk scale · repo-root clutter (exam fixtures config.json/
settings.yaml/defaults.txt → tests/fixtures/; stray wav/mp3; `_test_*.py` stragglers) ·
`logger` NameError at dashboard.py:404 (fix, it truncates utterances) · thoughts.jsonl and
dream_*.md rotation gaps (fix).

---

## 5. Migration plan (strangler-fig — the agent stays alive throughout)

Each phase ships independently, is ghost-testable (simulate.py / replayed tick contexts), and
lands behind a git checkpoint. Order chosen so risk is front-loaded *low* and Dean feels value
immediately.

| Phase | What | Why this order | Verify |
|---|---|---|---|
| **0** | Deletion sweep + repo hygiene + doc reconciliation (§4) | Zero behavior change; shrinks every later diff; kills the parity tax | tests still green; 1 h live run |
| **1** | Typed failures + typed events (`ToolResult.kind`, episode schema) | Foundation every later phase consumes; tiny blast radius | unit tests; observations show types |
| **2** | GBNF output contract; shrink parser to the happy path | Highest leverage per BIBLE; removes a whole failure class before the loop refactor | parse-error rate → 0 over a live day |
| **3** | **Streaming cognition → voice** (reply-first + per-sentence TTS push) | Delivers the thing Dean already asked for (handoff PART 2-A); proves the streaming spine | first text <1 s, first audio ~2.5 s, measured |
| **4** | Kernel event bus + supervisor control channel + job waiter threads | Kills the polling nervous system; pause/hold/chat wake become instant | latency: chat→context <100 ms; no sleeps in loop |
| **5** | Context compiler: one path, true KV-stable prefix, volatile-to-tail | Needs 1–4's stable foundations; big prefill win | prefill tokens/tick measured before/after |
| **6** | Glue with teeth: strain, ACC-refusal, condition label; pleas deleted as mechanisms land | The doctrine's core; needs typed failures (1) and the gate (exists) | ghost replays of tick-969-class scenarios |
| **7** | Episodic memory unification + state-triggered recall | Needs typed episodes (1) and the StatePacket (5) | "this is like last time" fires in replay |
| **8** | Shell split (supervisor/voice/UI), uniform auth, health-probe leg | Largest mechanical move; everything before it reduced what must move | apply→probe→rollback drill; LAN auth scan |

run_loop's decomposition happens across 4–6: the ~690-line monolith becomes an explicit,
testable gate pipeline (the audit confirmed every concern is already an identifiable block —
"the v2 work is separation plus event transport, not invention").

---

## 6. Decisions — RESOLVED by Dean, 2026-06-09

1. **Voice approach: C** — build A (stream the main tick, reply-first, per-sentence TTS push)
   now; add a dedicated chat fast-path later if A's latency isn't enough. The kernel must keep
   the fast-path trivial to add.
2. **Voice process: separate service** — TTS pipeline + GPU gate move out of the supervisor
   into their own process (new nssm service); a TTS bug can never take down the trust boundary.
3. **Embedding/semantic recall: COMMIT & WIRE** — do NOT delete embedding.py. Download the
   MiniLM ONNX model, enable it, and make semantic recall real alongside BM25 (folds into
   phase 7's recall work; the embedding store becomes part of the episodic recall key).
4. **persona.py: demote to cosmetics** — creature/XP/titles stay as dashboard charm but leave
   the model's context entirely; the computed condition label replaces mood as the
   load-bearing signal.
5. **Auth posture: 0.0.0.0 + uniform token** — keep LAN/Tailscale reachability as-is, set a
   real token, enforce it on EVERY state-changing POST (control/chat/speech included).
6. **Branch discipline: bless main** — commits-on-main is the doctrine; amend
   SELF_IMPROVEMENT_PLAN.md to match reality and delete the dead `git_self_branch` knob.
   (v2 development itself happens on the `eidos-v2` branch in a separate worktree until it
   replaces v1 — that's a development fork, not the self-improvement stack's discipline.)

---

## 7. Mantra for the build

*One organism: events in, one state packet, one mind call, one validated act, typed outcomes
back into memory.* Where v1 added a sentence to the prompt, v2 adds a mechanism to the glue —
and where v1 added a file and a poll, v2 posts an event on the bus. Build the substrate; the
character emerges.
