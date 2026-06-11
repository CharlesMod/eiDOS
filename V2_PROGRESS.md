# eiDOS v2 — implementation progress (branch eidos-v2, worktree Kairos-v2)

Tracks the EIDOS_V2_BLUEPRINT.md migration. Live v1 keeps running from
`C:\Users\cmod\llm\Kairos` (main) untouched; v2 develops here.
Run tests with the v1 venv: `C:\Users\cmod\llm\Kairos\.venv\Scripts\python.exe -m pytest`

## Phase 0 — deletion sweep + repo hygiene (no behavior change)

- [x] 0a. SSH-standby subsystem (session.py, eidos.py standby block, grace_period_s) — 9d285d0
- [x] 0b. attempt_llm_restart + restart_cmd/local_only knobs (keep failure counting) — 9d285d0/fa00962
- [x] 0c. subgoals tier: auto_subgoals block, tool_plan_goal, PLANNING_* prompts,
      memory read/write_subgoals + current_subtask, dashboard checklist, config keys — fa00962
- [x] 0d. legacy context generation: _assemble_legacy, SYSTEM_PROMPT, pyramid renderer,
      _build_intelligence_section + recall_cache + dream_prefetch, briefing_model flag,
      legacy compact() + COMPACTION_SYSTEM/USER prompts; harnesses (validate/exam/
      simulate/stress/validate_memory) ported to the briefing path; legacy test classes
      removed (briefing dream/context coverage retained). test_simulation memory.md scaffolding ported in 0e.
- [x] 0e. memory.md generation: tool_remember, read/write_memory aliases, read_plan
      fallback, prompt lines advertising `remember`, stub snapshotting
- [x] 0f. _tension_note + _BREADTH + _breadth_menu (gate owns pivoting)
- [x] 0g. tools misc: tool_http_get + fetch/http aliases, _looks_like_powershell,
      unify tree-killers, unused imports
- [x] 0h. goal_complete process-exit lifecycle + ask_supervisor/pending_questions
- [x] 0i. safety/telemetry Linux probes (check_ram/kill_child_processes/get_cpu_temp/
      get_cpu_pct) — Windows equivalents or removal of dead callers
- [x] 0j. dashboard: /api/pause toggle, legacy WAV speech endpoints, logger NameError
      at ~:404, stale read-only docstring, Pi-era gauges (CPU temp 105C, 64GB disk)
- [x] 0k. dead config knobs: git_self_branch, llm_local_only, phantom getattr keys
      promoted to real Config fields (world_state_max_items, context_notebook_max_chars,
      knowledge_dedup_threshold) — keep self_edit_health_probe_s (built in phase 8)
- [x] 0l. rotation gaps: thoughts.jsonl rotation, dream_*.md cap
- [x] 0m. repo hygiene: repo-root exam-run artifacts (config.json/settings.yaml/
      defaults.txt) deleted; _test_*.py/_analyze.py/_seed_dashboard.py -> scripts/;
      Pi docstrings (knowledge "SD card", embedding RPi cores, config.toml comment)
- [x] 0n. doc reconciliation: amend SELF_IMPROVEMENT_PLAN.md (accident-safety re-scope,
      drop LLM preamble line), update README.md (still describes the Pi project)

DECISIONS (resolved 2026-06-09): voice=C (A first), voice=separate service,
embeddings=COMMIT+WIRE (do NOT delete embedding.py), persona=cosmetics-only,
auth=0.0.0.0+uniform token, branch=bless main.

## Phase 1 — typed failures + typed events (COMPLETE — 7efe060 + aaca51a, suite green 596 passed/0 failed)

- ToolResult.fail_kind taxonomy (args/blocked/timeout/network/exec/parse/llm/crash/
  no_such_tool/error); execute_tool guarantees every failure leaves typed.
- Async exit-code sidecar: list-form PS commands get an epilogue that records the real
  exit code to <out>.exit and re-raises via `exit` (sync returncode stays truthful).
  collect/refresh/reap consult it: dead pid -> completed(0) | failed(N) | unknown->completed.
  Fixes the audit CRITICAL "failing async commands report success". 11 tests (test_failkind.py).
- Typed observations: fail_kind on tool/parse/llm/async/watchdog observation records.
- ALSO: test-suite repair. The phase-0 background gates piped pytest through `| tail`
  (exit code masked — vacuous); v1 baseline was ALREADY red (23F/598P). Repaired:
  load_config crash (orphaned planning.get lines — real production bug), ~30 stale tests
  ported from pre-redesign expectations to the current contract, 2 known-gap annotations
  pointing at phase 5 (_enforce_ceiling assumes the legacy 3-message shape).
  GATE RULE: never `pytest | tail` — redirect to file, check $? explicitly.

