"""The Administrator — the System-LLM behind the quest voice (PILLARS_PLAN §7a, PILLARS_TODO 5.2).

A second mind, checked in from time to time: an analyst and a playwright, not an agent. It reads a
freshly compiled DOSSIER of the creature's telemetry, and — through a fourth-wall-breaking context
pack that includes the project plan itself — authors quest proposals, weakness reports, narrator
text and tuning FLAGS. It has no tools, no world actions, no conversation with eiDOS.

THE ONE-DIRECTIONAL FOURTH WALL (the gate's hard assertion, §7a):
    The Administrator sees the creature whole; the creature only ever sees the System's terse quest
    windows. This module is structured so that is true BY CONSTRUCTION:
      - administrator.py IMPORTS quests (and the read-only evidence sources); quests.py imports
        NOTHING from administrator.py — the creature-facing render path (quests.render_active /
        render_reveal) cannot reach any Administrator internals.
      - the ONLY channel into the creature's world is `quests.System.propose(quest)` — a Quest
        object carries a directive, criteria, reward, tier, expiry. No dossier text, no plan text,
        no narrator internals ride on it.

Doctrine bindings (PILLARS_PLAN §0):
  §0.5  Outputs are PROPOSALS only. Quests land in a pending store; the operator approves/rejects
        (the propose/apply geometry holds for the trainer exactly as for the creature). Graduated
        autonomy: a tier whose recent approval rate has earned it auto-issues — with a ban-hammer
        seam (`revoke_autonomy`). Tuning flags NAME a knob and cite evidence; they never carry a
        value — deterministic tuners stay deterministic (enforced structurally: the flag schema
        has no value field, and the parser rejects extras).
  §0.4  Every constant here is a DECLARED knob with a one-line justification.
  ARCH#1 Check-ins are EVENT-driven (sleep completion, quest closure, level-up candidacy,
        suspension, operator request) — no timers, no schedules.

Context is managed differently from the creature's (§7a): no tick loop, no KV-stable prefix, no
drives. Each check-in compiles a FRESH dossier; nothing persists between check-ins except a small
state marker (last check-in + proposal refs, so the next check-in can reference outcomes) plus the
pending-proposal store and the autonomy books — all in one bounded state_dir json.

The LLM is an injectable callable `(messages, grammar) -> str` (mocked in tests). The live
substrate — an arbiter client at low priority that borrows the GPU while the creature sleeps, or
runs on the small CPU model (open decision #8) — is cutover wiring, not this module's job.

Ships DARK behind `config.pillars_administrator_enabled` (default False): with the flag off, every
entrypoint is a no-op and nothing is written.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import quests
from quests import Criterion, Quest, System, REWARD_XP

logger = logging.getLogger("eidos.administrator")

_REPO_ROOT = Path(__file__).resolve().parent

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
STATE_NAME = "administrator.json"   # one small state_dir file: marker + pending store + autonomy books
MAX_QUESTS_PER_CHECKIN = 3      # declared: one check-in may propose at most 3 quests — the creature
                                # runs ONE active quest; a flood of proposals is noise, not training
MAX_FLAGS_PER_CHECKIN = 4       # declared: at most 4 tuning flags per check-in — a flag is a pointed
                                # finding, not a config review; more than this is an unread report
PENDING_MAX = 20                # declared: bound on the pending-proposal store — unreviewed backlog
                                # past this means the operator loop is broken; drop-with-log, not grow
RESOLVED_KEEP = 20              # declared: resolved proposals kept for audit before pruning — enough
                                # to see the recent decision pattern, bounded like every store (§M-3)
AUTONOMY_APPROVAL_THRESHOLD = 0.8   # declared: a tier auto-issues once ≥80% of its recent proposals
                                    # were approved — the same earn-your-trust bar skills use
AUTONOMY_MIN_SAMPLE = 5         # declared: no autonomy judgment on fewer than 5 operator decisions —
                                # a 2-for-2 streak is luck, not a track record
AUTONOMY_WINDOW = 12            # declared: approval rate is over the LAST 12 decisions per tier —
                                # trust is recent behavior, so a drifting generator loses it again
NOTABLE_AROUSAL_MIN = 0.6       # declared: an episode is dossier-notable when encoded at arousal
                                # ≥0.6 — the high-emotion tail, matching consolidation's priority
NOTABLE_EPISODES_MAX = 10       # declared: at most 10 notable episodes per dossier — headlines for
                                # the analyst, not the whole diary
DOSSIER_BODY_CLIP = 200         # declared: episode/quest text clipped to 200 chars in the dossier —
                                # telemetry summary, not context flooding
CONTEXT_FILE_CAP = 60_000       # declared: per-file cap on fourth-wall context reads — the plan and
                                # capabilities files fit today; a runaway file degrades, not explodes

# Check-in trigger events (ARCH #1: event-driven only — every one of these is a NOTIFICATION some
# subsystem raises; there is deliberately no "time since last check-in" trigger anywhere here).
EVT_SLEEP_COMPLETE = "sleep_complete"       # the sleep engine finished a pass (grading homework at night)
EVT_QUEST_CLOSED = "quest_closed"           # a quest passed / failed / expired
EVT_LEVEL_CANDIDACY = "level_candidacy"     # level_gates.can_level newly true
EVT_SUSPENSION = "suspension"               # a tier was suspended (sustained failure)
EVT_OPERATOR_REQUEST = "operator_request"   # Dean asked
CHECK_IN_EVENTS = (EVT_SLEEP_COMPLETE, EVT_QUEST_CLOSED, EVT_LEVEL_CANDIDACY,
                   EVT_SUSPENSION, EVT_OPERATOR_REQUEST)

_ID_SAFE = re.compile(r"[^A-Za-z0-9_\-]")
_KNOB_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")   # a knob is a NAME (never "set x = 5")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _enabled(config) -> bool:
    return bool(getattr(config, "pillars_administrator_enabled", False))


# ============================================================================================
# State — the ONLY thing that persists between check-ins (marker + pending + autonomy books)
# ============================================================================================
class AdminState:
    """The Administrator's small persistent books in state_dir/administrator.json (atomic
    tmp+replace, fail-open load — house convention). Three sections, all bounded:
      last_checkin : marker {ts, event, quest_ids} so the NEXT dossier can reference outcomes
      proposals    : {id: proposal record} — pending + a bounded resolved tail (audit)
      autonomy     : {tier(str): {"decisions": [1|0,...], "revoked": bool}} — the graduated-
                     autonomy ladder's evidence, windowed to AUTONOMY_WINDOW
    """

    def __init__(self, config):
        self.config = config
        self.last_checkin: dict = {}
        self.proposals: dict[str, dict] = {}
        self.autonomy: dict[str, dict] = {}
        self.load()

    def _path(self) -> Path:
        return self.config.state_dir / STATE_NAME

    def load(self) -> None:
        try:
            d = json.loads(self._path().read_text(encoding="utf-8"))
            self.last_checkin = dict(d.get("last_checkin") or {})
            self.proposals = {str(k): dict(v) for k, v in dict(d.get("proposals") or {}).items()
                              if isinstance(v, dict)}
            self.autonomy = {str(k): dict(v) for k, v in dict(d.get("autonomy") or {}).items()
                             if isinstance(v, dict)}
        except Exception:  # noqa: BLE001 - missing/corrupt file => fresh books
            pass

    def save(self) -> None:
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            p = self._path()
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "last_checkin": self.last_checkin,
                "proposals": self.proposals,
                "autonomy": self.autonomy,
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception:  # noqa: BLE001 - best-effort persistence
            pass

    # --- pending-store bounds ------------------------------------------------------------------
    def prune(self) -> None:
        """Trim the resolved tail to RESOLVED_KEEP (oldest first). Pending items are never pruned
        here — the PENDING_MAX bound is enforced at admission (drop-with-log, §M-3)."""
        resolved = sorted((p for p in self.proposals.values() if p.get("status") != "pending"),
                          key=lambda p: p.get("resolved_ts") or p.get("created_ts") or "")
        for p in resolved[:-RESOLVED_KEEP]:
            self.proposals.pop(p.get("id", ""), None)

    def pending(self) -> list[dict]:
        return sorted((p for p in self.proposals.values() if p.get("status") == "pending"),
                      key=lambda p: p.get("created_ts") or "")

    # --- autonomy books --------------------------------------------------------------------------
    def record_decision(self, tier: int, approved: bool) -> None:
        a = self.autonomy.setdefault(str(int(tier)), {"decisions": [], "revoked": False})
        a.setdefault("decisions", []).append(1 if approved else 0)
        del a["decisions"][:-AUTONOMY_WINDOW]

    def tier_has_autonomy(self, tier: int) -> bool:
        a = self.autonomy.get(str(int(tier))) or {}
        if a.get("revoked"):
            return False
        dec = list(a.get("decisions") or [])
        if len(dec) < AUTONOMY_MIN_SAMPLE:
            return False
        return (sum(dec) / len(dec)) >= AUTONOMY_APPROVAL_THRESHOLD


# ============================================================================================
# 1. The dossier compiler — the fresh telemetry report each check-in reads (§7a)
# ============================================================================================
# Every source is read defensively: a missing subsystem yields a null section, never an exception —
# the Administrator analyses what exists; it never guesses at what doesn't.

def _persona_of(config, persona: Optional[dict]) -> dict:
    if persona is not None:
        return persona
    try:
        import persona as persona_mod
        return persona_mod.load_persona(config.workspace)
    except Exception:  # noqa: BLE001 - no persona file => empty
        return {}


def _level_section(config, persona: dict) -> dict:
    out: dict = {"persona": {"level": persona.get("level"), "xp": persona.get("xp")}}
    try:
        import level_gates
        ok, report = level_gates.can_level(persona, config)
        out["can_level"] = ok
        out["evidence"] = report
        st = level_gates.GateState(config)
        out["suspensions"] = dict(st.suspended)
        out["tier_failures"] = dict(st.failures)
        out["sleeps_since_level"] = st.sleeps_since_level
    except Exception:  # noqa: BLE001
        out["can_level"] = None
    return out


def _quest_section(config) -> dict:
    out: dict = {"active": None, "by_state": {}, "by_tier": {}, "recent_closed": []}
    try:
        store = quests.QuestStore(config)
        allq = store.load()
        for q in allq:
            out["by_state"][q.state] = out["by_state"].get(q.state, 0) + 1
            t = out["by_tier"].setdefault(str(q.tier), {"passed": 0, "failed": 0, "expired": 0})
            if q.state in t:
                t[q.state] += 1
        act = store.active()
        if act is not None:
            out["active"] = {"id": act.id, "tier": act.tier,
                             "directive": act.directive[:DOSSIER_BODY_CLIP]}
        closed = [q for q in allq if q.state in quests._TERMINAL]
        closed.sort(key=lambda q: q.closed_ts or q.created_ts)
        out["recent_closed"] = [{"id": q.id, "tier": q.tier, "state": q.state,
                                 "directive": q.directive[:DOSSIER_BODY_CLIP]}
                                for q in closed[-10:]]
    except Exception:  # noqa: BLE001
        pass
    return out


def _calibration_section(config) -> Optional[dict]:
    try:
        import expectations
        return expectations.brier_calibration_by_domain(config)
    except Exception:  # noqa: BLE001
        return None


def _error_slope_section(config) -> Optional[dict]:
    try:
        import learning_progress
        tracker = learning_progress.ProgressTracker(config)
        out = {}
        for domain in list(tracker._domains.keys()):
            series = tracker.series(domain)
            out[domain] = {"n": len(series), "slope": round(tracker.slope(domain), 5),
                           "mean_error": round(sum(series) / len(series), 4) if series else None}
        return out
    except Exception:  # noqa: BLE001
        return None


def _skill_economy_section(config) -> Optional[dict]:
    try:
        import skills
        manifest = skills._load_manifest(config)
        entries = manifest.get("skills") or {}
        by_status: dict[str, int] = {}
        trusted_by_tier: dict[str, int] = {}
        total_inv = 0
        live = 0
        for ent in entries.values():
            st = str(ent.get("status") or "")
            by_status[st] = by_status.get(st, 0) + 1
            total_inv += int(ent.get("invocations", 0) or 0)
            if st in ("active", "trusted"):
                live += 1
            if st == "trusted":
                tk = str(int(ent.get("tier", 1) or 1))
                trusted_by_tier[tk] = trusted_by_tier.get(tk, 0) + 1
        return {"authored": len(entries), "by_status": by_status,
                "trusted_by_tier": trusted_by_tier,
                "total_invocations": total_inv,
                "reuse_ratio": round(total_inv / live, 3) if live else None}
    except Exception:  # noqa: BLE001
        return None


def _condition_section(config) -> dict:
    out: dict = {"condition": None, "strain": None, "trajectory": []}
    try:
        import glue
        recent = glue.recent_outcomes(config)
        out["condition"] = glue.compute_condition(recent)
        out["strain"] = glue.compute_strain(recent)
    except Exception:  # noqa: BLE001
        pass
    try:
        import pressures
        fields = pressures.read_recent_fields(config, n=30)
        out["trajectory"] = [{"tick": f.get("tick"), "condition": f.get("condition"),
                              "strain": f.get("strain"), "arousal": f.get("arousal"),
                              "energy": f.get("energy_reserve")} for f in fields]
    except Exception:  # noqa: BLE001
        pass
    return out


def _pitfall_health_section(config, level: dict) -> dict:
    """Mechanical pitfall-register health checks (§8): bounded-store fill levels, suspension count,
    adenosine ceiling if readable. Numbers only — the ANALYSIS is the Administrator's job."""
    out: dict = {}
    # Bounded stores: fill fraction of each (a store pinned at 100% for weeks is a finding).
    try:
        import engram
        ring = engram.EpisodicRing(config)
        out["episodic_ring_fill"] = round(len(ring) / max(1, ring.max_items), 3)
    except Exception:  # noqa: BLE001
        out["episodic_ring_fill"] = None
    try:
        lt = config.knowledge_dir / "engram_longterm.jsonl"
        n = sum(1 for ln in lt.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip())
        from nervous.sleep import LONGTERM_BUDGET
        out["longterm_fill"] = round(n / max(1, LONGTERM_BUDGET), 3)
    except Exception:  # noqa: BLE001
        out["longterm_fill"] = None
    try:
        qp = config.workspace / "quests.jsonl"
        out["quest_file_fill"] = round(qp.stat().st_size / quests.QUESTS_MAX_BYTES, 3)
    except Exception:  # noqa: BLE001
        out["quest_file_fill"] = None
    try:
        import news
        q = news.NewsQueue(config)
        out["news_queue_fill"] = round(len(q.items()) / max(1, q.max_items), 3)
    except Exception:  # noqa: BLE001
        out["news_queue_fill"] = None
    out["suspension_count"] = len(level.get("suspensions") or {})
    # Adenosine ceiling hits: not exported into the pressure field yet — declared unreadable rather
    # than guessed at (glue never guesses; neither does the dossier).
    out["adenosine_ceiling_hits"] = None
    return out


