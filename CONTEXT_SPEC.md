# CONTEXT_SPEC.md — the context window as working memory

> ## ⚠ REVISED AFTER ADVERSARIAL REVIEW (2026-07-13)
> A Fable code-grounded review found this spec's *framing* sound but its *plan* inverted. Corrections,
> now authoritative over the original text below:
> - **Three of the four "handoffs" already ship in production** — continuous per-tick encoding with
>   arousal salience (`memory_manager.encode` in `after_outcome`), sleep consolidation, and
>   situation-cued recency-weighted recall. Handoff #1 was NOT the missing piece.
> - **The real cause of "wakes thin" was a units bug:** `compaction.should_compact` compared
>   `observations.jsonl` *bytes* to an *8000-token* threshold → it dreamed at ~2k tokens of a 16k
>   window, wiping working memory every few minutes. **Fixed** (token-based, threshold 5000) — plus
>   `_OBS_KEEP_TAIL` 4→20, recall budget 1200→4000 chars, history depth 14→24, and coherent ceilings.
>   This unlocks the 16k window we already have (lived stream ~2k → ~6k tokens), no model restart.
> - **Do NOT build the salience-*ranked* living stream (§5 reordering).** Keep the stream chronological
>   (preserves the thought→action→result adjacency a small model needs) and let salience drive
>   RETENTION/FIDELITY only (§6). Keep recall zones in the volatile TAIL, not between the KV-stable head
>   and the tick prompt — mid-context recall forces a near-full re-prefill every tick (gemma3 SWA makes
>   this worse), the exact lesson `context.py` already encodes.
> - **32k is VRAM-safe** (gemma3 SWA: only 8 global layers scale; q8 KV at 32k ≈ VRAM-neutral) but
>   **deferred** — the 16k is ~60% empty until the fix above is validated. Phase 2 only if the used
>   window proves too small.
> - **Open follow-up:** `LongTermStore.__append`/`__reembed` re-embeds the WHOLE store every commit
>   (O(n), ~330 embed calls/acting-tick now, grows forever). Make it append-incremental.
>
> The zone/handoff narrative below is retained as the accurate *mental model* of the system already
> built — read it as description, not as a build order.

---

# (original spec) — the context window as working memory (32k target)

> The context window IS the creature's **short-term / working memory**. Everything else — the engram
> long-term store, the episodic ring, the knowledge (BM25) store, the skill manifest, the reward
> value cache — is **long-term memory**. This spec treats the 32k window biomimetically: an
> attention-gated, salience-weighted working set that is *continuously* encoded to long-term as it
> flows, and *repopulated* by cued retrieval — not a chronological dump that suffers amnesia at 8k.
>
> Scope: **32k now** (gemma served with KV-quant at `-c 32768`). 64k+ is parked on VRAM. Every budget
> here is a **fraction of the served `n_ctx`**, not a hardcoded number, so the same design scales down
> to a Jetson node and up to a 128k cluster body.

---

## 1. The problem today (three incoherent limits + amnesia)

| Limit | Value | Effect |
|---|---|---|
| Model window (`llama-swap -c`) | 16,384 | hard ceiling |
| Compaction trigger | 8,000 | **guts history here** → the creature lives in an ~8k box |
| Context budget cap | ~30k (claims "128k") | fiction; would overflow the 16k KV and truncate the *head* (identity) if compaction didn't fire first |

Consequences the runs showed:
- **Amnesia, not consolidation.** Compaction at 8k distills + *drops* — the creature wakes thin ("it's quiet," "clawing for context").
- **Chronological, not salient.** Working memory is "the last 20 observations" (a FIFO), so a dull tick crowds out a pivotal one.
- **Lossy handoff.** Encoding to long-term happens *at* compaction, so anything salient that scrolls off between compactions is simply gone.

## 2. Biomimetic principle

