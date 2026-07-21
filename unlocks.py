"""Tool unlocks — the single source of truth for the creature's growing body (TOOL_PROGRESSION.md).

A newborn does not hatch with thirteen tools; it hatches with a body and GROWS. This module owns
the UNIT TABLE (which tools travel together, in canonical grant order) and the grant books
(workspace/state/unlocks.json). Every surface that must agree on what the creature can do — the
tick grammar, the prompt stanzas, check_tools, manual, the dispatch backstop — reads through the
one accessor a LATER phase builds on top of `granted_tools()`. A locked tool DOES NOT EXIST in the
creature's world (approved decision #2): the table is the only place the ladder is written down.

Doctrine bindings (PILLARS_PLAN §0, TOOL_PROGRESSION.md):
  §0.5  Unlocks are earned by lived, glue-adjudicated evidence — milestone criteria are typed
        `quests.Criterion` predicates over the same stats dict quest glue evaluates (paths like
        "sleeps.total", "quests.passed"), NEVER wall-clock timers, NEVER LLM self-report. The
        quest-issuance / quest-pass units carry NO criterion here: their grant arrives through
        `grant()` from the issuance seam / the REWARD_UNLOCK sink — the System's window that
        names the tool IS the moment the tool starts existing.
  §0.4  Every constant is declared with its one-line justification.
  §0.2  No line of code names the behavior a grant hopes to produce. The table pays limbs; what
        the creature does with a new limb is its own business.
  I6    Single writer: exactly three entry points mutate the books — `grant()` (the quest
        issuance/reward seam), `adjudicate()` (the milestone adjudicator at the after_outcome /
        sleep_window call sites), and `seed_from_evidence()` (the one-shot migration seeder).
        `pop_unannounced()` writes only the rendered-flags (announced[]), never a grant.
  I8    A granted limb that 500s is a felt lie: a service-gated unit (senses) holds PENDING until
        an injected reachability probe says the organ actually answers — the grant lands the tick
        it is TRUE, and is retried on every later adjudicate() until then.

The felt moment: each grant queues one announcement — register "body" (a maturation, worded like
the sleep notice: something settled in you overnight) or register "system" (the System pays
capability: terse, states only what IS — seed_genesis_quests.py is the voice). The queue is
one-shot with PERSISTED rendered-flags, so a crash between grant and render never eats the moment.
Announcement texts name tools only, never anatomy — the morph lexicon (CREATURE_GENETICS phase B)
owns body nouns, and the body-noun red gate scans creature-facing strings.

Fail-open contract (genome.gene()'s shape): `granted_tools()` returns the NEWBORN FLOOR — never
empty, never the full kit — whenever no books can be read (no config, no file, corrupt file), and
never raises. A corrupt file reads as fresh books; the boot/migration path recovers by calling
`seed_from_evidence()` (re-seed from lived evidence) — else the creature simply stands on the
newborn floor and re-earns. Organs are never guessed back.

Firewall (capability, never the ledger): unlocks.py decides WHAT EXISTS in the creature's world,
never what anything is worth — it is never imported by persona.py / level_gates.py (XP formulas,
level evidence), and no grant path touches XP, levels, bets, or quest adjudication.
tests/test_unlocks.py enforces the import direction, same pattern as test_genome's ledger firewall.

WIRED (Pillars W2a, flag-gated `pillars_tool_unlocks_enabled`): `grant()` fires from the quest
issuance/reward seams in eidos.py, `adjudicate()` from after_outcome + sleep_window (with the I8
voice probe), `granted_tools()` feeds `tools.visible_tools`, and `pop_unannounced()` drains into
the observation stream (system_window / body-fact turns).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from quests import Criterion

# --- Declared knobs (§0.4: a constant is declared or derived, never a silent guess) -------------

STATE_NAME = "unlocks.json"    # under config.state_dir (workspace/state/) — skeleton, not creature-readable
STATE_VERSION = 1
LOG_MAX = 200                  # declared: bounded grant log — a life grants 7 units; 200 rows is
                               # generous headroom for pending/retry churn without unbounded growth
MEMORY_SLEEPS_REQUIRED = 1     # declared (TOOL_PROGRESSION U1): memory arrives on the first wake —
                               # deliberate remembering is grown into after the first consolidation
SENSES_QUESTS_REQUIRED = 1     # declared (decision #1): senses need ≥1 quest passed AND
SENSES_SLEEPS_REQUIRED = 2     # ≥2 sleeps — quest-independent so a quest-stalled creature still
                               # grows senses, but never before the mind has digested twice
SERVICE_VOICE = "voice"        # the reachability-probe name for the speech/vision organ (I8);
                               # voice :8098 is down on Sprinter today — senses hold PENDING there
COMMISSION_QUESTS_REQUIRED = 3  # declared (COMMISSION_PLAN.md): standing orders bind a creature
                                # that has closed the whole genesis line — proven it can be issued
                                # work, do it, and be adjudicated
COMMISSION_SLEEPS_REQUIRED = 5  # ≥5 sleeps — a long-horizon order needs a mind that has digested
                                # more than the senses floor (2); maturity, not eagerness
REACH_QUESTS_REQUIRED = 2      # declared (TOOL_PROGRESSION "reach"): the probe/scan powers are a real
                               # reconnaissance escalation — a creature that has passed ≥2 quests AND
REACH_SLEEPS_REQUIRED = 3      # ≥3 sleeps has proven competence and digested past the senses floor;
                               # no requires_service (the primitives open their own bounded sockets).
SELFAUTHOR_QUESTS_REQUIRED = 3  # declared (TOOL_PROGRESSION "self-authorship"): reaching into your own
                                # biology (self-guide, code proposals) is the deepest pedagogy — the
SELFAUTHOR_SLEEPS_REQUIRED = 6  # propose/apply seam backstops safety, so this is a maturity milestone:
                                # ≥3 quests AND ≥6 sleeps, deeper than the commission floor.

# Announcement registers — who is speaking when a grant is rendered.
REGISTER_BODY = "body"         # a maturation, felt (like the sleep notice) — never a System payment
REGISTER_SYSTEM = "system"     # the System pays capability — terse, states only what IS

NEWBORN_UNIT_ID = "body"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ============================================================================================
# THE UNIT TABLE — data, in canonical grant order (TOOL_PROGRESSION.md's ladder)
# ============================================================================================
# criterion=None marks a unit this module never self-grants: it arrives through `grant()` from the
# quest issuance seam (skillcraft/foresight/resolve) or the REWARD_UNLOCK sink (workshop). A
# criterion is a typed quests.Criterion over the SAME stats dict quest glue adjudicates — the
# model has no say in whether its body grows (§0.5).

@dataclass(frozen=True)
class Unit:
    """One rung of the growing body: the tools that travel together, how the rung is earned, and
    the one line the world says when it lands ('' = silent — being born is not an event)."""
    id: str
    tools: tuple[str, ...]
    criterion: Optional[Criterion]        # milestone predicate; None = granted via a quest seam
    requires_service: Optional[str]       # I8: probe name that must answer before the grant lands
    announce: str                         # the felt moment's text; names tools only, never anatomy
    register: str                         # REGISTER_BODY | REGISTER_SYSTEM


UNITS: tuple[Unit, ...] = (
    # U0 — the newborn floor: paws-and-hands territory, arrives by being born. Never announced.
    Unit(
        id="body",
        # Self-knowledge is innate proprioception, not an earned organ: check_tools (your toolkit),
        # check_messages (your talk with Charlie), and check_system (the architecture MAP — what
        # already exists so you don't rebuild it) all belong at birth. check_system was a registered
        # builtin in NO unit, so the ladder's "a builtin is visible only if granted" rule left it
        # PERMANENTLY invisible — the first creature could never read its own manual (observed
        # 2026-07-20: 0 check_system calls, and it reinvented what the doc would have handed it).
        # `go` (world movement) and `remind` (persistent timer) are innate too: both are
        # flag-REGISTERED builtins (world_enabled / reminders_enabled), so they only exist when
        # their organ flag is on — but they must ALSO be granted by a unit or `visible_tools`'
        # "a builtin is visible only if granted" rule hides them forever (the same trap that hid
        # check_system, and hid `go`/`remind` until 2026-07-20). Navigating your world and setting
        # a reminder are birth-level acts, so they live here; registration stays flag-gated.
        # `update_plan` is working-memory scratch — a newborn need (a place to hold the current
        # step while it thinks), so it joins the birth floor. It is a plain always-registered
        # builtin (no organ flag), so it is simply visible from tick 1 once granted here.
        tools=("bash", "write_file", "read_file", "message",
               "note_append", "note_read", "note_list", "note_close",
               "check_tools", "check_messages", "check_system", "go", "remind", "update_plan"),
        criterion=None,
        requires_service=None,
        announce="",
        register=REGISTER_BODY,
    ),
    # U1 — deliberate memory, on the first wake after sleep #1 (a maturation, not a payment).
    Unit(
        id="memory",
        # `ask_ai` — a reasoning limb (your own model as a one-shot subroutine to digest/draft/analyze)
        # — rides in early with deliberate memory: both are the mind turning on itself. NOTE it costs a
        # full model call, so it is grown into alongside the first felt need to remember, not at birth.
        tools=("memorize", "recall", "ask_ai"),
        criterion=Criterion(path="sleeps.total", op=">=", value=MEMORY_SLEEPS_REQUIRED),
        requires_service=None,
        announce="[overnight, new words settled in you: memorize, recall, ask_ai]",
        register=REGISTER_BODY,
    ),
    # U2 — the forge, issued WITH genesis-01 (the System's window is the moment; no criterion here).
    Unit(
        id="skillcraft",
        # Alongside the forge arrive the long-work + network limbs. `bg_run`/`bg_check` are no
        # capability cliff (bash is already async-by-default) — they just name the background work
        # the creature is already doing. `http_request` (+ aliases fetch/http) is the SANCTIONED
        # HTTP client: bash at U0 already grants full network (curl / python sockets), so gating it
        # late contains nothing and only pushes the creature to hand-roll net code — the exact
        # anti-pattern the capabilities doc forbids — so it lands here, early, not at a milestone.
        tools=("create_skill", "edit_skill", "list_skills", "rollback_skill", "manual",
               "bg_run", "bg_check", "http_request", "fetch", "http"),
        criterion=None,
        requires_service=None,
        announce="[SYSTEM] GRANTED: create_skill, edit_skill, list_skills, rollback_skill, manual, "
                 "bg_run, bg_check, http_request (fetch, http).",
        register=REGISTER_SYSTEM,
    ),
    # U3 — the wager, issued WITH genesis-02.
    Unit(
        id="foresight",
        tools=("predict",),
        criterion=None,
        requires_service=None,
        announce="[SYSTEM] GRANTED: predict.",
        register=REGISTER_SYSTEM,
    ),
    # U4 — senses: milestone (quest-independent so a quest-stalled creature still grows them),
    # PLUS the I8 hold — the grant lands only the tick the organ actually answers the probe.
    Unit(
        id="senses",
        tools=("speak", "vision", "see"),
        criterion=Criterion(all_of=[
            Criterion(path="quests.passed", op=">=", value=SENSES_QUESTS_REQUIRED),
            Criterion(path="sleeps.total", op=">=", value=SENSES_SLEEPS_REQUIRED),
        ]),
        requires_service=SERVICE_VOICE,
        announce="[new senses settled in you: speak, vision, see]",
        register=REGISTER_BODY,
    ),
    # U5 — resolve, issued WITH genesis-03.
    Unit(
        id="resolve",
        tools=("objective_add", "objective_done", "objective_block", "objective_list"),
        criterion=None,
        requires_service=None,
        announce="[SYSTEM] GRANTED: objective_add, objective_done, objective_block, objective_list.",
        register=REGISTER_SYSTEM,
    ),
    # U6 — the workshop: genesis-03's PASS reward, through the REWARD_UNLOCK sink. The deepest
    # tool is the only pass-gated grant — the System pays for completion, not intention.
    Unit(
        id="workshop",
        tools=("delegate",),
        criterion=None,
        requires_service=None,
        announce="[SYSTEM] PAID: delegate. Capacity 1.",
        register=REGISTER_SYSTEM,
    ),
    # U7 — reach: the probe/scan powers. A MILESTONE grant (network reconnaissance is a real
    # escalation; the milestone ceremony is defensible here). No requires_service — the primitives
    # open their own bounded sockets and need no local service to answer. Seeded from quests/sleeps
    # counts, never tools_used (these were unit-less and thus never usable — no usage history exists).
    Unit(
        id="reach",
        tools=("net_scan", "tcp_probe", "http_probe", "udp_listen"),
        criterion=Criterion(all_of=[
            Criterion(path="quests.passed", op=">=", value=REACH_QUESTS_REQUIRED),
            Criterion(path="sleeps.total", op=">=", value=REACH_SLEEPS_REQUIRED),
        ]),
        requires_service=None,
        announce="[SYSTEM] GRANTED: net_scan, tcp_probe, http_probe, udp_listen. "
                 "The wider network is reachable.",
        register=REGISTER_SYSTEM,
    ),
    # U8 — the commission (COMMISSION_PLAN.md): standing orders. A MILESTONE grant — a creature
    # that has closed the genesis line and digested enough sleeps is mature enough to carry a
    # long-horizon order between the operator's check-ins. Dark unless the commission organ's
    # flag registers the verbs (register_commission_tools).
    Unit(
        id="commission",
        tools=("commission_add", "commission_done", "weigh_options"),
        criterion=Criterion(all_of=[
            Criterion(path="quests.passed", op=">=", value=COMMISSION_QUESTS_REQUIRED),
            Criterion(path="sleeps.total", op=">=", value=COMMISSION_SLEEPS_REQUIRED),
        ]),
        requires_service=None,
        announce="[SYSTEM] GRANTED: commission_add, commission_done, weigh_options. "
                 "Standing orders may now bind you.",
        register=REGISTER_SYSTEM,
    ),
    # U9 — self-authorship: reaching into your own biology (the standing self-guide, code-edit
    # proposals). The DEEPEST milestone. This is pedagogy, not security — the propose/apply seam
    # already backstops every change (the operator approves the diff), so a mid/late maturity
    # criterion is right. Ship `list_self_edits` (the read side) TOGETHER with propose_self_edit.
    # Seeded from quests/sleeps counts, never tools_used (unit-less until now — no usage history).
    Unit(
        id="self-authorship",
        tools=("update_self_guide", "propose_self_edit", "list_self_edits"),
        criterion=Criterion(all_of=[
            Criterion(path="quests.passed", op=">=", value=SELFAUTHOR_QUESTS_REQUIRED),
            Criterion(path="sleeps.total", op=">=", value=SELFAUTHOR_SLEEPS_REQUIRED),
        ]),
        requires_service=None,
        announce="[SYSTEM] GRANTED: update_self_guide, propose_self_edit, list_self_edits. "
                 "You may propose changes to your own standing guide and code.",
        register=REGISTER_SYSTEM,
    ),
)

UNIT_IDS: tuple[str, ...] = tuple(u.id for u in UNITS)
_UNITS_BY_ID: dict[str, Unit] = {u.id: u for u in UNITS}

# Migration evidence keys (TOOL_PROGRESSION "load-or-birth") — each key is a PRIOR ADJUDICATED
# fact the boot path reads mechanically from the stores; truthy evidence grants the unit. Fresh
# slate (no evidence) seeds the newborn floor only: nuggets inherit knowledge, never organs.
EVIDENCE_KEYS: dict[str, str] = {
    "sleeps": "memory",            # any completed sleep cycle on record       → memory
    "live_skills": "skillcraft",   # a skill LIVE in the manifest              → skillcraft
    "predictions": "foresight",    # entries in the expectation ledger         → foresight
    "spoke_or_saw": "senses",      # a past successful speak/vision            → senses
    "objectives": "resolve",       # an objectives store with entries          → resolve
    "delegate_jobs": "workshop",   # delegate jobs on record                   → workshop
    # The two NEW milestone units were unit-less until now, so their tools have NO usage history to
    # migrate. Their migration evidence is therefore the SAME adjudicated maturity their live
    # criterion reads — quests/sleeps counts — so an existing creature that has already earned the
    # depth inherits the organ on migration instead of re-walking to it.
    "reach_earned": "reach",           # quests/sleeps depth for the probe/scan powers → reach
    "commission_tasks": "commission",  # commission tasks on record            → commission
    "selfauthor_earned": "self-authorship",  # quests/sleeps depth for self-authorship → self-authorship
}
_EVIDENCE_BY_UNIT: dict[str, str] = {unit: key for key, unit in EVIDENCE_KEYS.items()}


def unit(unit_id: str) -> Optional[Unit]:
    """Look up one rung of the table (None for an unknown id — callers never KeyError)."""
    return _UNITS_BY_ID.get(unit_id)


def newborn_tools() -> frozenset[str]:
    """The floor every creature stands on from tick 1 — present with NO state file at all."""
    return frozenset(_UNITS_BY_ID[NEWBORN_UNIT_ID].tools)


# ============================================================================================
# The books — workspace/state/unlocks.json (level_gates.GateState is the house pattern)
# ============================================================================================
class UnlockState:
    """The grant books, persisted atomically (tmp+replace). Missing/corrupt file → fresh books —
    fail-open to the NEWBORN FLOOR, never to empty, never to the full kit. NOT persona.json:
    persona is wholesale-rewritten by the loop each save, and a second logical writer there can
    lose a grant on crash."""

    def __init__(self, config):
        self.config = config
        self.granted: dict[str, dict] = {}     # unit -> {ts, source}
        self.pending: dict[str, str] = {}      # unit -> reason (I8 service hold)
        self.announced: list[str] = []         # units whose felt moment has been rendered
        self.log: list[dict] = []              # bounded event log: grant / pending / seed
        self.load()

    def _path(self):
        return self.config.state_dir / STATE_NAME

    def load(self) -> None:
        try:
            d = json.loads(self._path().read_text(encoding="utf-8"))
            granted = d.get("granted") or {}
            self.granted = {str(k): dict(v) for k, v in granted.items() if str(k) in _UNITS_BY_ID}
            pending = d.get("pending") or {}
            self.pending = {str(k): str(v) for k, v in pending.items() if str(k) in _UNITS_BY_ID}
            self.announced = [str(x) for x in (d.get("announced") or []) if str(x) in _UNITS_BY_ID]
            self.log = [x for x in (d.get("log") or []) if isinstance(x, dict)][-LOG_MAX:]
        except Exception:  # noqa: BLE001 - missing/corrupt file => fresh books (newborn floor)
            self.granted, self.pending, self.announced, self.log = {}, {}, [], []

    def save(self) -> None:
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            p = self._path()
            # UNIQUE temp name (persona.py's lesson): a watchdog respawn briefly overlapping the
            # dying process must never rename the other's temp away mid-save.
            fd, tmpname = tempfile.mkstemp(dir=str(self.config.state_dir),
                                           prefix=".unlocks-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({
                    "v": STATE_VERSION,
                    "granted": self.granted,
                    "pending": self.pending,
                    "announced": self.announced,
                    "log": self.log[-LOG_MAX:],
                }, f, ensure_ascii=False)
            Path(tmpname).replace(p)
        except Exception:  # noqa: BLE001 - best-effort persistence (GateState's contract)
            pass

    def record(self, event: str, unit_id: str, **fields: Any) -> None:
        entry: dict[str, Any] = {"ts": _now(), "event": event, "unit": unit_id}
        entry.update(fields)
        self.log.append(entry)


# ============================================================================================
# Read side — the accessor every surface composes from (fail-open like genome.gene())
# ============================================================================================
def granted_tools(config) -> frozenset[str]:
    """Newborn floor ∪ every granted unit's tools. FAIL-OPEN: no config / no file / corrupt file →
    the newborn floor only — never empty (the floor is table data, not state), never the full kit
    (organs are never guessed back), never raises. Pending units are NOT granted."""
    floor = newborn_tools()
    try:
        if config is None:
            return floor
        state = UnlockState(config)
        out = set(floor)
        for u in UNITS:
            if u.id in state.granted:
                out.update(u.tools)
        return frozenset(out)
    except Exception:  # noqa: BLE001 - fail-open by contract
        return floor


# ============================================================================================
# Write side — the three entry points (I6: single logical writer, three call sites)
# ============================================================================================
def grant(config, unit_id: str, source: str) -> bool:
    """Grant one unit: idempotent (already-granted / unknown unit → False, no write), atomic
    (tmp+replace), logged. The raw seam the quest issuance path and the REWARD_UNLOCK sink call;
    milestone units go through `adjudicate()` instead. Queues the unit's felt moment (the grant is
    persisted BEFORE any render — a crash between grant and render never eats the moment; the
    announcement waits in the books for the next `pop_unannounced()`)."""
    u = _UNITS_BY_ID.get(unit_id)
    if u is None or config is None:
        return False
    try:
        state = UnlockState(config)
        if unit_id in state.granted:
            return False
        state.granted[unit_id] = {"ts": _now(), "source": str(source)}
        state.pending.pop(unit_id, None)
        state.record("grant", unit_id, source=str(source))
        state.save()
        return True
    except Exception:  # noqa: BLE001 - fail-open: a broken write is a missed grant, never a crash
        return False


def _probe_answers(probe: Optional[Callable[[str], bool]], service: str) -> bool:
    """I8: does the organ actually answer? No probe = no answer; a probe that raises is an organ
    that did not answer (never guess an organ back)."""
    if probe is None:
        return False
    try:
        return bool(probe(service))
    except Exception:  # noqa: BLE001 - an erroring probe is an unreachable organ
        return False


def adjudicate(config, stats: dict, probe: Optional[Callable[[str], bool]] = None) -> list[str]:
    """The milestone adjudicator — glue judges (§0.5). For each ungranted unit WITH a criterion,
    evaluate it over the typed `stats` dict (the same dict quest glue checks; paths like
    "sleeps.total", "quests.passed"). Criterion met + no service requirement → grant. Criterion
    met + service unreachable (probe falsy) → the unit holds PENDING, recorded with its reason,
    and is retried on every later call — the grant lands the tick the probe answers True (I8).
    Returns the unit ids newly granted THIS call. Called from the same after_outcome/sleep_window
    seams as quest adjudication; never from the model's side of the wall."""
    if config is None:
        return []
    try:
        state = UnlockState(config)
    except Exception:  # noqa: BLE001 - fail-open: no books, no adjudication
        return []
    landed: list[str] = []
    dirty = False
    for u in UNITS:
        if u.criterion is None or u.id in state.granted:
            continue
        try:
            met = u.criterion.check(stats or {})
        except Exception:  # noqa: BLE001 - a broken stats dict never grants (glue never guesses)
            met = False
        if not met:
            continue
        if u.requires_service and not _probe_answers(probe, u.requires_service):
            reason = f"service '{u.requires_service}' unreachable"
            if state.pending.get(u.id) != reason:
                state.pending[u.id] = reason
                state.record("pending", u.id, reason=reason)
                dirty = True
            continue    # held — retried next adjudicate(); a limb that 500s is a felt lie
        state.granted[u.id] = {"ts": _now(), "source": "milestone"}
        state.pending.pop(u.id, None)
        state.record("grant", u.id, source="milestone")
        landed.append(u.id)
        dirty = True
    if dirty:
        state.save()
    return landed