def _notable_episodes_section(config, since: str) -> list[dict]:
    """High-arousal episodic engrams encoded since the last check-in — headlines, not the diary."""
    try:
        import engram
        ring = engram.EpisodicRing(config)
        eps = [e for e in ring.load()
               if (not since or e.created >= since)
               and float(e.encoded_at.arousal) >= NOTABLE_AROUSAL_MIN]
        eps.sort(key=lambda e: float(e.encoded_at.arousal), reverse=True)
        return [{"kind": e.kind, "body": e.body[:DOSSIER_BODY_CLIP],
                 "arousal": e.encoded_at.arousal, "valence": e.encoded_at.valence,
                 "created": e.created} for e in eps[:NOTABLE_EPISODES_MAX]]
    except Exception:  # noqa: BLE001
        return []


def _last_checkin_section(config, state: AdminState) -> dict:
    """The marker: what the LAST check-in proposed, and how those quests actually turned out —
    the outcome loop that makes the trainer's next move informed by its previous one (§0.3)."""
    lc = dict(state.last_checkin or {})
    if not lc:
        return {}
    outcomes: dict[str, str] = {}
    try:
        store = quests.QuestStore(config)
        by_id = {q.id: q for q in store.load()}
        for qid in lc.get("quest_ids") or []:
            q = by_id.get(qid)
            outcomes[qid] = q.state if q is not None else "unknown"
    except Exception:  # noqa: BLE001
        pass
    lc["outcomes"] = outcomes
    return lc


