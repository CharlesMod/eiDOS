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

## Later phases (see blueprint §5)
1 typed failures/events · 2 GBNF · 3 streaming→voice · 4 kernel/event bus ·
5 context compiler/KV prefix · 6 glue teeth · 7 episodic memory (+ wire embeddings) ·
8 shell split + uniform auth + health probe
