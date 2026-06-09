"""Configuration loading for eiDOS. TOML config + env var overrides."""

import dataclasses
import os
import sys
from pathlib import Path
from typing import List

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


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

    # Tick
    tick_interval_s: int = 5              # idle cadence — sleep this long when there's no momentum
    tick_interval_active_s: float = 0.4   # active cadence — near-zero sleep when working (action taken
                                          # last tick, or background jobs still running). Adaptive: fast
                                          # when there's work, calm when idle (doc: multi-rate loop).
    loop_detect_window: int = 3

    # Compaction
    compaction_token_threshold: int = 8000
    compaction_tick_threshold: int = 20
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
    llm_restart_cmd: str = ""       # e.g. "systemctl restart llama-server"
    llm_local_only: bool = False     # production: True — restart always means local llama-server
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
    metrics_archive_count: int = 3       # keep last N metrics archives
    snapshot_max_count: int = 20         # keep last N memory snapshots

    # Context budgets (chars) — per-section limits for normal ticks
    context_obs_max_chars: int = 4000
    context_obs_max_count: int = 20
    context_goal_max_chars: int = 2000
    context_memory_max_chars: int = 4000
    context_plan_max_chars: int = 800           # briefing model: plan section budget
    context_subgoals_max_chars: int = 1500       # subgoals section budget
    context_intelligence_max_chars: int = 1200  # briefing model: auto-recalled knowledge
    context_env_max_chars: int = 800
    context_interventions_max_chars: int = 2000
    context_max_total_chars: int = 20000  # test/dev default; production uses config.toml (6500)
    briefing_model: bool = False  # enable new context structure (Phase 2)
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

    # Knowledge store
    knowledge_enabled: bool = True
    knowledge_recall_top_k: int = 3         # entries auto-surfaced per tick
    knowledge_recall_max_chars: int = 1200  # budget for Intelligence section
    knowledge_embedding_enabled: bool = False   # Phase 5: semantic search
    knowledge_embedding_cohost: bool = False    # keep model in RAM between dream cycles
    embedding_model_dir: str = "models/all-MiniLM-L6-v2"

    # Planning model (hot-swap for subgoal generation)
    planning_model_path: str = "/home/ei/models/qwen3.5-4b-q4.gguf"
    planning_context_size: int = 4096
    planning_reasoning_budget: int = 512
    planning_max_tokens: int = 512

    # Mock mode
    mock_mode: bool = False

    # --- Self-improvement subsystem (self-guide, listening hold, git safety, self-edit) ---
    self_guide_enabled: bool = True
    context_self_guide_max_chars: int = 1200   # budget injected into context each tick
    self_guide_max_bytes: int = 6000           # cap on the self_guide.md file itself
    chat_hold_ttl_s: float = 60.0              # listening hold freshness (cooperative client)
    chat_hold_max_continuous_s: float = 300.0  # hard ceiling so a stuck hold can't pin the loop
    git_safety_enabled: bool = True
    git_self_branch: str = "eidos-self"        # self-edit commits land here, never pushed
    git_checkpoint_keep: int = 30              # prune to last N eidos-good-* tags
    self_edit_enabled: bool = False            # gated self-code-editing (opt-in)
    self_edit_max_proposal_bytes: int = 200000
    self_edit_health_probe_s: int = 90         # post-restart health window before auto-rollback
    dashboard_token: str = ""                  # shared token gating state-changing POSTs ('' = off)

    @property
    def workspace(self) -> Path:
        return Path(self.workspace_dir)

    @property
    def goal_path(self) -> Path:
        return self.workspace / "goal.md"

    @property
    def memory_path(self) -> Path:
        return self.workspace / "memory.md"

    @property
    def plan_path(self) -> Path:
        return self.workspace / "plan.md"

    @property
    def subgoals_path(self) -> Path:
        return self.workspace / "subgoals.md"

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