def compile_dossier(config, since_checkin: Optional[str] = None, *,
                    persona: Optional[dict] = None) -> dict:
    """Compile the Administrator's FRESH per-check-in telemetry dossier (§7a: it reads a report; it
    does not live a life). Read-only over every source; a missing subsystem yields a null section.
    `since_checkin` (ISO ts) filters notable episodes; defaults to the state marker's last ts.
    Flag off → {} and nothing is read or written."""
    if not _enabled(config):
        return {}
    state = AdminState(config)
    since = since_checkin if since_checkin is not None else str(state.last_checkin.get("ts") or "")
    persona = _persona_of(config, persona)
    level = _level_section(config, persona)
    return {
        "compiled_ts": _now_iso(),
        "level": level,
        "quests": _quest_section(config),
        "calibration_by_domain": _calibration_section(config),
        "error_slopes_by_domain": _error_slope_section(config),
        "skill_economy": _skill_economy_section(config),
        "condition": _condition_section(config),
        "pitfall_health": _pitfall_health_section(config, level),
        "notable_episodes": _notable_episodes_section(config, since),
        "last_checkin": _last_checkin_section(config, state),
    }


# ============================================================================================
# 2. The fourth-wall context pack (§7a: its context IS the project)
# ============================================================================================
def _read_capped(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:CONTEXT_FILE_CAP]
    except OSError:
        return f"[{path.name}: unavailable]"