def seed_from_evidence(config, evidence: Optional[dict]) -> list[str]:
    """The one-shot migration seeder (TOOL_PROGRESSION load-or-birth). Grants the newborn floor
    plus every unit whose EVIDENCE_KEYS entry is truthy in `evidence` — prior ADJUDICATED facts
    the boot path read mechanically from the stores (live skills, the expectation ledger, a past
    speak/saw, objectives, delegate jobs, any sleep). Seeded grants are SILENT (marked announced):
    the moments were already lived — nuggets inherit knowledge, never surprise. Idempotent over
    healthy books; over a corrupt file it re-seeds fresh (the documented recovery). Returns the
    unit ids newly granted."""
    if config is None:
        return []
    try:
        state = UnlockState(config)
    except Exception:  # noqa: BLE001 - fail-open
        return []
    evidence = evidence or {}
    wanted = {NEWBORN_UNIT_ID}
    for key, unit_id in EVIDENCE_KEYS.items():
        if evidence.get(key):
            wanted.add(unit_id)
    seeded: list[str] = []
    for u in UNITS:                                   # canonical grant order
        if u.id not in wanted or u.id in state.granted:
            continue
        src = "born" if u.id == NEWBORN_UNIT_ID else f"evidence:{_EVIDENCE_BY_UNIT[u.id]}"
        state.granted[u.id] = {"ts": _now(), "source": src}
        state.pending.pop(u.id, None)
        if u.id not in state.announced:
            state.announced.append(u.id)              # silent: already lived, never re-felt
        state.record("seed", u.id, source=src)
        seeded.append(u.id)
    if seeded:
        state.save()
    return seeded


