# eiDOS v2 — handoff for the operator-supervised phases (4, 7-embeddings, 8)

Phases 0–3, 5, 6 were completed autonomously (branch `eidos-v2`, all gated green, v1 on
`main` never touched). The remaining work needs Dean in the loop because each piece either
edits the **operator-owned trust boundary** (`dashboard.py`, on `git_safety.PROTECT_PATHS`),
mutates the **environment** (a ~90 MB model download + a venv package install + a 16 GB-VRAM
cohost decision), or needs **live dual-process validation** that can't be done from tests
alone. None of it should land unsupervised. Each is fully designed below.

## What's done (autonomous): 0a–0n, 1, 2, 3, 5, 6
See V2_PROGRESS.md. Net so far: ~3,600 lines deleted (two dead generations + Pi vestiges),
the failure taxonomy + async exit-code fix, GBNF-constrained output, streaming voice
(first-audio ~12s→~2.5s), the KV-stable context compiler (95% prefix reuse), and the strain
glue with mechanical teeth. The suite is green and cleaner than v1's (which fails 23).

---

## Phase 4 — kernel event bus + control channel + job waiters (needs supervision)

**Why supervised:** edits `dashboard.py` (trust boundary), adds a `jobs.json` writer (needs the
deferred cross-process lock), and the wins only show under live dual-process runs.

**Design (the audit + blueprint agree):**
1. **Supervisor control channel.** Add `GET /api/control/wait` to the dashboard: it holds a
   `threading.Condition` over the {paused, chat_hold, intervention-present} state and returns the
   instant any of them changes (the GPU-gate pattern in reverse). eidos replaces its three
   file-poll gates (pause @5s, hold @2s, interventions @≤2s) with ONE blocking call to this
   endpoint. KEEP the sentinel files as crash-survivable ground truth — read them at boot and on
   channel loss; it's the *polled consumption*, not the file, that violates ARCH #1.
2. **Watchdog by event.** `_spawn_eidos` currently discards the child's Popen handle and 5s-polls
   `tasklist`. Hold the handle and `proc.wait()` on a thread → death is an interrupt, not a poll.
   Keep heartbeat-staleness as the wedged-alive (vs dead) detector.
3. **Job waiter threads.** Per async job, a thread that `proc.wait()`s then writes the exit
   sidecar + flips ledger status — removes the per-tick `tasklist` subprocess storm and the
   PID-reuse hazard. PREREQUISITE: give `jobs.json` a single-writer rule or a lockfile first (the
   dashboard also writes it on reap; a waiter thread becomes a third writer). The phase-1 exit
   sidecar already makes the exit code recoverable, so this is a latency/robustness layer on top.

**Validation:** chat→context latency under 100 ms; no `time.sleep` polls left in run_loop's gates;
watchdog reacts to a killed child within ~1s; no duplicate eidos under apply/stop races.

---

## Phase 7 — episodic memory unification + wire embeddings

**Two parts. The refactor is in-process (could be autonomous with live observation); the
embedding wiring needs Dean's environment authorization.**

**7a — Embedding wiring (needs Dean): decision was COMMIT+WIRE.**
- `models/all-MiniLM-L6-v2/` is ABSENT and `onnxruntime` is not installed. Steps: download the
  MiniLM ONNX (~90 MB) to that dir, `uv pip install onnxruntime` (or onnxruntime-gpu), set
  `embedding_enabled = true`, decide `embedding_cohost` (CPU vs sharing the 16 GB card with the
  resident house model — likely CPU to avoid eviction). `embedding.py` is already a complete
  pipeline (load_model/embed_texts/embed_and_store) with zero live callers — it just needs the
  model present + the recall path wired. Verify it doesn't evict house-ai from VRAM.

**7b — Episode store (in-process; do with live observation):**
- Today the episodic material is shredded across four surfaces: observations.jsonl (truncated
  every dream), thoughts.jsonl (unbounded, never recalled by similarity), the knowledge
  "errors" category (free prose), and dream records (write-only). BIBLE §2.4 wants ONE typed
  episode store of (situation→action→outcome→fix) with **state-similarity recall that fires
  involuntarily before failure** ("this is like last time → do X"), not query-RAG.
- Now feasible because phase 1 gave every outcome a typed `fail_kind` and phase 6 persists the
  outcome stream (`workspace/state/outcomes.jsonl`). The episode = {state packet, action,
  fail_kind, outcome, fix}; recall keys on the current StatePacket (BM25 now, embeddings after
  7a). Inject the top matched episode into the salience block before the model acts.
- Why live observation: the value (does recall actually fire on the right situations?) is only
  judgeable against real ticks; ghost-testing covers the mechanics but not the relevance tuning.

---

## Phase 8 — shell split + uniform auth + health probe (needs supervision)

**Why supervised:** edits `dashboard.py` (trust boundary) and changes how Dean reaches the
dashboard; needs the apply→probe→rollback drill run live.

**Design:**
1. **Uniform auth.** One `_require_auth` on EVERY state-changing POST — today the token (when
   set) skips exactly `/api/control/*`, `/api/chat`, `/api/speech/*`, i.e. the kill-switch and the
   agent's input channel. Decision: keep 0.0.0.0 + enforce a real token uniformly.
2. **Health-probe leg (the genuinely missing self-edit safety).** The advertised pipeline is
   propose→approve→apply→**health-probe**→auto-rollback, but the probe leg was never built — a
   non-crashing bad self-edit is invisible (the watchdog checks PID-exists only). Build:
   `pending_apply` marker → `applied_ok` breadcrumb on the new boot → require a heartbeat ts newer
   than a pre-kill baseline within `self_edit_health_probe_s` (the knob exists, read by nothing) →
   auto-rollback on timeout. Reconcile a dangling marker in `main()` before arming the watchdog.
3. **Shell split (larger, optional).** dashboard.py is four programs (supervisor / voice / UI /
   GPU gate). Split voice+gate into their own nssm service (decision: separate service) so a TTS
   bug can't wound the watchdog; keep supervisor+apply+watchdog co-located (the rollback is the
   apply path's safety net). The 1,750-line inline HTML becomes static files + a thin status API.

**Validation:** LAN auth scan (every state-changing POST 401s without the token); a deliberate
bad self-edit auto-rolls-back within the probe window; voice service restart doesn't disturb the
watchdog.

---

## Suggested order when Dean is available
8.2 (health-probe leg — closes the real self-edit safety gap, small) → 8.1 (uniform auth, small)
→ 7a (embedding wiring — one environment session) → 4 (event bus — the big IPC change) →
7b (episode store — wants live observation) → 8.3 (shell split — largest, mostly mechanical).