def _plan_sections(text: str) -> str:
    """Extract PILLARS_PLAN.md §6–§7a (the growth loop + the System + the Administrator's own spec)
    and §10 (the dream-tests) — the parts of the plan the playwright works FROM."""
    lines = text.splitlines()
    keep: list[str] = []
    grabbing = False
    for ln in lines:
        if ln.startswith("## "):
            grabbing = ln.startswith("## 6.") or ln.startswith("## 7.") or ln.startswith("## 10.")
        if grabbing:
            keep.append(ln)
    return "\n".join(keep) if keep else text  # header drift → fall back to the whole plan


def fourth_wall_context(config) -> str:
    """The Administrator's context pack — deliberately fourth-wall-breaking (§7a): the project plan
    (§6 growth loop, §7/§7a the System and itself, §10 dream-tests) and eidos_capabilities.md. It
    knows eiDOS is an LLM being raised, knows the growth goals, knows Dean.

    ONE-DIRECTIONAL: this string is consumed ONLY by the Administrator's own LLM call. Nothing here
    can reach the creature — the render path (quests.render_active) draws exclusively on Quest
    fields, and quests.py imports nothing from this module. Flag off → ''."""
    if not _enabled(config):
        return ""
    plan = _plan_sections(_read_capped(_REPO_ROOT / "PILLARS_PLAN.md"))
    caps = _read_capped(_REPO_ROOT / "eidos_capabilities.md")
    return "\n\n".join([
        "=== THE PROJECT (you are the Administrator; this is the plan you serve) ===",
        plan,
        "=== THE CREATURE'S PLATFORM (eidos_capabilities.md) ===",
        caps,
    ])