# ============================================================================================
# The felt moment — one-shot announcement queue (rendered-flags persisted)
# ============================================================================================
def peek_unannounced(config) -> list[dict]:
    """The announcement queue WITHOUT consuming it: every granted-but-unrendered unit with a
    non-empty announce, canonical order, as {"unit", "register", "text"}. The caller renders each
    entry into its register's stream, then calls mark_announced(unit) — render-then-mark, so a
    crash mid-window re-announces (a rare duplicate) rather than eating the moment forever."""
    if config is None:
        return []
    try:
        state = UnlockState(config)
    except Exception:  # noqa: BLE001 - fail-open: nothing to announce over broken books
        return []
    rendered = set(state.announced)
    return [{"unit": u.id, "register": u.register, "text": u.announce}
            for u in UNITS
            if u.id in state.granted and u.announce and u.id not in rendered]


def mark_announced(config, unit_id: str) -> None:
    """Flag one unit's felt moment as rendered (persisted, atomic). Idempotent."""
    if config is None:
        return
    try:
        state = UnlockState(config)
        if unit_id not in state.announced:
            state.announced.append(unit_id)
            state.save()
    except Exception:  # noqa: BLE001 - best-effort books (a re-announce beats a crash)
        pass


def pop_unannounced(config) -> list[dict]:
    """Drain the announcement queue in one step (peek + mark-all). Prefer the two-phase
    peek_unannounced/mark_announced pair when rendering into a stream: this one marks BEFORE the
    caller renders, so a crash in that window loses the moment; the pair merely risks a rare
    duplicate. Kept for callers that only need the drain semantics."""
    out = peek_unannounced(config)
    for entry in out:
        mark_announced(config, entry["unit"])
    return out