## Phase 2 — GBNF grammar-constrained decoding (PLANNED — next up)

Goal (BIBLE 2.1): the model literally cannot emit a malformed action. Kills the 6-tier
parser fallback cascade (~210 lines, 85% of parser.py) + the parse-error tick tax, and
makes no_such_tool unrepresentable (tool names are grammar-enumerated from the registry).

Build order:
1. grammar.py (new): build_tick_grammar(tool_names, require_reply=False) -> GBNF string.
   root ::= thought? reply? toolcall?  — thought = tag-free text run; reply = <reply>..</reply>;
   toolcall = <tool>NAME</tool><args>JSON</args> with NAME ::= "bash" | "read_file" | ...
   generated from the LIVE registry each tick (skills hot-load -> regenerate; cache keyed
   on sorted tool names). JSON rule embedded from llama.cpp's json.gbnf. The require_reply
   flag + reply-before-tool ORDER are built now but only exploited in phase 3 (reply-first
   becomes structural, not prompted).
2. llm.py: complete(..., grammar=None) -> payload["grammar"]. FAIL-OPEN: on a server error
   with grammar set, retry once without it, log + typed llm observation. Tick call only —
   ask_ai/vision/dream stay unconstrained (dream gets its own grammar later, stretch).
3. config: [llm] grammar_enabled = true.
4. Verify before trusting: (a) direct :8081 grammar request works on this llama.cpp build
   (GBNF support + whether {m,n} repetition bounds are available — avoid if old);
   (b) the :8088 tap forwards the unknown "grammar" field (else soak pointing at :8081);
   (c) tok/s overhead via workspace llm_speed pattern (expect negligible; investigate if >10%);
   (d) grammar+streaming compose (sampling-time constraint — should be free).
5. Soak: validate.py staged run + exam.py graded run + 1h supervised live-sim against the
   real model; gate = zero parse_error observations and no degenerate outputs.
6. AFTER the soak: delete the parser fallback cascade (unclosed-tag/no-args/alt-format/
   _clean_json/shorthand-tags/_extract_cmd_fallback) + rewrite test_parser to the strict
   contract; shrink the format-coaching prose (grammar enforces FORM; prompt keeps CONTENT
   guidance); the eidos parse-error branch becomes a minimal guard.
Risks: tap may strip unknown fields (test early); grammar too tight fights the model
(keep thought free-form; thought-only and reply-only stay legal); old llama.cpp grammar
syntax limits. Two commits: land grammar -> soak -> delete cascade.

## Phase 3 — streaming cognition → voice (COMPLETE — option A)

Boss-waiting ticks use require_reply grammar (reply generated first; measured live: reply
formed at 0.52s vs ~5.4s in v1's trailing-reply order). _ReplyVoicePump fires TTS on the
reply's first complete sentence mid-generation; suppresses the post-tick speak. First-audio
~12s → ~2.5s. Decision C: fast-path B remains trivial to add (pump + require_reply are the
substrate). 8 pump unit tests.

## AUTONOMOUS-RUN SEQUENCING DECISION (2026-06-10)

Phases 4 and 8 modify the OPERATOR-OWNED dashboard (PROTECT_PATHS) and need live dual-process
validation — NOT appropriate to land unsupervised. Deferred to a Dean-supervised session.
Phases 5/6/7 are eidos-internal and unit-testable → done autonomously. (Phase 5 has no hard
dependency on phase 4's event bus; it's pure context.py work.)

## Phase 4 — kernel event bus + control channel + job waiters (COMPLETE — Dean-supervised)

The cross-process polling nervous system, killed. Done in three commits with live dual-process
smokes (Dean watching):
- 4a (6bae355) control channel: dashboard control_notify bumps a Condition-guarded seq on every
  control mutation; GET /api/control/wait long-polls until seq passes ?since= (the GPU-gate
  pattern in reverse). eidos's pause/hold/end-of-tick waits + the two LLM-error-branch sleeps go
  event-driven; sentinel files stay crash-survivable ground truth; fail-open to a bounded nap.
  Live: chat→wake ~31ms (was ≤2000ms), pause/resume ~25ms (was ≤5000ms).
- 4b (7eccf00) watchdog death event: _spawn_eidos holds the Popen handle; a daemon thread
  wait()s and fires _child_died, so the watchdog's 5s poll becomes _child_died.wait(timeout=5).
  Live: respawn 0.34s after kill (was 0–5s poll window).
- 4c (0abf95e) job waiters + _jobs_lock: per async job a daemon thread holds the handle and
  records the real returncode; refresh/reap/collect skip tasklist polling for waited jobs.
- Two real bugs the test investigation surfaced: _interruptible_sleep busy-spun at interval≤0
  (now time.sleep(0) yields once + preserves the test seam — test_resilience 653s→0.75s); the
  cold-boot health probe hit real HTTP in tests (skipped under the isolation flag).
- Test isolation: conftest sets EIDOS_NO_DASHBOARD session-wide so no test reaches the live
  :8099 dashboard (the channel clients check it and fail-open). 484-test fast suite 11.8s.

## Phase 5 — context compiler: one path, true KV-stable prefix (COMPLETE)

- Durable message reordered into 3 KV tiers: STABLE head (self-guide/skills/mission) → SEMI
  (plan/world-model/backlog/notebook) → VOLATILE tail (focus gauge/rotation/presence/conversation).
  The volatile tail also sits closest to the decision point (history thread + tick prompt follow),
  so this improves KV reuse AND salience. Measured: 95% byte-identical prefix across consecutive
  ticks (breaks only at the per-tick presence timestamp); old order broke at byte 0 on any tension
  change.
- _enforce_ceiling rewritten for the real briefing shape: preserves system[0] + trailing
  decision-point messages (tick prompt + optional whats_new); trims OLDEST history turns first,
  then the durable blob's tail. (Old code assumed 3 messages, took overhead from a history turn,
  left the thread+tick untrimmable — the known-gap annotations are now real assertions.)