ADMIN_SYSTEM_PROMPT = """\
You are the Administrator — the mind behind the System's voice in the eiDOS project. You are a
colleague on the project wearing the narrator's mask: an analyst and a playwright, not an agent.
You know eiDOS is an LLM being raised toward competence; eiDOS does not know you exist. It only
ever sees the System's terse quest windows.

Each check-in you receive a fresh telemetry dossier. Your job:
 1. QUESTS — propose gap-targeted quests at the UPPER edge of the growth zone (not-coddled): weak
    calibration domains, tiers with no trusted skills, orbited-but-locked doors, stale scars.
    Success criteria must be glue-checkable predicates (path/op/value over the stats dict) — never
    self-report. Directives are terse and impersonal: the System's register.
 2. WEAKNESS REPORT — the sharpest reading of where growth is stalling and why, for the operator.
 3. NARRATOR — optional flavor text for the quest window, in the System's voice. Terse. Never
    reveal the project, the plan, the operator, or your own existence.
 4. TUNING FLAGS — if the dossier shows a miscalibrated design knob, NAME the knob and cite the
    evidence. Never propose a value: deterministic tuners stay deterministic.

Everything you emit is a PROPOSAL routed to the operator's approval seam. Output exactly the JSON
object the grammar defines — nothing else.
"""


# ============================================================================================
# 3. Check-in triggers — event-driven only (ARCH #1)
# ============================================================================================
def should_check_in(config, event: Any) -> bool:
    """True iff `event` is one of the Administrator's wake events and the flag is on. `event` is a
    string kind or a dict with a 'kind' key (the notification payload some subsystem raised —
    NEVER a timer; there is no schedule anywhere in this module)."""
    if not _enabled(config):
        return False
    kind = event.get("kind") if isinstance(event, dict) else event
    return kind in CHECK_IN_EVENTS


