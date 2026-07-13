"""Configuration loading for eiDOS. TOML config + env var overrides."""

import dataclasses
import os
import sys
from pathlib import Path
from typing import List

from typed_boundary import (
    load_env_overrides,
    validate_config_document,
    validate_resolved_config,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

# The repo root (this file's directory). All default paths anchor here so eiDOS runs from wherever it
# was cloned, on any OS — never a hardcoded C:\Users\... that only exists on the original author's box.
REPO_ROOT = Path(__file__).resolve().parent

# The machine-local overlay the Settings UI / installer writes (gitignored). Loaded ON TOP of the
# committed config.toml so the hand-commented base file is never rewritten by the dashboard.
LOCAL_CONFIG_NAME = "config.local.toml"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base (mutating base). Nested tables merge key-by-key; scalars and
    lists in the overlay replace those in base. Used to layer config.local.toml over config.toml."""
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _toml_scalar(v) -> str:
    """Serialize one TOML scalar/list value (the subset the settings overlay needs: bool, int, float,
    str, and lists of those)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _toml_key(k: str) -> str:
    """A table-header/key segment: bare when safe (A-Za-z0-9_-), else quoted. Lets model names like
    `gemma4-12b` head a sub-table [llm.profiles.gemma4-12b] while still handling odd names safely."""
    import re
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k or "") else _toml_scalar(str(k))


def _emit_table(prefix: str, table: dict, lines: list) -> None:
    """Emit one [prefix] table: its scalar keys first (TOML requires them before any sub-table header),
    then recurse into nested dicts as dotted sub-tables ([prefix.sub]). Handles arbitrary depth so the
    per-model [llm.profiles.<model>] overlay round-trips."""
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subs = {k: v for k, v in table.items() if isinstance(v, dict)}
    lines.append(f"[{prefix}]")
    for k, v in scalars.items():
        lines.append(f"{_toml_key(k)} = {_toml_scalar(v)}")
    lines.append("")
    for k, v in subs.items():
        _emit_table(f"{prefix}.{_toml_key(k)}", v, lines)


def _dump_toml(data: dict) -> str:
    """Minimal TOML writer for the overlay: top-level scalars first, then [section] tables (nested tables
    emitted as dotted sub-tables). Sufficient for the settings the dashboard persists (no nested-table
    arrays, no datetimes)."""
    top = {k: v for k, v in data.items() if not isinstance(v, dict)}
    lines = ["# eiDOS machine-local settings — written by the dashboard Settings menu.",
             "# Overrides config.toml; safe to edit by hand or delete to reset to defaults.", ""]
    for k, v in top.items():
        lines.append(f"{k} = {_toml_scalar(v)}")
    if top:
        lines.append("")
    for sect, kv in data.items():
        if isinstance(kv, dict):
            _emit_table(sect, kv, lines)
    return "\n".join(lines).rstrip() + "\n"


def save_overrides(changes: dict, path: str = "config.toml") -> Path:
    """Merge `changes` (a {section: {key: value}} / top-level-key dict in config.toml's SHAPE) into the
    machine-local overlay and write it atomically. Returns the overlay path. The dashboard calls this
    from the Settings menu; nothing else writes config.local.toml."""
    local_path = Path(path).with_name(LOCAL_CONFIG_NAME)
    existing = {}
    if local_path.exists():
        try:
            with open(local_path, "rb") as f:
                existing = tomllib.load(f)
        except Exception:  # noqa: BLE001
            existing = {}
    _deep_merge(existing, changes or {})
    tmp = local_path.with_suffix(".toml.tmp")
    tmp.write_text(_dump_toml(existing), encoding="utf-8")
    os.replace(tmp, local_path)
    return local_path


