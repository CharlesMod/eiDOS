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

## Phase 1 — typed failures + typed events (code complete, pending final gate + commit)

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

## Later phases (see blueprint §5)
3 streaming→voice (option A; fast-path later per decision C) · 4 kernel/event bus ·
5 context compiler/KV prefix (fixes _enforce_ceiling known-gaps) · 6 glue teeth ·
7 episodic memory (+ wire embeddings per decision) · 8 shell split + uniform auth + health probe