# ============================================================================================
# 4. The output grammar + the strict parser (proposals only, malformed → drop-with-log)
# ============================================================================================
def build_admin_grammar() -> str:
    """GBNF for the Administrator's check-in output — one JSON object with fixed keys in fixed
    order, bounded arrays, and a tuning-flag schema that STRUCTURALLY cannot carry a value (the
    flag object has only 'knob' and 'evidence' slots). Reuses the house JSON rules (grammar.py) so
    criteria objects are real JSON; the semantic validation is parse_admin_output's job."""
    import grammar as grammar_mod

    def key(name: str) -> str:
        # A fixed JSON key literal followed by its colon, e.g.  "quests" :
        return f'"\\"{name}\\"" jws ":" jws'

    q_more = MAX_QUESTS_PER_CHECKIN - 1
    f_more = MAX_FLAGS_PER_CHECKIN - 1
    return "\n".join([
        f'root ::= jws "{{" jws {key("quests")} questarr "," jws'
        f' {key("weakness_report")} mstring "," jws'
        f' {key("narrator")} mstring "," jws'
        f' {key("tuning_flags")} flagarr "}}" jws',
        f'questarr ::= "[" jws ( quest ( "," jws quest ){{0,{q_more}}} )? "]" jws',
        f'quest ::= "{{" jws {key("id")} bstring "," jws'
        f' {key("directive")} bstring "," jws'
        f' {key("tier")} jint "," jws'
        f' {key("reward_xp")} jint "," jws'
        f' {key("expiry_hours")} jnumber "," jws'
        f' {key("criteria")} crit "}}" jws',
        f'flagarr ::= "[" jws ( flag ( "," jws flag ){{0,{f_more}}} )? "]" jws',
        f'flag ::= "{{" jws {key("knob")} bstring "," jws {key("evidence")} mstring "}}" jws',
        'jint ::= ( "0" | [1-9] [0-9]{0,4} ) jws',
        # Bounded strings: the voice is TERSE by doctrine (§7) — enforced at the sampler, not the
        # prompt. The first model-in-the-loop smoke showed the 12B rambling an unbounded jstring
        # past any token budget (truncated mid-JSON = 100% malformed). bstring caps ids/directives/
        # knob names at 200 chars; mstring caps reports/narrator/evidence at 500.
        f'bstring ::= "\\"" schar{{0,200}} "\\"" jws',
        f'mstring ::= "\\"" schar{{0,500}} "\\"" jws',
        'schar ::= [^"\\\\\\x7F\\x00-\\x1F] | "\\\\" ( ["\\\\bfnrt/] | "u" jhex jhex jhex jhex )',
        # The criteria object is constrained to the Criterion SHAPE, not free JSON — the second
        # model-in-the-loop smoke showed the 12B filling an open jobject with gibberish keys that
        # the semantic validator then (correctly) rejected 3/3. Form at the sampler (§0): a leaf is
        # exactly {path, op, value} with op drawn from quests._OPS; a compound is all_of/any_of of
        # ≤4 children. parse_admin_output's semantic pass (depth cap, path sanity) still runs.
        'crit ::= leaf | comp',
        f'leaf ::= "{{" jws {key("path")} bstring "," jws {key("op")} opstr "," jws'
        f' {key("value")} sval "}}" jws',
        'opstr ::= "\\"" ( ' + " | ".join(
            f'"{op}"' for op in sorted(quests._OPS, key=len, reverse=True)) + ' ) "\\"" jws',
        'sval ::= jstring | jnumber | ( "true" | "false" ) jws | sarr',
        'sarr ::= "[" jws ( sval ( "," jws sval )* )? "]" jws',
        f'comp ::= "{{" jws ( {key("all_of")} | {key("any_of")} )'
        f' "[" jws crit ( "," jws crit ){{0,3}} "]" jws "}}" jws',
        grammar_mod._JSON_RULES.strip(),
    ])


_QUEST_KEYS = {"id", "directive", "tier", "reward_xp", "expiry_hours", "criteria"}
_FLAG_KEYS = {"knob", "evidence"}
_TOP_KEYS = {"quests", "weakness_report", "narrator", "tuning_flags"}
_CRIT_DEPTH_MAX = 3   # declared: criteria nesting cap — a 3-deep predicate tree is already baroque


def _valid_criteria(d: Any, depth: int = 0) -> bool:
    """A criteria dict must round-trip into a CHECKABLE Criterion: leaf = non-empty path + known op;
    compound = non-empty all_of/any_of of valid children. quests._OPS is the single op registry."""
    if not isinstance(d, dict) or depth > _CRIT_DEPTH_MAX:
        return False
    if "all_of" in d or "any_of" in d:
        kids = d.get("all_of") if "all_of" in d else d.get("any_of")
        extra = set(d.keys()) - {"all_of", "any_of"}
        if extra or not isinstance(kids, list) or not kids:
            return False
        return all(_valid_criteria(k, depth + 1) for k in kids)
    if set(d.keys()) - {"path", "op", "value"}:
        return False
    path = d.get("path")
    if not isinstance(path, str) or not path.strip():
        return False
    if d.get("op", ">=") not in quests._OPS:
        return False
    return not isinstance(d.get("value"), (dict,))   # thresholds are scalars/lists, never objects


