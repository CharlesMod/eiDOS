# OPERATOR_DIRECTIVES — when Charlie speaks, the System makes it the creature's focus

*(Charlie + Claude, 2026-07-20. Status: approved for build, flag-dark.)*

## The problem (observed live, 2026-07-20)

Charlie asked the newborn to look at the network and to check in in 10 minutes. It replied
"i can try" / "i'll send a message in 10 minutes" — then puttered around its nest and did
neither. Root cause in the code: an operator message is **consumed after one tick**
(`context.py:_build_whats_new`, comment line 507). It gets a one-tick "🗣 BOSS JUST MESSAGED —
reply THIS tick" banner, the creature replies, and the message is cleared. It never becomes a
goal; within ~12 ticks it scrolls out of context entirely. The architecture optimized for
**social responsiveness** (don't talk past Boss) but has no bridge to **task adoption** (adopt
Boss's request and see it through). A casual spoken command falls through every existing channel:
too ad-hoc for a commission, too specific for the self-guide, and objectives are self-authored.

## The design (decisions: Charlie, 2026-07-20)

**The System tags it, not the creature.** The Administrator (`administrator.py` — the SAME gemma
in the System role, behind the fourth wall, with its own dossier context and grammar-constrained
output) is the translator. The creature stays focused on *acting*; the System does the *framing*.
On an operator message the System classifies chatter-vs-command and, for a command, emits a
structured **operator directive** that becomes the creature's active objective.

**Priority focus, not hard preempt.** A directive becomes the top objective and stays primary
until discharged (done or reported blocked) — but the creature keeps judgment: it can still sleep
when adenosine forces it, or interleave a blocking dependency. It obeys without becoming a puppet.

### Flow

1. **Trigger.** A fresh operator message fires `_event("operator_message", persona)` in the tick
   loop — BEFORE the message is consumed as one-tick chat — waking the Administrator (event-driven,
   ARCH #1, no timer). The creature's own reply-this-tick behavior is UNCHANGED (it still answers
   promptly); the System acts in parallel, in its own role.
2. **Classify + frame.** The dossier gains an `operator_message` section (the text + recent chat).
   The System's grammar gains an optional `directive` object: `{is_request: bool, title, why,
   criterion?, deferral?}`. Chatter → `is_request:false` → nothing created (the creature just
   replies as today). A request → a structured directive.
3. **Adopt as a priority objective.** The directive creates an objective with `origin:"operator"`:
   set as `active_id` immediately (preempt), high priority, and — because it's Charlie's word —
   **exempt from the frustration-rotation gate AND the exposure-death cap** (an operator goal is
   never auto-parked or auto-killed; only Charlie or completion closes it). It renders in the
   situation/focus block every tick, so it persists instead of scrolling away.
4. **Discharge + report.** The creature works it until its criterion is glue-satisfied (or it
   reports blocked). On completion the System window notes it and the creature returns to
   autonomous drives. `origin:"operator"` objectives that complete are mastery evidence (they are
   real adjudicated work) exactly like self-chosen ones.
5. **Deferral → the `remind` primitive.** "check in in 10 minutes" is a directive with a
   `deferral` (a fire-time). The `remind` tool (below) persists the timer; when it fires, the
   reminder surfaces as high salience and (re)activates the operator-objective. Survives naps and
   restarts (persistent store) — unlike a `bg_run "sleep"` job, which dies on restart.

## The `remind` primitive (self-contained)

`reminders.py` — a persistent, bounded timer store (`state/reminders.json`, atomic, fail-open).
- Tool `remind(in="10m" | at="ISO", note="...")` — settable by the creature AND by the System's
  directive path. Returns a typed result (ARCH #4: honest — the reminder really is scheduled).
- A due-check at the top of each tick (cheap; it's a bounded read): a fired reminder is delivered
  into the salience/`whats_new` channel as a high-priority event (`⏰ REMINDER: <note>`), and — when
  it originated from an operator directive — re-raises that operator-objective to active focus.
- Event-driven in spirit: the fire-time is the event; we check due-ness at the one natural gate
  rather than sleeping a thread. Persistent across nap AND eidos restart (the whole point).
- Bounded (≤ 32 pending), backward-clock-guarded, corrupt-file → empty (fail-open).

## Invariants (each gets a test)

- **OD1 — Persistence over consumption.** An operator request outlives the one-tick chat banner:
  it lives as an `origin:"operator"` objective (or a pending reminder) until discharged. Pinned by
  a test that advances many ticks and asserts the directive is still the active focus.
- **OD2 — The System frames, the creature acts.** Directive creation happens in the Administrator
  role (separate llm call, its own grammar), never in the creature's tick output. Chatter creates
  nothing.
- **OD3 — Priority, not tyranny.** An operator objective preempts focus and resists rotation/death,
  but does NOT block a forced sleep or a genuinely blocking dependency (the creature can `objective_block`
  it with a reason, which reports back rather than silently dropping it).
- **OD4 — Honest throughout (ARCH #4).** `remind` returns a real scheduled/failed result; a directive
  the System couldn't frame is visible, never a silent drop.
- **OD5 — Flag-dark.** `operator_directives_enabled` (default false, requires administrator_enabled)
  and the `remind` tool behind it: off = byte-identical (one-tick-consume behavior unchanged).

## Build split

- **Core (this session, hand-built — tightly-coupled live-loop code):** objectives `origin` +
  operator-objective creation/exemptions; Administrator dossier section + grammar `directive` +
  apply; eidos `_event("operator_message")` trigger + apply wiring; flag + config + capabilities.
- **`remind` primitive (parallel agent):** `reminders.py`, the `remind` tool, the due-check tick
  hook, tests. Interface contract: `reminders.set(config, note, *, fire_ts) -> dict`,
  `reminders.due(config, now) -> list[dict]` (pops fired), tool `remind`, and a one-line
  `_deliver_due_reminders(config, tick)` the loop calls at the gate.