@dataclasses.dataclass
class Config:
    # LLM
    llm_url: str = "http://127.0.0.1:8080"
    llm_model: str = "local"
    llm_temperature: float = 0.6
    llm_max_tokens: int = 1024
    llm_request_timeout_s: int = 300
    llm_top_p: float = 0.95
    llm_top_k: int = 20
    llm_min_p: float = 0.0
    llm_presence_penalty: float = 1.5
    llm_frequency_penalty: float = 0.4   # scales with token count → breaks degenerate repeat loops
    llm_repeat_penalty: float = 1.1      # llama.cpp n-gram repeat penalty (1.0 = off)
    # Per-model sampler profiles: {model_name: {temperature, top_p, top_k, min_p, presence_penalty,
    # frequency_penalty, repeat_penalty}}. When the active llm_model has an entry, those keys override
    # the base llm_* values for that model (each model's "best settings"). config [llm.profiles.<model>].
    llm_profiles: dict = dataclasses.field(default_factory=dict)
    llm_grammar_enabled: bool = True   # GBNF tick-output contract (BIBLE 2.1)

    # Tick
    tick_interval_s: int = 5              # idle cadence — sleep this long when there's no momentum
    tick_interval_active_s: float = 0.4   # active cadence — near-zero sleep when working (action taken
                                          # last tick, or background jobs still running). Adaptive: fast
                                          # when there's work, calm when idle (doc: multi-rate loop).
    loop_detect_window: int = 3

    # Compaction (dream/consolidation). token_threshold is now REAL tokens (should_compact divides
    # the observation byte-count by chars_per_token). Sized so the lived stream fills its share of a
    # 16k window without starving the head/recall/response — the old 8000 was compared to BYTES and
    # fired at ~2k tokens (constant amnesia). tick_threshold is the dry-spell backstop, raised so
    # content — not a fixed 20-tick clock — drives consolidation.
    compaction_token_threshold: int = 5000
    compaction_tick_threshold: int = 60
    compaction_max_tokens: int = 2048
    compaction_retry_max_tokens: int = 4096  # retry budget if thinking exhausts tokens

    # Output
    output_truncation_chars: int = 2000

    # Safety
    cmd_timeout_s: int = 120
    cmd_async_ceiling_s: float = 180.0  # hard kill for async/auto bg jobs
    bg_job_max_age_s: float = 1800.0    # generous lifetime cap for MANUAL bg_run jobs (kills runaways)
    disk_min_gb: float = 1.0
    ram_max_pct: float = 85.0
    bg_output_max_bytes: int = 10_000_000  # 10MB cap for bg_run output files
    protected_patterns: List[str] = dataclasses.field(default_factory=lambda: [
        r"rm\s+-rf\s+/",
        r"rm\s+.*-r",            # rm -r, rm -rf, rm -ri, etc.
        r"find\s+.*-exec\s+rm",  # find ... -exec rm
        r"find\s+.*-delete",     # find ... -delete
        r"systemctl\s+(stop|disable|kill)\s+.*eidos",
        r"pkill.*eidos",
        r"kill.*eidos",
        r"shutdown",
        r"reboot",
        r"halt",
        r"mkfs",
        r"dd\s+.*of=/dev/",
    ])

    # Self-healing
    llm_max_consecutive_failures: int = 5

    # Adaptive token management — for thinking models that may exhaust budget
    llm_max_tokens_ceiling: int = 4096       # hard upper limit for adaptive scaling
    llm_token_backoff_step: int = 512        # bump per reasoning exhaustion
    llm_reasoning_exhaust_compaction_trigger: int = 3  # force compaction after N consecutive

    # Rotation
    obs_max_lines: int = 5000
    obs_archive_days: int = 14
    llm_log_max_bytes: int = 5_000_000   # 5MB then rotate
    llm_log_archive_count: int = 3       # keep last N archives
    metrics_max_bytes: int = 2_000_000   # 2MB then rotate
    thoughts_max_bytes: int = 2_000_000  # thoughts.jsonl rotation threshold
    thoughts_archive_count: int = 2
    metrics_archive_count: int = 3       # keep last N metrics archives
    snapshot_max_count: int = 20         # keep last N memory snapshots

    # Context budgets (chars) — per-section limits for normal ticks
    context_obs_max_chars: int = 4000
    context_obs_max_count: int = 20
    context_goal_max_chars: int = 2000
    context_memory_max_chars: int = 4000
    context_plan_max_chars: int = 800           # briefing model: plan section budget
    context_subgoals_max_chars: int = 1500       # subgoals section budget
    context_intelligence_max_chars: int = 4000  # auto-recalled knowledge — this IS the continuity that
    #                                              repopulates working memory after a dream; 1200 (~300
    #                                              tokens) was starvation. Recall is remember-via-retrieval.
    context_env_max_chars: int = 800
    context_interventions_max_chars: int = 2000
    context_max_total_chars: int = 36000  # ~10-11k tokens (gemma ~3.3 char/tok on tool-trace): coherent
    #                                        backstop under the 16k window leaving ~5k for the response.
    # BIBLE §2.11 delta prompting: memoize the byte-stable KV head (identity/self-guide/skills/learned/
    # mission) so it is re-RENDERED only when one of its source files actually changes — the per-tick
    # work becomes the deltas, not re-reading + re-truncating the whole prefix. Kill-switch (default on).
    context_stable_head_cache: bool = True
    dream_combined: bool = True    # combined plan+extract in one LLM call (Phase 4)

    # Compaction context budgets (chars) — generous for distillation
    compaction_obs_max_chars: int = 16000
    compaction_memory_max_chars: int = 6000
    compaction_context_max_chars: int = 40000  # ~11400 tokens — room for full distillation

    # Token estimation
    chars_per_token: float = 3.5  # rough estimate for Qwen3

    # Paths
    workspace_dir: str = "workspace"

    # Persona
    persona_enabled: bool = True

    # Dashboard
    dashboard_port: int = 8099
    voice_port: int = 8098      # standalone voice service (phase 8.3): TTS + GPU speech-gate

    # Knowledge store
    knowledge_enabled: bool = True
    knowledge_dedup_threshold: float = 0.65      # store-time near-dup overlap threshold
    knowledge_recall_top_k: int = 3         # entries auto-surfaced per tick
    knowledge_recall_max_chars: int = 1200  # budget for Intelligence section
    knowledge_embedding_enabled: bool = False   # Phase 5: semantic search
    knowledge_embedding_cohost: bool = False    # keep model in RAM between dream cycles
    embedding_model_dir: str = "models/all-MiniLM-L6-v2"
    # HTTP embedding backend (Sprinter: a resident llama.cpp --embedding server in spare VRAM). When
    # embedding_endpoint is set, embed_texts POSTs to {endpoint}/v1/embeddings instead of the ONNX
    # path — the robust route on a Blackwell GPU where onnxruntime's CUDA EP is a gamble but the
    # CUDA-built llama.cpp already serves the mind. Empty endpoint → ONNX/mock, unchanged.
    embedding_endpoint: str = ""                 # e.g. "http://127.0.0.1:8082"
    embedding_model: str = "nomic-embed"         # payload "model" field (llama-server ignores it)
    embedding_query_prefix: str = ""             # nomic wants "search_query: " on queries
    embedding_doc_prefix: str = ""               # ...and "search_document: " on stored documents

    # Planning model (hot-swap for subgoal generation)

    # Mock mode
    mock_mode: bool = False

    # --- Creature mode (V3): the undisturbed-creature experiment. Swaps in SYSTEM_PROMPT_CREATURE,
    #     drops the task/objective/mission scaffolding, and runs without an assigned goal. ---
    creature_mode: bool = False
    # The creature's shell. "wsl" runs its bash through WSL2 (real Linux: ls/grep/find/cat/sed, UTF-8,
    # forward-slash paths) — working WITH the model's bash fluency instead of translating it to
    # PowerShell. "powershell" keeps the Windows shell + the dialect lints. Creature-only; the house-AI
    # eiDOS always uses PowerShell (it manages Windows services). The source firewall covers both shells.
    creature_shell: str = "wsl"
    creature_wsl_distro: str = "Ubuntu-24.04"   # must auto-mount /mnt/c (the default distro may not)

    # --- Self-improvement subsystem (self-guide, listening hold, git safety, self-edit) ---
    self_guide_enabled: bool = True
    world_state_max_items: int = 12              # world-model panel entries shown per tick
    context_notebook_max_chars: int = 1200       # open-notebook panel budget
    context_self_guide_max_chars: int = 1200   # budget injected into context each tick
    self_guide_max_bytes: int = 6000           # cap on the self_guide.md file itself
    chat_hold_ttl_s: float = 60.0              # listening hold freshness (cooperative client)
    chat_hold_max_continuous_s: float = 300.0  # hard ceiling so a stuck hold can't pin the loop
    git_safety_enabled: bool = True
    git_checkpoint_keep: int = 30              # prune to last N eidos-good-* tags
    self_edit_enabled: bool = False            # gated self-code-editing (opt-in)
    self_edit_max_proposal_bytes: int = 200000
    skill_sandbox_enabled: bool = True         # M2: forbid eval/exec/compile/__import__ in skill source;
    #                                            set false to "set it free" (full coding-agent freedom)
    self_edit_health_probe_s: int = 90         # post-restart health window before auto-rollback
    eidos_stuck_threshold_s: int = 600         # watchdog restarts eidos if alive but not ticking this long
    dashboard_token: str = ""                  # shared token gating state-changing POSTs ('' = off)

    # --- Delegate (hand long-horizon tasks to the pi coding agent as a background job) ---
    delegate_enabled: bool = False             # config.toml flips this on
    delegate_timeout_s: float = 600.0          # watchdog ceiling for one delegate run
    delegate_allowed_dirs: List[str] = dataclasses.field(default_factory=list)  # extra cwd roots
    delegate_max_sessions: int = 12            # retained job sandboxes (oldest pruned at dispatch)
    delegate_pi_path: str = ""                 # '' = resolve 'pi' from PATH
    delegate_pi_provider: str = "house"        # pi provider name (~/.pi/agent/models.json → llama-swap :8080)
    delegate_pi_model: str = "gemma4-12b"

    # --- Code IDE (browser GUI over the pi coding agent — interactive pi --mode rpc) ---
    ide_enabled: bool = True
    ide_port: int = 8100
    ide_pi_provider: str = "house-tap"
    ide_pi_model: str = "house-ai"
    ide_max_stints: int = 8                     # concurrent live pi rpc processes
    ide_stint_idle_timeout_s: float = 1800.0    # close a quiet stint after this

    # --- Nervous system (V3 afferent bus — P0 the seam; EIDOS_V3_ARCHITECTURE.md) ---
    nervous_enabled: bool = True
    nervous_transport: str = "inproc"           # "inproc" | "zmq" (the deployment-manifest switch, I9)
    nervous_bind: str = "tcp://0.0.0.0:8120"     # zmq: this bus binds here
    nervous_peer: str = "tcp://127.0.0.1:8120"   # zmq: connect to a peer here (loopback = cross-device proxy)
    nervous_schema_version: int = 1
    nervous_fungible_qsize: int = 256            # per-subscriber bounded queue (voice.py maxsize generalization)
    nervous_ordered_seq_max_buffered: int = 512  # ordered staging cap before atomic abort
    nervous_reliable_backpressure_max_s: float = 30.0   # liveness cap → drop+log+alarm (never wedge, ARCH #2)
    nervous_ordered_backpressure_max_s: float = 10.0
    nervous_payload_store_max_bytes: int = 67_108_864   # 64 MB content-addressed store cap
    nervous_payload_inline_max_bytes: int = 65_536      # <= this ships inline; larger is fetched by ref
    nervous_admits_per_source_per_window: int = 1000    # I10 fair-admission token bucket
    nervous_admission_window_s: float = 1.0
    nervous_heartbeat_interval_s: float = 0.5           # the trivial sense cadence
    nervous_context_max_chars: int = 1500               # P3: per-tick afferent block budget (volatile tail)
    nervous_context_max_events: int = 12                # P3: max admitted events rendered into context per tick
    nervous_interoception_enabled: bool = True          # P1a: the first organ (host telemetry → felt bars)
    nervous_interoception_interval_s: float = 5.0       # P1a: interoception sampling cadence
    nervous_drop_log_name: str = "drop_events.jsonl"
    nervous_metrics_log_name: str = "nervous_metrics.jsonl"
    nervous_gpu_leases_log_name: str = "gpu_leases.jsonl"   # P2: GPU arbiter grant/preempt/reclaim log
    nervous_monitor_enabled: bool = True            # the "behind the curtain" nervous-system snapshot for the dashboard
    nervous_monitor_interval_s: float = 1.0         # how often the monitor writes its snapshot
    nervous_monitor_feed_max: int = 48              # rolling event-feed length carried in the snapshot
    nervous_snapshot_name: str = "nervous_snapshot.json"
    nervous_learning_enabled: bool = True           # the dopaminergic reward-learning keystone (learn from outcomes over time)
    nervous_learning_sleep_interval_s: float = 10.0  # how often the sleep cycle checks whether to dream
    nervous_learning_sleep_arousal: float = 0.32     # consolidate (dream) when arousal is at/below this (calm)
    nervous_learning_consolidate_interval_s: float = 120.0  # but dream at most this often (throttle)
    # Ventral Striatum: incompletion/regret pressure → a bounded arousal floor (initiative when idle —
    # an unfinished objective keeps the creature awake/acting instead of drowsing). Relieved by progress.
    nervous_goaltension_enabled: bool = True
    # DMN: the slow personality drift (initiative/persistence/caution), learned from this creature's own
    # success/failure/override history; feeds the gate's park threshold + the goal-tension itch.
    nervous_temperament_enabled: bool = True
    # M0: metabolism — the energy economy. Thinking drains the reserve; hunger is felt; when arousal
    # collapses to torpor the creature rests + recovers (hibernation, not death). The stakes that make
    # inaction costly so the creature acts like an organism instead of ruminating.
    nervous_metabolism_enabled: bool = True
    nervous_metabolism_rest_arousal: float = 0.2     # at/below this arousal the creature is resting (low-power dormancy)
    # Post-pivot (2026-06-20): food = literal battery power. archetype "plant" = recharges from
    # environmental power (solar) only; "animal" = also recharges by resting/docking. This node is a
    # stationary solar-powered desktop → plant. Real power source = the Renogy Rover BLE (SOC + PV
    # watts); until that reader exists, a plant uses the solar_charge_in() daylight placeholder.
    nervous_metabolism_archetype: str = "plant"
    nervous_metabolism_solar_enabled: bool = True    # interim solar daylight curve (plant); off once Renogy is wired
    nervous_metabolism_solar_peak: float = 0.03      # per-tick charge at solar noon
    nervous_metabolism_solar_sunrise_h: float = 6.0  # local-hour daylight window (placeholder; PV reading replaces it)
    nervous_metabolism_solar_sunset_h: float = 20.0
    # M4 real power — the Renogy MPPT over BLE (the real food source; replaces the solar placeholder).
    # Default OFF: enabling it makes the creature poll Bluetooth. This node opts in via config.toml.
    # Self-healing: when Dean's Renogy phone app holds the single BLE link, reads fail-open + back off
    # + the reserve falls back to the internal sim, then re-anchors when the device is free again.
    power_enabled: bool = False
    power_mppt_address: str = ""                      # e.g. "C4:64:E3:53:D9:00" (BT-TH-… charge controller)
    power_device_id: int = 255                        # modbus id (device answers as 1; 255 broadcast works)
    power_poll_interval_s: float = 60.0
    power_stale_after_s: float = 600.0                # after this with no read, the feed is STALE (sim takes over)
    power_backoff_max_s: float = 600.0                # cap on exponential backoff while the device is busy
    power_battery_cells: int = 8                      # LiFePO4 8S = 24V nominal
    power_battery_capacity_ah: float = 100.0          # 24V 100Ah ≈ 2.56 kWh (for Wh framing)
    power_battery_r_internal: float = 0.015           # ohms, for the resting-voltage correction

    # --- Pillars roadmap (PILLARS_PLAN.md / PILLARS_TODO.md). Every feature ships DARK behind its
    # flag; a flag flips ON only after that phase's gate passes. See PILLARS_TODO.md. ---
    # Phase 0
    pillars_causal_ledger_enabled: bool = False       # 0.3 per-tick pressure-field log (pressures.py)
    pillars_causal_ledger_max_bytes: int = 8_000_000  # rotate the ledger to state/ at this size
    pillars_backup_enabled: bool = False              # 0.4 workspace snapshot/restore (backup.py)
    pillars_backup_daily_keep: int = 14               # rotation: daily snapshots retained
    pillars_backup_weekly_keep: int = 8               # rotation: weekly snapshots retained
    # Phase 1
    pillars_killable_skills_enabled: bool = False      # 1.2 subprocess-isolated, hard-killable skills
    pillars_skill_timeout_floor_s: float = 5.0         # derived timeout = p95*3, clamped to [floor, ceiling]
    pillars_skill_timeout_ceiling_s: float = 60.0
    # Phase 2 — the memory core (the engram economy)
    pillars_memory_engram_enabled: bool = False        # 2.1 the engram + hot/episodic/long-term stores (engram.py; a LIBRARY until 2.2 wires it)
    pillars_memory_manager_enabled: bool = False       # 2.2 the manager: store importer + 4-layer recall cascade (memory_manager.py)
    pillars_recall_explore_ratio: float = 0.15         # declared: fraction of a recall set reserved for a low-strength sample slot (anti-Matthew, plan §6)
    pillars_recall_recency_enabled: bool = False       # recall ranking tilts toward the present (floored engram.recency_factor over the cascade + BM25)
    pillars_encode_salience_enabled: bool = False      # §M-1 arousal-modulated encoding: the emotional stamp SEEDS birth strength (a flat tick fades first)
    pillars_sleep_engine_enabled: bool = False         # 2.4 real sleep engine: job-list consolidation/decay/distillation (nervous/sleep.py)
    pillars_max_wake_hours: float = 18.0               # declared: adenosine cap — past this, sleep-pressure overrides all drive floors (pitfall #2)
    pillars_expectations_enabled: bool = False         # 4.1 expectation ledger: typed open predictions closed by glue → surprise (expectations.py)
    pillars_max_open_predictions: int = 12             # declared: bound on simultaneously-open predictions (no unbounded growth)
    pillars_salience_gate_enabled: bool = False        # 1.3 salience-gate organ: admission bias = salience × relevance × neuromod gain (nervous/salience.py)
    pillars_bet_ledger_enabled: bool = False           # 2.3 recall-utility loop: every injected engram is a bet settled by glue (bets.py; decision #5)
    pillars_learning_xp_enabled: bool = False          # 4.2 XP = learning-progress-weighted adjudicated success — falling error slope pays, noise/mastery pay ~0 (decision #5b)
    pillars_news_enabled: bool = False                 # 4.4 news queue: deferred-communication store, presence-gated, engagement-ranked (news.py)
    pillars_news_max_items: int = 20                   # declared: bound on queued news items (expiry + eviction past this; no unbounded growth)
    pillars_mastery_gates_enabled: bool = False        # 4.3 levels = glue-checked mastery evidence (trusted skills/calibration/reuse/sleep cycles), XP just the progress bar (level_gates.py)
    pillars_min_sleeps_per_level: int = 3              # declared: mandatory digestion between levels (spacing effect as a hard floor; early levels take days by design)
    pillars_administrator_enabled: bool = False        # 5.2 the System-LLM behind the voice: dossier → grammar-constrained quest/weakness proposals, event-driven check-ins (administrator.py)
    pillars_administrator_autonomy: str = "earned"     # 5.2 quest auto-issue: "earned" = the graduated ladder (≥80% approval over ≥5 decisions/tier); "full" = a STANDING operator grant — every valid, leak-free proposal auto-issues (revoke stays the ban-hammer; locked-tool leaks still pend)
    # Phase 6/7 — the capability extensions (NOT biomimetic): shadows & generals
    pillars_shadows_enabled: bool = False              # 6 scripted CPU workers: trusted skill + event loop + budget + dead-man lease (shadow.py)
    pillars_shadow_capacity: int = 1                   # declared: concurrent shadow slots at unlock — capacity grows on demonstrated stewardship, not level alone (§6)
    pillars_generals_enabled: bool = False             # 7 delegated LLM minds on mission contracts (missions.py)
    pillars_max_generals: int = 5                      # DERIVED (0.5 spike, 2026-07-03): empirical ceiling 8 slots @8k on 16GB − 2 headroom − the mind's slot; ~45 tok/s/slot at 6-way
    # Phase 3 — skill economy (from library to language)
    pillars_skill_affordances_enabled: bool = False    # 3.1 surface top-K situation-relevant skills at the decision point
    pillars_skill_affordance_k: int = 3                # declared: how many affordances to surface
    pillars_skill_economy_enabled: bool = False        # 3.2 similarity-priced authoring + reuse-favoring XP + auto-retire
    pillars_skill_author_energy_cost: float = 0.02     # declared: metabolic cost of authoring a FULLY-NOVEL skill (scaled by similarity)
    pillars_skill_retire_unused_days: float = 30.0     # declared: archive skills unused this long (recoverable via rollback)
    pillars_skill_composition_enabled: bool = False    # 3.3 the `call` atom (skill→skill) + promotion-to-atom (depth cap, shared budget, static cycle check)
    # Phase 5 — the System (quests)
    pillars_quests_enabled: bool = False               # 5.1 quest engine (issue/track/adjudicate; one active quest)
    # Phase 5.x — the tool-progression ladder (TOOL_PROGRESSION.md / CREATURE_GENETICS.md)
    pillars_tool_unlocks_enabled: bool = False         # unit grants + visible_tools accessor + infant nap curve + stage-expressed alleles (unlocks.py); a locked tool does not exist (§0)
    pillars_commission_enabled: bool = False           # COMMISSION_PLAN.md: the standing-order organ — brief/todo/verdict settlement (commission.py)

    @property
    def workspace(self) -> Path:
        return Path(self.workspace_dir)

    @property
    def goal_path(self) -> Path:
        return self.workspace / "goal.md"

    @property
    def plan_path(self) -> Path:
        return self.workspace / "plan.md"

    @property
    def observations_path(self) -> Path:
        return self.workspace / "observations.jsonl"

    @property
    def wal_path(self) -> Path:
        return self.workspace / "wal.json"

    @property
    def interventions_dir(self) -> Path:
        return self.workspace / "interventions"

    @property
    def snapshots_dir(self) -> Path:
        return self.workspace / "snapshots"

    @property
    def outputs_dir(self) -> Path:
        return self.workspace / "outputs"

    @property
    def jobs_path(self) -> Path:
        return self.workspace / "jobs.json"

    @property
    def knowledge_dir(self) -> Path:
        return self.workspace / "knowledge"

    @property
    def knowledge_index_path(self) -> Path:
        return self.knowledge_dir / "index.json"

    # --- Self-improvement subsystem paths ---
    @property
    def state_dir(self) -> Path:
        """Lifecycle/marker state the dashboard owns (pause, holds, self-edit markers)."""
        return self.workspace / "state"

    @property
    def chat_hold_path(self) -> Path:
        return self.state_dir / "chat_hold.json"

    @property
    def power_cache_path(self) -> Path:
        """Shared latest-power reading: the always-on dashboard polls the Renogy MPPT and writes here;
        eidos and the behind-the-curtain panel read it (so battery/solar is live even when eidos is
        stopped). In state_dir → skeleton, so the creature never reads the raw file (it feels it via the bus)."""
        return self.state_dir / "power_latest.json"

    @property
    def battery_profile_path(self) -> Path:
        """The learned battery model (true v_full/v_empty/capacity, fused SOC). Lives OUTSIDE the
        workspace — at the repo root, not under workspace/ — so a creature wipe never erases hardware
        knowledge that took weeks of observation to learn. Gitignored runtime data."""
        return Path(self.workspace_dir).parent / "battery_profile.json"

    @property
    def self_guide_path(self) -> Path:
        return self.workspace / "self_guide.md"

    @property
    def self_guide_proposed_path(self) -> Path:
        return self.workspace / "self_guide_proposed.md"

    @property
    def self_guide_proposals_path(self) -> Path:
        return self.workspace / "self_guide_proposals.jsonl"

    @property
    def proposals_dir(self) -> Path:
        """Where eiDOS stages self-edit / skill proposals for operator approval."""
        return self.workspace / "proposals"

    # --- Nervous-system log paths (under state_dir, the glue.py/outcomes.jsonl convention) ---
    @property
    def nervous_drop_log_path(self) -> Path:
        return self.state_dir / self.nervous_drop_log_name

    @property
    def nervous_metrics_log_path(self) -> Path:
        return self.state_dir / self.nervous_metrics_log_name

    @property
    def nervous_gpu_leases_log_path(self) -> Path:
        return self.state_dir / self.nervous_gpu_leases_log_name

    @property
    def nervous_snapshot_path(self) -> Path:
        """The 'behind the curtain' nervous-system snapshot the monitor writes + the dashboard serves."""
        return self.state_dir / self.nervous_snapshot_name