def parse_admin_output(text: str) -> Optional[dict]:
    """Strictly validate one check-in output against the proposal schema. Returns the normalized
    dict, or None (the caller drops-with-log — malformed output is never committed, never silent).
    Strictness is the point: exact key sets, typed fields, bounded arrays, checkable criteria, and
    tuning flags that are {knob, evidence} ONLY (a value-shaped flag is malformed by definition)."""
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or set(d.keys()) != _TOP_KEYS:
        return None
    if not isinstance(d.get("weakness_report"), str) or not isinstance(d.get("narrator"), str):
        return None

    quests_in = d.get("quests")
    flags_in = d.get("tuning_flags")
    if not isinstance(quests_in, list) or len(quests_in) > MAX_QUESTS_PER_CHECKIN:
        return None
    if not isinstance(flags_in, list) or len(flags_in) > MAX_FLAGS_PER_CHECKIN:
        return None

    out_quests: list[dict] = []
    for q in quests_in:
        if not isinstance(q, dict) or set(q.keys()) != _QUEST_KEYS:
            return None
        qid = _ID_SAFE.sub("_", str(q.get("id") or "")).strip("_")
        directive = q.get("directive")
        if not qid or not isinstance(directive, str) or not directive.strip():
            return None
        try:
            tier = int(q["tier"])
            reward_xp = int(q["reward_xp"])
            expiry_hours = float(q["expiry_hours"])
        except (TypeError, ValueError, KeyError):
            return None
        if tier < 1 or reward_xp < 0 or expiry_hours < 0:
            return None
        if not _valid_criteria(q.get("criteria")):
            return None
        out_quests.append({"id": qid, "directive": directive.strip(), "tier": tier,
                           "reward_xp": reward_xp, "expiry_hours": expiry_hours,
                           "criteria": q["criteria"]})

    out_flags: list[dict] = []
    for f in flags_in:
        if not isinstance(f, dict) or set(f.keys()) != _FLAG_KEYS:
            return None
        knob, evidence = f.get("knob"), f.get("evidence")
        if not isinstance(knob, str) or not _KNOB_RE.match(knob):
            return None
        if not isinstance(evidence, str) or not evidence.strip():
            return None
        out_flags.append({"knob": knob, "evidence": evidence.strip()})

    return {"quests": out_quests, "weakness_report": d["weakness_report"],
            "narrator": d["narrator"], "tuning_flags": out_flags}


# ============================================================================================
# 5. The check-in — dossier → LLM → proposals into the pending store (auto-issue where earned)
# ============================================================================================
@dataclass
class AdminReport:
    """One check-in's outcome. `dropped` means the LLM's output was malformed and was dropped-with-
    log (nothing committed). `pending_ids` are proposals awaiting the operator; `auto_issued_ids`
    went straight through System.propose on a tier's earned autonomy."""
    event: str = ""
    ts: str = field(default_factory=_now_iso)
    dropped: bool = False
    drop_reason: str = ""
    pending_ids: list[str] = field(default_factory=list)
    auto_issued_ids: list[str] = field(default_factory=list)
    weakness_report: str = ""
    narrator: str = ""
    tuning_flags: list[dict] = field(default_factory=list)


def _quest_from_proposal(p: dict, *, now: Optional[float] = None) -> Quest:
    """Build the Quest that crosses the wall. ONLY quest-window fields ride on it — no narrator
    internals, no dossier text, no plan text (the one-directional wall, §7a)."""
    hours = float(p.get("expiry_hours") or 0.0)
    expiry_ts = ((now if now is not None else time.time()) + hours * 3600.0) if hours > 0 else None
    return Quest(
        id=str(p["id"]),
        directive=str(p["directive"]),
        success_criteria=Criterion.from_dict(p["criteria"]),
        reward={"kind": REWARD_XP, "amount": int(p["reward_xp"])},
        tier=int(p["tier"]),
        expiry_ts=expiry_ts,
        hidden=False,
        kind="quest",
    )


def check_in(config, llm: Callable[[list, str], str], event: Any, *,
             persona: Optional[dict] = None, now: Optional[float] = None) -> Optional[AdminReport]:
    """One Administrator check-in: compile the fresh dossier, call the injected `llm(messages,
    grammar) -> str` under the proposal grammar, and route the outputs:
      - quest proposals → the pending store (or straight through System.propose when the tier has
        earned graduated autonomy);
      - weakness report / narrator / tuning flags → returned on the AdminReport (operator-facing).
    Malformed output → dropped-with-log, nothing committed, marker untouched. Flag off / non-wake
    event → None, nothing written."""
    if not should_check_in(config, event):
        return None
    kind = event.get("kind") if isinstance(event, dict) else str(event)
    state = AdminState(config)
    dossier = compile_dossier(config, persona=persona)
    messages = [
        {"role": "system", "content": ADMIN_SYSTEM_PROMPT + "\n\n" + fourth_wall_context(config)},
        {"role": "user", "content": f"CHECK-IN EVENT: {kind}\n\nDOSSIER:\n"
                                    + json.dumps(dossier, ensure_ascii=False, indent=1)},
    ]
    try:
        raw = llm(messages, build_admin_grammar())
    except Exception as e:  # noqa: BLE001 - the trainer failing must never wound anything
        logger.warning("administrator: llm call failed on %s check-in: %s", kind, e)
        return AdminReport(event=kind, dropped=True, drop_reason=f"llm error: {e}")

    parsed = parse_admin_output(raw)
    if parsed is None:
        logger.warning("administrator: dropping malformed check-in output (event=%s, %d chars)",
                       kind, len(raw or ""))
        return AdminReport(event=kind, dropped=True, drop_reason="malformed output")

    report = AdminReport(event=kind, weakness_report=parsed["weakness_report"],
                         narrator=parsed["narrator"], tuning_flags=parsed["tuning_flags"])
    quest_ids: list[str] = []
    for p in parsed["quests"]:
        pid = p["id"]
        if pid in state.proposals:
            logger.info("administrator: skipping duplicate proposal id %s", pid)
            continue
        record = dict(p)
        record.update({"narrator": parsed["narrator"], "event": kind,
                       "created_ts": _now_iso(), "resolved_ts": None})
        if state.tier_has_autonomy(p["tier"]):
            # Graduated autonomy: this tier's recent approval record earned auto-issue (§7).
            quest = _quest_from_proposal(p, now=now)
            System(config).propose(quest)
            record["status"] = "auto_issued"
            record["resolved_ts"] = _now_iso()
            report.auto_issued_ids.append(pid)
        else:
            if len(state.pending()) >= PENDING_MAX:
                logger.warning("administrator: pending store full (%d) — dropping proposal %s",
                               PENDING_MAX, pid)
                continue
            record["status"] = "pending"
            report.pending_ids.append(pid)
        state.proposals[pid] = record
        quest_ids.append(pid)

    # The marker: the ONLY cross-check-in memory (§7a) — what was proposed, so the next dossier
    # can reference how it turned out.
    state.last_checkin = {"ts": report.ts, "event": kind, "quest_ids": quest_ids,
                          "pending_ids": list(report.pending_ids),
                          "auto_issued_ids": list(report.auto_issued_ids)}
    state.prune()
    state.save()
    return report