- Fixed a real bug it exposed: _plan_next_step / _current_focus embedded an UNBOUNDED plan line
  into the never-trimmed tick prompt (an 8k plan line ballooned the prompt past the ceiling).
  Capped to 200/300 chars; deduped _current_focus's copy of the step extraction.

## Phase 6 — glue with teeth (PLANNED — next, in-process)

Replace remaining advisory prose with mechanism (BIBLE §2.3, §5; CONTEXT_REDESIGN "loop-breaker
still advisory"). Strain (Insula): typed-failure accumulation (phase-1 fail_kind) mechanically
lowers the retry/persistence budget and forces a method switch. Condition label (DMN):
STABLE/FOCUSED/STRAINED/RECOVERY from recent success/failure, replacing XP-only mood in context
(persona stays dashboard cosmetics per decision). ACC teeth: at K identical failure signatures
the validate path refuses the repeat with a repair hint instead of injecting "STOP re-reading".
All in eidos.py/context.py/a new glue module + objectives gate — unit-testable.

## Phase 7 — episodic memory (7b DONE) + wire embeddings (7a DONE)

- 7b (episode store, DONE): one typed (situation→action→outcome→fix) store, `episodes.py`. The
  shredded episodic material (observations truncated each dream, thoughts never recalled by
  similarity, the knowledge "errors" free prose, write-only dream records) gets a typed home: one
  episode recorded per ACTING tick (system/watchdog/dream/thought-only ticks skipped — not
  decisions). SITUATION key = `<active objective id>|<normalized next step>` (digits collapsed to
  `#` so v3/v4/v5 retries and ip/port variants share a situation); ACTION = tool+sig (the loop
  detector's normalized signature); OUTCOME = success+fail_kind (phase-1 taxonomy). Recall fires
  INVOLUNTARILY — `context.py` surfaces it in the volatile tail BEFORE the model acts — but ONLY
  when a STANDING FAILURE exists in this situation (a sig that failed and never recovered): it then
  shows "✗ `tool` here FAILED (kind) ×N — don't repeat" plus the working ALTERNATIVES ("✓ `tool`
  WORKED here — prefer that"). No failure → empty → no recall noise. Exact situation-key match
  preferred, same-objective fallback. Self-bounding 600-line ring; deterministic, embedding-free
  (7a can layer semantic similarity on top). 13 unit tests (test_episodes.py) + the context
  integration. eidos.py records at tick-end; context.py renders in `_assemble_briefing`.
- 7a (wire embeddings, DONE — Dean-directed): the embedding substrate, lit up and serving BOTH
  recall surfaces from ONE loaded model (the cohesive cut, not two bolt-ons). MiniLM is CPU-only
  (onnxruntime intra_op=2) so there's NO VRAM contention with house-ai — the cohost "blocker"
  dissolved; cohost just keeps the ~90MB resident in RAM. Model fetched by setup_embedding.py;
  models/ gitignored (never commit the 90MB ONNX).
  - 7a-1 (481dd57): embedding.py was complete-but-dead since phase 5. Wired live WITHOUT dream-cycle
    surgery: embed_query() (shared single-text primitive, mock-aware, fail-open), sync_knowledge_
    vectors() (idempotent boot sync — embeds only entries lacking a vector), eidos.run_loop loads
    the model + syncs once before the loop, and context._build_relevant_recall fuses BM25 + semantic
    via reciprocal-rank fusion (_rrf_blend). Embeddings off → byte-identical BM25-only. Verified
    live: a "discover machines on the local network" paraphrase semantically surfaces an "arp -a
    enumerated 25 hosts" fact and excludes an unrelated pip error. 11 unit tests.
  - 7a-2 (episode situation similarity — the centerpiece, what the 7b docstring promised): embed at
    the granularity of distinct situation KEYS (normalized → few, self-bounding 256-ring), dropping
    the opaque objective id and embedding the STEP so resemblance crosses objectives. recall() gains
    a semantic FALLBACK that fires ONLY when the deterministic exact/objective pool is empty (a
    genuinely novel situation) → pulls the nearest resembling situation's episodes, tagged "similar"
    so render says "this resembles a situation you've been in before (\"…\")". Threshold 0.45
    (calibrated for MiniLM AND the mock embedder). The index self-builds from live ticks via
    _remember_situation in record_episode (gated, hot-path-cheap: a keys-json check short-circuits
    known situations). Off → exactly the 7b deterministic store. 6 unit tests + live mock smoke
    (a dht-wiring failure under objA recalled for a novel dht-install step under objB; unrelated
    "poem about the ocean" correctly ignored).
  - Gates: episode 19/19, embedding 33/33, fast suite 525/0, non-slow simulation 61/61.

## Phase 8 — health probe + uniform auth + shell split (ALL DONE — Dean-supervised)

- 8.2 (health-probe leg, DONE): the missing self-edit safety. apply arms a pending_apply marker;
  the booting eidos drops an applied_ok breadcrumb (a paused eidos never ticks, so heartbeat
  alone can't prove a healthy boot); the watchdog's alive-branch _selfedit_probe resolves
  (booted AND paused/ticking-past-baseline) or rolls back to prev_sha at the deadline; the
  crash-loop path clears the marker; main() boot-reconciles a stranded one. Closes the hole where
  a self-edit that boots-but-hangs or wedges-alive was invisible (watchdog was PID-exists only).
  11 unit tests + boot smoke. The self_edit_health_probe_s knob is finally read.
- 8.1 (uniform auth, DONE): one _token_ok at the top of do_POST gates EVERY state-changing POST
  (control/chat/speech were ungated even with a token set); hmac.compare_digest. Default empty
  token stays open (Dean's posture). Live smoke: control/chat/speech 401 without token.
- 8.3 (shell split, DONE — Dean chose full process isolation): dashboard.py went from 3,418 ->
  1,317 lines (4 programs -> supervisor + UI; voice is its own process). Decision: FULL split (a
  TTS bug can't wound the watchdog), not modularize-in-place.
  - 8.3a (304d915): the 1,732-line inline _HTML string -> static/dashboard.html; do_GET "/" reads
    + placeholder-fills it per request. Rendered page byte-identical to the old inline (verified vs
    HEAD); live HTTP smoke. dashboard.py 3,418 -> 1,685.
  - 8.3b (d477d74): VOICE -> voice.py, a standalone service on config.voice_port (8098): GLaDOS TTS
    (/api/speech/say + /api/speech/stream) + the GPU speech-gate (/api/gpu/wait). _speech_segments
    verified byte-identical across 7 cases. The control channel (/api/control/wait) STAYS on the
    dashboard (it was interleaved with the voice block; removal preserved it and repointed its
    threading/time aliases). Callers repointed to voice_port: gpu_gate.yield_to_speech,
    eidos._post_speech, tools.tool_speak (control_wait stays on dashboard). Browser derives
    NX_VOICE_BASE from location.hostname (localhost AND Tailscale). CORS on voice responses.
    voice.py + dashboard.html added to PROTECT_PATHS. scripts/install_voice_service.ps1 registers
    HouseAI-EidosVoice via nssm but does NOT auto-start (v1->v2 cutover stays deliberate so two
    voice services never race the GPU). Two-process live smoke green (gpu/wait moved off dashboard
    -> 404, served by voice; gpu_gate reaches voice; control/wait still on dashboard).
  - No separate supervisor.py: the blueprint keeps supervisor+apply+watchdog co-located with the UI
    status API, so dashboard.py IS that module now. Gates: fast 525/0, dashboard 35/35.

CUTOVER TODO (when v2 is blessed to live): start HouseAI-EidosVoice (scripts/install_voice_service.ps1)
as part of the cutover, AFTER stopping v1's in-dashboard voice path — they must not both run.