# Sampler keys that can be tuned per-model via [llm.profiles.<model>] (each model's "best settings").
SAMPLER_KEYS = ("temperature", "top_p", "top_k", "min_p",
                "presence_penalty", "frequency_penalty", "repeat_penalty")


def active_sampler(config: "Config", model: str = None) -> dict:
    """Effective sampler settings for `model` (default: the active llm_model): the base llm_* values
    with that model's profile (config [llm.profiles.<model>]) overlaid on top. Single source of truth
    for 'which numbers does this model actually run with', used by llm.py and the Settings UI."""
    model = model or config.llm_model
    base = {k: getattr(config, "llm_" + k) for k in SAMPLER_KEYS}
    prof = (getattr(config, "llm_profiles", None) or {}).get(model, {})
    for k in SAMPLER_KEYS:
        v = prof.get(k)
        if v is not None:
            base[k] = v
    return base


def load_config(path: str = "config.toml") -> Config:
    """Load config from TOML file, then apply env var overrides."""
    config = Config()

    # Load TOML if it exists
    config_path = Path(path)
    data = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    # Settings-UI / installer overlay: config.local.toml (gitignored) overrides the committed,
    # hand-commented config.toml — so the dashboard's Settings menu never rewrites the base file, and
    # a fresh install can ship ONLY this overlay (no base config.toml needed).
    local_path = config_path.with_name(LOCAL_CONFIG_NAME)
    if local_path.exists():
        try:
            with open(local_path, "rb") as f:
                _deep_merge(data, tomllib.load(f))
        except Exception:  # noqa: BLE001 - a corrupt overlay must never block boot
            pass
    validate_config_document(data, str(config_path))
    if data:
        config.creature_mode = data.get("creature_mode", config.creature_mode)
        config.creature_shell = data.get("creature_shell", config.creature_shell)
        config.creature_wsl_distro = data.get("creature_wsl_distro", config.creature_wsl_distro)

        llm = data.get("llm", {})
        config.llm_url = llm.get("url", config.llm_url)
        config.llm_model = llm.get("model", config.llm_model)
        config.llm_temperature = llm.get("temperature", config.llm_temperature)
        config.llm_max_tokens = llm.get("max_tokens", config.llm_max_tokens)
        config.llm_request_timeout_s = llm.get("request_timeout_s", config.llm_request_timeout_s)
        config.llm_top_p = llm.get("top_p", config.llm_top_p)
        config.llm_top_k = llm.get("top_k", config.llm_top_k)
        config.llm_min_p = llm.get("min_p", config.llm_min_p)
        config.llm_presence_penalty = llm.get("presence_penalty", config.llm_presence_penalty)
        config.llm_frequency_penalty = llm.get("frequency_penalty", config.llm_frequency_penalty)
        config.llm_repeat_penalty = llm.get("repeat_penalty", config.llm_repeat_penalty)
        # Per-model sampler overrides live under [llm.profiles.<model>]; keep only mapping values so a
        # stray scalar can't poison the merge in llm.py / active_sampler().
        config.llm_profiles = {m: dict(p) for m, p in (llm.get("profiles") or {}).items()
                               if isinstance(p, dict)}
        config.llm_grammar_enabled = llm.get("grammar_enabled", config.llm_grammar_enabled)

        tick = data.get("tick", {})
        config.tick_interval_s = tick.get("interval_s", config.tick_interval_s)
        config.tick_interval_active_s = tick.get("interval_active_s", config.tick_interval_active_s)
        config.loop_detect_window = tick.get("loop_detect_window", config.loop_detect_window)

        comp = data.get("compaction", {})
        config.compaction_token_threshold = comp.get("token_threshold", config.compaction_token_threshold)
        config.compaction_tick_threshold = comp.get("tick_threshold", config.compaction_tick_threshold)
        config.compaction_max_tokens = comp.get("max_tokens", config.compaction_max_tokens)
        config.compaction_retry_max_tokens = comp.get("retry_max_tokens", config.compaction_retry_max_tokens)

        out = data.get("output", {})
        config.output_truncation_chars = out.get("truncation_chars", config.output_truncation_chars)

        safety = data.get("safety", {})
        config.cmd_timeout_s = safety.get("cmd_timeout_s", config.cmd_timeout_s)
        config.cmd_async_ceiling_s = safety.get("cmd_async_ceiling_s", config.cmd_async_ceiling_s)
        config.bg_job_max_age_s = safety.get("bg_job_max_age_s", config.bg_job_max_age_s)
        config.disk_min_gb = safety.get("disk_min_gb", config.disk_min_gb)
        config.ram_max_pct = safety.get("ram_max_pct", config.ram_max_pct)
        config.bg_output_max_bytes = safety.get("bg_output_max_bytes", config.bg_output_max_bytes)
        if "protected_patterns" in safety:
            config.protected_patterns = safety["protected_patterns"]

        healing = data.get("self_healing", {})
        config.llm_max_consecutive_failures = healing.get(
            "max_consecutive_failures", config.llm_max_consecutive_failures)
        config.llm_max_tokens_ceiling = healing.get(
            "max_tokens_ceiling", config.llm_max_tokens_ceiling)
        config.llm_token_backoff_step = healing.get(
            "token_backoff_step", config.llm_token_backoff_step)
        config.llm_reasoning_exhaust_compaction_trigger = healing.get(
            "reasoning_exhaust_compaction_trigger",
            config.llm_reasoning_exhaust_compaction_trigger)

        rot = data.get("rotation", {})
        config.obs_max_lines = rot.get("obs_max_lines", config.obs_max_lines)
        config.obs_archive_days = rot.get("archive_days", config.obs_archive_days)
        config.llm_log_max_bytes = rot.get("llm_log_max_bytes", config.llm_log_max_bytes)
        config.llm_log_archive_count = rot.get("llm_log_archive_count", config.llm_log_archive_count)
        config.metrics_max_bytes = rot.get("metrics_max_bytes", config.metrics_max_bytes)
        config.thoughts_max_bytes = rot.get("thoughts_max_bytes", config.thoughts_max_bytes)
        config.thoughts_archive_count = rot.get("thoughts_archive_count", config.thoughts_archive_count)
        config.metrics_archive_count = rot.get("metrics_archive_count", config.metrics_archive_count)
        config.snapshot_max_count = rot.get("snapshot_max_count", config.snapshot_max_count)

        ctx = data.get("context", {})
        config.context_stable_head_cache = ctx.get("stable_head_cache", config.context_stable_head_cache)
        config.context_obs_max_chars = ctx.get("obs_max_chars", config.context_obs_max_chars)
        config.context_obs_max_count = ctx.get("obs_max_count", config.context_obs_max_count)
        config.context_goal_max_chars = ctx.get("goal_max_chars", config.context_goal_max_chars)
        config.context_memory_max_chars = ctx.get("memory_max_chars", config.context_memory_max_chars)
        config.context_plan_max_chars = ctx.get("plan_max_chars", config.context_plan_max_chars)
        config.context_subgoals_max_chars = ctx.get("subgoals_max_chars", config.context_subgoals_max_chars)
        config.context_intelligence_max_chars = ctx.get("intelligence_max_chars", config.context_intelligence_max_chars)
        config.context_env_max_chars = ctx.get("env_max_chars", config.context_env_max_chars)
        config.context_interventions_max_chars = ctx.get("interventions_max_chars", config.context_interventions_max_chars)
        config.context_max_total_chars = ctx.get("max_total_chars", config.context_max_total_chars)
        config.chars_per_token = ctx.get("chars_per_token", config.chars_per_token)
        config.dream_combined = ctx.get("dream_combined", config.dream_combined)

        comp_ctx = data.get("compaction", {})
        config.compaction_obs_max_chars = comp_ctx.get("obs_max_chars", config.compaction_obs_max_chars)
        config.compaction_memory_max_chars = comp_ctx.get("memory_max_chars", config.compaction_memory_max_chars)
        config.compaction_context_max_chars = comp_ctx.get("context_max_chars", config.compaction_context_max_chars)

        persona = data.get("persona", {})
        config.persona_enabled = persona.get("enabled", config.persona_enabled)

        dashboard = data.get("dashboard", {})
        config.dashboard_port = dashboard.get("port", config.dashboard_port)
        config.voice_port = dashboard.get("voice_port", config.voice_port)
        config.dashboard_token = dashboard.get("token", config.dashboard_token)

        si = data.get("self_improvement", {})
        config.self_guide_enabled = si.get("self_guide_enabled", config.self_guide_enabled)
        config.context_self_guide_max_chars = si.get("self_guide_max_chars_ctx", config.context_self_guide_max_chars)
        config.world_state_max_items = ctx.get("world_state_max_items", config.world_state_max_items)
        config.context_notebook_max_chars = ctx.get("notebook_max_chars", config.context_notebook_max_chars)
        config.self_guide_max_bytes = si.get("self_guide_max_bytes", config.self_guide_max_bytes)
        config.chat_hold_ttl_s = si.get("chat_hold_ttl_s", config.chat_hold_ttl_s)
        config.chat_hold_max_continuous_s = si.get("chat_hold_max_continuous_s", config.chat_hold_max_continuous_s)
        config.git_safety_enabled = si.get("git_safety_enabled", config.git_safety_enabled)
        config.git_checkpoint_keep = si.get("git_checkpoint_keep", config.git_checkpoint_keep)
        config.self_edit_enabled = si.get("self_edit_enabled", config.self_edit_enabled)
        config.self_edit_max_proposal_bytes = si.get("self_edit_max_proposal_bytes", config.self_edit_max_proposal_bytes)
        config.skill_sandbox_enabled = si.get("skill_sandbox_enabled", config.skill_sandbox_enabled)
        config.self_edit_health_probe_s = si.get("self_edit_health_probe_s", config.self_edit_health_probe_s)
        config.eidos_stuck_threshold_s = si.get("eidos_stuck_threshold_s", config.eidos_stuck_threshold_s)

        knowledge = data.get("knowledge", {})
        config.knowledge_enabled = knowledge.get("enabled", config.knowledge_enabled)
        config.knowledge_recall_top_k = knowledge.get("recall_top_k", config.knowledge_recall_top_k)
        config.knowledge_recall_max_chars = knowledge.get("recall_max_chars", config.knowledge_recall_max_chars)
        config.knowledge_embedding_enabled = knowledge.get("embedding_enabled", config.knowledge_embedding_enabled)
        config.knowledge_embedding_cohost = knowledge.get("embedding_cohost", config.knowledge_embedding_cohost)
        config.embedding_model_dir = knowledge.get("embedding_model_dir", config.embedding_model_dir)
        config.embedding_endpoint = knowledge.get("embedding_endpoint", config.embedding_endpoint)
        config.embedding_model = knowledge.get("embedding_model", config.embedding_model)
        config.embedding_query_prefix = knowledge.get("embedding_query_prefix", config.embedding_query_prefix)
        config.embedding_doc_prefix = knowledge.get("embedding_doc_prefix", config.embedding_doc_prefix)

        dlg = data.get("delegate", {})
        config.delegate_enabled = dlg.get("enabled", config.delegate_enabled)
        config.delegate_timeout_s = float(dlg.get("timeout_s", config.delegate_timeout_s))
        config.delegate_allowed_dirs = dlg.get("allowed_dirs", config.delegate_allowed_dirs)
        config.delegate_max_sessions = dlg.get("max_sessions", config.delegate_max_sessions)
        config.delegate_pi_path = dlg.get("pi_path", config.delegate_pi_path)
        config.delegate_pi_provider = dlg.get("pi_provider", config.delegate_pi_provider)
        config.delegate_pi_model = dlg.get("pi_model", config.delegate_pi_model)

        ide = data.get("ide", {})
        config.ide_enabled = ide.get("enabled", config.ide_enabled)
        config.ide_port = ide.get("port", config.ide_port)
        config.ide_pi_provider = ide.get("pi_provider", config.ide_pi_provider)
        config.ide_pi_model = ide.get("pi_model", config.ide_pi_model)
        config.ide_max_stints = ide.get("max_stints", config.ide_max_stints)
        config.ide_stint_idle_timeout_s = float(
            ide.get("stint_idle_timeout_s", config.ide_stint_idle_timeout_s))

        nervous = data.get("nervous", {})
        config.nervous_enabled = nervous.get("enabled", config.nervous_enabled)
        config.nervous_transport = nervous.get("transport", config.nervous_transport)
        config.nervous_bind = nervous.get("bind", config.nervous_bind)
        config.nervous_peer = nervous.get("peer", config.nervous_peer)
        config.nervous_schema_version = nervous.get("schema_version", config.nervous_schema_version)
        config.nervous_fungible_qsize = nervous.get("fungible_qsize", config.nervous_fungible_qsize)
        config.nervous_ordered_seq_max_buffered = nervous.get(
            "ordered_seq_max_buffered", config.nervous_ordered_seq_max_buffered)
        config.nervous_reliable_backpressure_max_s = float(nervous.get(
            "reliable_backpressure_max_s", config.nervous_reliable_backpressure_max_s))
        config.nervous_ordered_backpressure_max_s = float(nervous.get(
            "ordered_backpressure_max_s", config.nervous_ordered_backpressure_max_s))
        config.nervous_payload_store_max_bytes = nervous.get(
            "payload_store_max_bytes", config.nervous_payload_store_max_bytes)
        config.nervous_payload_inline_max_bytes = nervous.get(
            "payload_inline_max_bytes", config.nervous_payload_inline_max_bytes)
        config.nervous_admits_per_source_per_window = nervous.get(
            "admits_per_source_per_window", config.nervous_admits_per_source_per_window)
        config.nervous_admission_window_s = float(nervous.get(
            "admission_window_s", config.nervous_admission_window_s))
        config.nervous_heartbeat_interval_s = float(nervous.get(
            "heartbeat_interval_s", config.nervous_heartbeat_interval_s))
        config.nervous_context_max_chars = nervous.get("context_max_chars", config.nervous_context_max_chars)
        config.nervous_context_max_events = nervous.get("context_max_events", config.nervous_context_max_events)
        config.nervous_interoception_enabled = nervous.get("interoception_enabled", config.nervous_interoception_enabled)
        config.nervous_interoception_interval_s = float(nervous.get("interoception_interval_s", config.nervous_interoception_interval_s))
        config.nervous_drop_log_name = nervous.get("drop_log_name", config.nervous_drop_log_name)
        config.nervous_metrics_log_name = nervous.get("metrics_log_name", config.nervous_metrics_log_name)
        config.nervous_gpu_leases_log_name = nervous.get("gpu_leases_log_name", config.nervous_gpu_leases_log_name)
        config.nervous_monitor_enabled = nervous.get("monitor_enabled", config.nervous_monitor_enabled)
        config.nervous_monitor_interval_s = float(nervous.get("monitor_interval_s", config.nervous_monitor_interval_s))
        config.nervous_monitor_feed_max = nervous.get("monitor_feed_max", config.nervous_monitor_feed_max)
        config.nervous_snapshot_name = nervous.get("snapshot_name", config.nervous_snapshot_name)
        config.nervous_learning_enabled = nervous.get("learning_enabled", config.nervous_learning_enabled)
        config.nervous_learning_sleep_interval_s = float(nervous.get("learning_sleep_interval_s", config.nervous_learning_sleep_interval_s))
        config.nervous_learning_sleep_arousal = float(nervous.get("learning_sleep_arousal", config.nervous_learning_sleep_arousal))
        config.nervous_learning_consolidate_interval_s = float(nervous.get("learning_consolidate_interval_s", config.nervous_learning_consolidate_interval_s))
        config.nervous_goaltension_enabled = nervous.get("goaltension_enabled", config.nervous_goaltension_enabled)
        config.nervous_temperament_enabled = nervous.get("temperament_enabled", config.nervous_temperament_enabled)
        config.nervous_metabolism_enabled = nervous.get("metabolism_enabled", config.nervous_metabolism_enabled)
        config.nervous_metabolism_rest_arousal = float(nervous.get("metabolism_rest_arousal", config.nervous_metabolism_rest_arousal))
        config.nervous_metabolism_archetype = str(nervous.get("metabolism_archetype", config.nervous_metabolism_archetype))
        config.nervous_metabolism_solar_enabled = nervous.get("metabolism_solar_enabled", config.nervous_metabolism_solar_enabled)
        config.nervous_metabolism_solar_peak = float(nervous.get("metabolism_solar_peak", config.nervous_metabolism_solar_peak))
        config.nervous_metabolism_solar_sunrise_h = float(nervous.get("metabolism_solar_sunrise_h", config.nervous_metabolism_solar_sunrise_h))
        config.nervous_metabolism_solar_sunset_h = float(nervous.get("metabolism_solar_sunset_h", config.nervous_metabolism_solar_sunset_h))
        config.power_enabled = nervous.get("power_enabled", config.power_enabled)
        config.power_mppt_address = str(nervous.get("power_mppt_address", config.power_mppt_address))
        config.power_device_id = int(nervous.get("power_device_id", config.power_device_id))
        config.power_poll_interval_s = float(nervous.get("power_poll_interval_s", config.power_poll_interval_s))
        config.power_stale_after_s = float(nervous.get("power_stale_after_s", config.power_stale_after_s))
        config.power_backoff_max_s = float(nervous.get("power_backoff_max_s", config.power_backoff_max_s))
        config.power_battery_cells = int(nervous.get("power_battery_cells", config.power_battery_cells))
        config.power_battery_capacity_ah = float(nervous.get("power_battery_capacity_ah", config.power_battery_capacity_ah))
        config.power_battery_r_internal = float(nervous.get("power_battery_r_internal", config.power_battery_r_internal))

        pillars = data.get("pillars", {})
        config.pillars_causal_ledger_enabled = pillars.get("causal_ledger_enabled", config.pillars_causal_ledger_enabled)
        config.pillars_causal_ledger_max_bytes = int(pillars.get("causal_ledger_max_bytes", config.pillars_causal_ledger_max_bytes))
        config.pillars_backup_enabled = pillars.get("backup_enabled", config.pillars_backup_enabled)
        config.pillars_backup_daily_keep = int(pillars.get("backup_daily_keep", config.pillars_backup_daily_keep))
        config.pillars_backup_weekly_keep = int(pillars.get("backup_weekly_keep", config.pillars_backup_weekly_keep))
        config.pillars_killable_skills_enabled = pillars.get("killable_skills_enabled", config.pillars_killable_skills_enabled)
        config.pillars_skill_timeout_floor_s = float(pillars.get("skill_timeout_floor_s", config.pillars_skill_timeout_floor_s))
        config.pillars_skill_timeout_ceiling_s = float(pillars.get("skill_timeout_ceiling_s", config.pillars_skill_timeout_ceiling_s))
        config.pillars_memory_engram_enabled = pillars.get("memory_engram_enabled", config.pillars_memory_engram_enabled)
        config.pillars_memory_manager_enabled = pillars.get("memory_manager_enabled", config.pillars_memory_manager_enabled)
        config.pillars_recall_explore_ratio = float(pillars.get("recall_explore_ratio", config.pillars_recall_explore_ratio))
        config.pillars_recall_recency_enabled = pillars.get("recall_recency_enabled", config.pillars_recall_recency_enabled)
        config.pillars_encode_salience_enabled = pillars.get("encode_salience_enabled", config.pillars_encode_salience_enabled)
        config.pillars_sleep_engine_enabled = pillars.get("sleep_engine_enabled", config.pillars_sleep_engine_enabled)
        config.pillars_max_wake_hours = float(pillars.get("max_wake_hours", config.pillars_max_wake_hours))
        config.pillars_expectations_enabled = pillars.get("expectations_enabled", config.pillars_expectations_enabled)
        config.pillars_max_open_predictions = int(pillars.get("max_open_predictions", config.pillars_max_open_predictions))
        config.pillars_salience_gate_enabled = pillars.get("salience_gate_enabled", config.pillars_salience_gate_enabled)
        config.pillars_bet_ledger_enabled = pillars.get("bet_ledger_enabled", config.pillars_bet_ledger_enabled)
        config.pillars_learning_xp_enabled = pillars.get("learning_xp_enabled", config.pillars_learning_xp_enabled)
        config.pillars_news_enabled = pillars.get("news_enabled", config.pillars_news_enabled)
        config.pillars_news_max_items = int(pillars.get("news_max_items", config.pillars_news_max_items))
        config.pillars_mastery_gates_enabled = pillars.get("mastery_gates_enabled", config.pillars_mastery_gates_enabled)
        config.pillars_min_sleeps_per_level = int(pillars.get("min_sleeps_per_level", config.pillars_min_sleeps_per_level))
        config.pillars_administrator_enabled = pillars.get("administrator_enabled", config.pillars_administrator_enabled)
        _adm_auto = str(pillars.get("administrator_autonomy", config.pillars_administrator_autonomy)).strip().lower()
        config.pillars_administrator_autonomy = _adm_auto if _adm_auto in ("earned", "full") else "earned"
        config.pillars_shadows_enabled = pillars.get("shadows_enabled", config.pillars_shadows_enabled)
        config.pillars_shadow_capacity = int(pillars.get("shadow_capacity", config.pillars_shadow_capacity))
        config.pillars_generals_enabled = pillars.get("generals_enabled", config.pillars_generals_enabled)
        config.pillars_max_generals = int(pillars.get("max_generals", config.pillars_max_generals))
        config.pillars_skill_affordances_enabled = pillars.get("skill_affordances_enabled", config.pillars_skill_affordances_enabled)
        config.pillars_skill_affordance_k = int(pillars.get("skill_affordance_k", config.pillars_skill_affordance_k))
        config.pillars_skill_economy_enabled = pillars.get("skill_economy_enabled", config.pillars_skill_economy_enabled)
        config.pillars_skill_author_energy_cost = float(pillars.get("skill_author_energy_cost", config.pillars_skill_author_energy_cost))
        config.pillars_skill_retire_unused_days = float(pillars.get("skill_retire_unused_days", config.pillars_skill_retire_unused_days))
        config.pillars_skill_composition_enabled = pillars.get("skill_composition_enabled", config.pillars_skill_composition_enabled)
        config.pillars_quests_enabled = pillars.get("quests_enabled", config.pillars_quests_enabled)
        config.pillars_tool_unlocks_enabled = pillars.get("tool_unlocks_enabled", config.pillars_tool_unlocks_enabled)
        config.pillars_commission_enabled = pillars.get("commission_enabled", config.pillars_commission_enabled)

        paths = data.get("paths", {})
        config.workspace_dir = paths.get("workspace", config.workspace_dir)

    # Env var overrides (highest precedence)
    env = load_env_overrides()
    if env.llm_url:
        config.llm_url = env.llm_url
    if env.workspace:
        config.workspace_dir = env.workspace
    if env.mock:
        config.mock_mode = True
        config.tick_interval_s = 5

    # Resolve the workspace to an ABSOLUTE path: expand `~`, and anchor a relative path at the repo root
    # (not the process CWD) so `python dashboard.py` works from anywhere. Everything else (state_dir,
    # knowledge_dir, battery_profile_path, …) derives from this, so this one resolution makes the whole
    # tree portable.
    _ws = Path(os.path.expanduser(str(config.workspace_dir)))
    if not _ws.is_absolute():
        _ws = REPO_ROOT / _ws
    config.workspace_dir = str(_ws)

    validate_resolved_config(config)

    return config