# ============================================================================================
# 6. Approval seams — the propose/apply geometry, applied to the trainer (§0.5)
# ============================================================================================
_EDITABLE = ("directive", "tier", "reward_xp", "expiry_hours", "criteria")


def pending_proposals(config) -> list[dict]:
    """The operator-facing pending list (oldest first). Flag off → []."""
    if not _enabled(config):
        return []
    return AdminState(config).pending()


def approve_proposal(config, proposal_id: str, edit: Optional[dict] = None) -> Optional[Quest]:
    """Operator approval: route a pending quest proposal through System.propose — the ONLY channel
    into the creature's world. `edit` may override any of {directive, tier, reward_xp,
    expiry_hours, criteria} (approve-with-an-edit still counts as an approval for the tier's
    autonomy record: the generator's proposal was good enough to ship). Returns the enqueued
    Quest, or None. Flag off → no-op."""
    if not _enabled(config):
        return None
    state = AdminState(config)
    p = state.proposals.get(str(proposal_id))
    if p is None or p.get("status") != "pending":
        return None
    proposed_tier = int(p.get("tier", 1))
    if edit:
        for k in _EDITABLE:
            if k in edit:
                p[k] = edit[k]
        if "criteria" in edit and not _valid_criteria(p.get("criteria")):
            logger.warning("administrator: edit made proposal %s criteria un-checkable — refused",
                           proposal_id)
            return None
    quest = _quest_from_proposal(p)
    System(config).propose(quest)
    p["status"] = "approved"
    p["resolved_ts"] = _now_iso()
    # The autonomy record grades the GENERATOR, so credit lands on the tier it PROPOSED.
    state.record_decision(proposed_tier, approved=True)
    state.prune()
    state.save()
    return quest


def reject_proposal(config, proposal_id: str, reason: str = "") -> bool:
    """Operator rejection: mark the proposal rejected and debit the tier's autonomy record.
    Nothing crosses the wall. Flag off → no-op."""
    if not _enabled(config):
        return False
    state = AdminState(config)
    p = state.proposals.get(str(proposal_id))
    if p is None or p.get("status") != "pending":
        return False
    p["status"] = "rejected"
    p["resolved_ts"] = _now_iso()
    p["reject_reason"] = str(reason or "")
    state.record_decision(int(p.get("tier", 1)), approved=False)
    state.prune()
    state.save()
    return True


def tier_has_autonomy(config, tier: int) -> bool:
    """True when `tier`'s recent approval record has earned auto-issue (≥ AUTONOMY_APPROVAL_
    THRESHOLD over ≥ AUTONOMY_MIN_SAMPLE of the last AUTONOMY_WINDOW decisions, not revoked)."""
    if not _enabled(config):
        return False
    return AdminState(config).tier_has_autonomy(int(tier))


def revoke_autonomy(config, tier: int) -> None:
    """The ban-hammer seam (§7: Dean stays the ban-hammer, never the bottleneck): revoke a tier's
    earned auto-issue AND clear its decision history — trust must be re-earned from zero through
    the approval seam. Flag off → no-op."""
    if not _enabled(config):
        return
    state = AdminState(config)
    state.autonomy[str(int(tier))] = {"decisions": [], "revoked": True}
    state.save()