def load_config(path: str = "config.toml") -> Config:
    """Load config from TOML file, then apply env var overrides."""
    config = Config()

    # Load TOML if it exists
    config_path = Path(path)
    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)

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
        config.llm_restart_cmd = healing.get("restart_cmd", config.llm_restart_cmd)
        config.llm_local_only = healing.get("local_only", config.llm_local_only)
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
        config.metrics_archive_count = rot.get("metrics_archive_count", config.metrics_archive_count)
        config.snapshot_max_count = rot.get("snapshot_max_count", config.snapshot_max_count)

        ctx = data.get("context", {})
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
        config.briefing_model = ctx.get("briefing_model", config.briefing_model)
        config.dream_combined = ctx.get("dream_combined", config.dream_combined)

        comp_ctx = data.get("compaction", {})
        config.compaction_obs_max_chars = comp_ctx.get("obs_max_chars", config.compaction_obs_max_chars)
        config.compaction_memory_max_chars = comp_ctx.get("memory_max_chars", config.compaction_memory_max_chars)
        config.compaction_context_max_chars = comp_ctx.get("context_max_chars", config.compaction_context_max_chars)

        persona = data.get("persona", {})
        config.persona_enabled = persona.get("enabled", config.persona_enabled)

        dashboard = data.get("dashboard", {})
        config.dashboard_port = dashboard.get("port", config.dashboard_port)
        config.dashboard_token = dashboard.get("token", config.dashboard_token)

        si = data.get("self_improvement", {})
        config.self_guide_enabled = si.get("self_guide_enabled", config.self_guide_enabled)
        config.context_self_guide_max_chars = si.get("self_guide_max_chars_ctx", config.context_self_guide_max_chars)
        config.self_guide_max_bytes = si.get("self_guide_max_bytes", config.self_guide_max_bytes)
        config.chat_hold_ttl_s = si.get("chat_hold_ttl_s", config.chat_hold_ttl_s)
        config.chat_hold_max_continuous_s = si.get("chat_hold_max_continuous_s", config.chat_hold_max_continuous_s)
        config.git_safety_enabled = si.get("git_safety_enabled", config.git_safety_enabled)
        config.git_self_branch = si.get("git_self_branch", config.git_self_branch)
        config.git_checkpoint_keep = si.get("git_checkpoint_keep", config.git_checkpoint_keep)
        config.self_edit_enabled = si.get("self_edit_enabled", config.self_edit_enabled)
        config.self_edit_max_proposal_bytes = si.get("self_edit_max_proposal_bytes", config.self_edit_max_proposal_bytes)
        config.self_edit_health_probe_s = si.get("self_edit_health_probe_s", config.self_edit_health_probe_s)

        planning = data.get("planning", {})
        config.planning_model_path = planning.get("model_path", config.planning_model_path)
        config.planning_context_size = planning.get("context_size", config.planning_context_size)
        config.planning_reasoning_budget = planning.get("reasoning_budget", config.planning_reasoning_budget)
        config.planning_max_tokens = planning.get("max_tokens", config.planning_max_tokens)

        knowledge = data.get("knowledge", {})
        config.knowledge_enabled = knowledge.get("enabled", config.knowledge_enabled)
        config.knowledge_recall_top_k = knowledge.get("recall_top_k", config.knowledge_recall_top_k)
        config.knowledge_recall_max_chars = knowledge.get("recall_max_chars", config.knowledge_recall_max_chars)
        config.knowledge_embedding_enabled = knowledge.get("embedding_enabled", config.knowledge_embedding_enabled)
        config.knowledge_embedding_cohost = knowledge.get("embedding_cohost", config.knowledge_embedding_cohost)
        config.embedding_model_dir = knowledge.get("embedding_model_dir", config.embedding_model_dir)

        paths = data.get("paths", {})
        config.workspace_dir = paths.get("workspace", config.workspace_dir)

    # Env var overrides (highest precedence)
    if url := os.environ.get("EIDOS_LLM_URL"):
        config.llm_url = url
    if os.environ.get("EIDOS_MOCK") == "1":
        config.mock_mode = True
        config.tick_interval_s = 5

    return config