Human working memory is **small, attention-gated, and reconstructive**. It does not retain the day; it holds a vivid *specious present*, encodes the salient parts to long-term continuously (hippocampal encoding), and *reinstates* relevant long-term traces when the situation cues them (pattern completion). Forgetting the raw is a feature — memory lives in the store and is *recalled*, not held. Our context should work the same way.

## 3. The 32k working-memory layout (zones)

Assembly order follows the **serial-position curve**: the stable self at the **primacy head**, recalled long-term support in the low-attention **middle trough**, and the vivid **present clustered at the recency tail** (where the model attends most), ending on the attentional spotlight.

| # | Zone | Budget¹ | Brain analogue | Source / handoff |
|---|------|--------:|----------------|------------------|
| **A** | **Identity core** (stable head, KV-cached) | 3.5k | persistent self-schema / semantic self | system prompt + granted-unit stanzas + self-guide. Does not decay. |
| **B** | **Semantic ground** (recalled facts) | 2.5k | semantic reinstatement | `knowledge` BM25 + engram long-term facts, **situation-cued, recency-weighted**. LT-semantic → WM. |
| **C** | **Autobiographical thread** (recalled episodes) | 2.0k | episodic reinstatement (hippocampal pattern completion) | `engram` episodic recall — "last time here, X happened." LT-episodic → WM. |
| **E** | **The living stream** (WM core) | **15k** | the specious present / active working memory | a **salience-ranked, recency-weighted narrative** of recent thoughts + deeds + outcomes, the active objective's arc, open bets, commission state. The heart. |
| **D** | **Sensorium** (the felt present) | 1.0k | sensory buffer + interoception | felt body (energy/arousal/mood) **+ machine-native senses** (daylight, network, thermal, load, power). Only salient sensations get prominence. |
| **F** | **Active intent** (goal maintenance) | 1.5k | prefrontal goal-maintenance | current objective / plan / subgoals — rehearsed, biases all. |
| **G** | **Orienting** (what's new) | 1.0k | orienting response / interrupt | new Charlie messages, news queue, env alerts, quest issuance. |
| **H** | **Attentional spotlight** (tick prompt) | 0.3k | the "now" | the tick prompt. Protected trailing block. |
| | *headroom (response + safety)* | ~3.2k | | reserve under 32,768 |

¹ Budgets are **fractions of the served `n_ctx`** (here shown resolved for 32k), computed at boot from the model's real window — never hardcoded. Sum of zones ≈ 26.8k, +3.2k headroom ≈ 30k, safely under 32,768.

**Ordering in the assembled message list:** A → B → C → **E → D → F → G → H**. The recalled long-term (B/C) sits in the mid-context trough as *support*; the vivid present (E onward) rides the high-attention tail.

## 4. The handoffs — stitching the memory systems holistically

This is the core of "handled holistically." Four continuous flows bind working memory to the long-term stores:

1. **Continuous encoding (WM → long-term episodic).** *Every tick*, as an event flows through Zone E, it is encoded into the episodic engram with its **arousal/salience stamp** (via `memory_manager.encode`, which already seeds birth-strength from arousal). Salient → strong; flat → weak or skipped. This is hippocampal encoding, and it happens *as the stream flows*, not at compaction — so nothing salient is lost when a tick ages out of the window.

2. **Consolidation (episodic → semantic, during sleep).** The sleep engine already dedups/decays and **distills** episodic traces into semantic long-term facts (`provenance='dreamed'`, confidence-capped). Hippocampal → cortical transfer. The day's continuously-encoded episodes are its raw material.

3. **Retrieval (long-term → WM).** Each tick, **situation- + goal-cued** recall (recency-weighted, already implemented across the 4-layer cascade and BM25) repopulates Zones **B** and **C**. Pattern completion: the present cues the relevant past back into mind.

4. **Compaction reframed as consolidation, not amnesia.** It fires **rarely** (~26k, near the tail of the budget), gists Zone E's *oldest* items into episode summaries, and drops the raw — but the raw was **already encoded** by handoff #1, so nothing is lost. The creature remembers via **recall**, not raw retention. On wake it reconstitutes a *rich* working memory through B/C/E — the cure for "it's quiet."

```
        ┌──────────────── WORKING MEMORY (32k context) ────────────────┐
  cue → │  B semantic   C episodic        E living stream  D sensorium  │
        └───▲───────────────▲──────────────────┬──────────────┬────────┘
   (3) recall            (3) recall     (1) continuous     (senses feed
   recency-weighted   pattern-complete   encode w/ salience   the buffer)
        │                   │                  ▼
   ┌────┴─────┐      ┌───────┴──────┐   ┌───────────────┐   (4) rare
   │ knowledge│      │   engram      │   │ engram episodic│  compaction =
   │  (BM25)  │◄─────│ long-term     │◄──│     ring       │  gist oldest,
   └──────────┘ (2)  │  (semantic)   │(2)└───────────────┘  raw already
              consolidate (sleep)  ◄─── dedup/distill/decay   encoded
```

## 5. Salience gating — the attention doorman

What **enters** Zone E is ranked, not chronological:

```
salience = w_a·arousal_stamp + w_s·surprise(RPE / world-model) + w_g·goal_relevance + w_r·recency
```

Mirrors amygdala tagging + attentional selection, reusing signals **already computed** each tick (the arousal stamp, the reward-prediction-error, the world-model surprise, the active-objective match). When Zone E is over budget, the **lowest-salience** items are compressed or dropped first — a pivotal, surprising tick survives; a dull one fades. All weights are declared knobs.

## 6. Decay curve — fidelity by age × salience

Within Zone E an item degrades gracefully rather than vanishing at a cliff:

```
recent & salient   → full text
older / flatter    → one-line gist
below threshold    → dropped from WM (but already encoded to episodic — recallable)
```

A tuned recency×salience curve (declared knobs), so the window holds *many faint traces + a few vivid ones*, like real working memory — not N full-fidelity rows and then nothing.

## 7. Scaling to the body (Jetson → cluster) and the 128k ceiling

Because every budget is a **fraction of the served `n_ctx`** read at boot:

- **Small body** (Jetson, tiny model, ~4–8k window): zones compress, the decay curve steepens, the creature leans harder on **recall** — it holds little but remembers via the store.
- **Sprinter (now)**: 32k via `-fa --cache-type-k q8_0 --cache-type-v q8_0 -c 32768`.
- **Big body** (cluster, 128k): the living stream widens, compaction becomes almost never, more episodes stay resident. Same design, no rewrite.

The **senses** (Zone D) scale the same way: a node with a network senses the LAN; a solar node feels daylight/power; a shared machine feels co-tenant load. The sense-set is read from the body, not assumed.

## 8. The consistency invariant

Always, at every body size:

```
model n_ctx  ≥  Σ zone budgets + headroom  >  compaction trigger
```

For 32k: **n_ctx 32768 ≥ ~30000 budget (>26000 compaction) + response reserve (2048–4096)**. This is the single rule that keeps the three limits coherent (today they aren't).

## 9. Rollout (dark-flag, reversible)

- `pillars_working_memory_enabled` (default off). Build the **zone assembler**, **continuous-encode hook**, **salience gate**, and **decay curve** behind it; assert byte-identical context with the flag off (the wiring-test discipline).
- Land the **coherent-limits invariant** + budget-from-`n_ctx` first (safe, no model restart).
- Flip the flag **with** the gemma `-c 32768` + KV-quant change (one brief serving blip).
- Then land **Zone D senses** (daylight first) into the felt body — now that there's room to perceive.

## 10. Open knobs (to tune on the first 32k run)

- Zone budget fractions (§3), salience weights (§5), decay thresholds (§6), compaction trigger (§8).
- Metric to watch: on wake after a compaction, does the creature reconstitute continuity (recall repopulating B/C/E) rather than reporting a blank/"quiet" box? And does a *pivotal* tick from 10 minutes ago still surface when the situation re-cues it?
