# eiDOS architecture principles

Standing engineering preferences for this system. When you design a fix, choose the option
that matches these — and if you catch yourself reaching for a timer, a sleep, or a poll loop,
stop and find the event.

## 1. Event-driven over polled. Call-response, notification, or interrupt — never delay-based. (Dean, 2026-06)

**The preference, in order:**
1. **Interrupt / notification** — the producer signals the moment something changes; the consumer
   is woken. (OS event, `threading.Condition.notify`, SSE push, webhook, callback.)
2. **Call-response (blocking acquire)** — the consumer makes ONE request that returns exactly when
   the condition is met. The *wait* lives on the server side as an event wait, not a client poll.
   (Long-poll, blocking lock acquire, `select`/`epoll`, a queue `.get()`.)
3. **Polling with a delay** — only as a last resort, when there is genuinely no signal to subscribe
   to (e.g. an external process the harness can't notify us about). If you must, say so explicitly.

**Anti-patterns to avoid:**
- `sleep(N)` then check, hoping N was "long enough" — races, wasted latency, or wasted CPU.
- Fixed cooldowns / debounce timers used as a substitute for knowing when the thing actually finished.
- "Tick every N seconds to see if X changed" when X's owner could just tell us X changed.

**Why:** delays are guesses. A guess is either too short (race / fires early) or too long (dead
air / latency). An event is the ground truth. This is the same reason the tick loop must never
block on a tool (see #2): we react to reality, we don't wait on a clock and hope.

**Concrete pattern (cross-process serialize):** the resource owner holds a `threading.Condition`
and a state; it `notify_all()`s on change. A waiter calls a blocking endpoint that does
`with cond: while busy: cond.wait(...)` and returns the instant it's released. One request, woken
by an event. This is the **GPU speech-gate** (`gpu_gate.py` + dashboard `/api/gpu/wait`): the house
tick yields to in-progress TTS and resumes the moment synthesis ends — no sleeps, no cooldown timers.

**Don't bound an event-wait with a guessed duration — bound it with liveness.** The first cut of
the speech-gate used an `8 s` cap, which is exactly the kind of magic number this principle warns
against (too short for long speech, arbitrary). The fix: the producer stamps a `last_progress` time
as it streams output; the waiter holds as long as progress is fresh and only bails after `STALL_S`
with no progress (genuinely wedged), plus a generous absolute backstop. The single knob is a
*liveness* threshold ("how long with zero output = stuck"), not a guess about how long the work
"should" take — so it self-adapts to any utterance length. Reach for a fixed timeout only when
there is no progress signal to observe at all.

## 2. The tick loop must never block on a tool (real-time, low-latency)
Dispatch async by default (`bash` async, `bg_run`); a tool that hangs must not freeze the mind.
Even a 45 s foreground call is too long. Kill dead air — always be doing something. A blocking
*acquire* with a bounded cap (#1) is fine because it is bounded and event-released; an unbounded
wait or a fixed `sleep` is not.

## 3. eiDOS proposes, the operator-controlled dashboard applies
Accident-safety, not adversary-proofing. Git-reversible. See `SELF_IMPROVEMENT_PLAN.md`.
