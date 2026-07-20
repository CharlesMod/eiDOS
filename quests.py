"""The System — the quest engine (PILLARS_PLAN §7, PILLARS_TODO 5.1).

This is the ENGINE only: issue, track, adjudicate, and cadence the quests that goad the creature
onto new ground. The fourth-wall Administrator LLM (§7a / phase 5.2) that AUTHORS quests, and the
level-gates (phase 4.3) that some rewards unlock, are LATER — this module leaves clean seams for
them (`propose()` to enqueue, `reward_sink`/criteria hooks for adjudication) and builds neither.

Doctrine bindings (PILLARS_PLAN §0):
  §0.5  The glue judges; the creature never grades its own homework. A quest's success_criteria is
        a GLUE-CHECKABLE spec — a typed predicate over a passed-in stats dict (persona / objectives
        / skill manifest), NOT free text the model could narrate its way past. `check()` evaluates
        that predicate against typed state; self-report is never the reward signal.
  §0.4  Every constant here is either derived or a DECLARED knob with a one-line justification.
  §0.2  No line of code names the behavior it hopes to produce. This module builds the FIELD — an
        impersonal trainer that issues one challenge at a time on a state-driven cadence — and
        the "competent, not coddled" growth is what a creature under that field does.

Cadence (the not-coddled rule, §7): exactly ONE active quest at a time (BIBLE L-2). A queue holds
proposed quests; `issue_next()` promotes one to active ONLY when the prior closed AND ≥1 sleep
cycle has passed AND condition is healthy (not RECOVERY — read from glue). Silence otherwise.
Expiry / ignore is itself recorded as a failure-lite episode-shaped record — challenge is not
withheld to protect the creature from challenge.

State lives in a bounded `workspace/quests.jsonl` (+ dated monthly archive), the same
single-writer / bounded-file / monthly-archive shape as observations.jsonl and pressures.jsonl.
Ships DARK behind `config.pillars_quests_enabled` (default False); a LATER phase wires
`render_active()` into the context and calls `issue_next()` on the sleep boundary.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
QUESTS_MAX_BYTES = 1_000_000   # declared: rotate quests.jsonl at ~1 MB — years of quests, not hours
SLEEPS_REQUIRED = 1            # declared (§7): ≥1 sleep cycle between a close and the next issue —
                               # mandatory digestion, the spacing effect as a hard floor
QUEST_STALL_SLEEPS = 5         # declared (TOOL_PROGRESSION stall handling — pressure, not script):
                               # an active quest whose criteria show ZERO movement across this many
                               # consecutive sleeps closes FAILED (the reserved abandon path),
                               # unfreezing quest_line_closed so the Administrator's gap-mining can
                               # propose a smaller re-attack. Never an auto-grant timer.
_HEALTHY_BLOCK = ("RECOVERY",)  # declared (§7): the ONLY condition that stays the System's hand —
                               # silence is reserved for genuine recovery, never to dodge challenge
ARCHIVE_PREFIX = "quests_archive_"   # + YYYYMM.jsonl (house monthly-archive convention)

# Quest lifecycle states.
OFFERED = "offered"     # in the queue, not yet promoted
ACTIVE = "active"       # the one live quest (at most one)
PASSED = "passed"       # criteria met; reward applied
FAILED = "failed"       # closed unmet without reaching expiry — today: the stall-abandon path
EXPIRED = "expired"     # expiry reached (or ignored) before criteria met → failure-lite
_TERMINAL = (PASSED, FAILED, EXPIRED)

# Reward kinds. Only `xp` pays out in 5.1; `unlock`/`capacity` are recorded and handed to the
# reward_sink (the seam the level-gate phase fills) — this engine never unlocks a tier itself.
REWARD_XP = "xp"
REWARD_UNLOCK = "unlock"
REWARD_CAPACITY = "capacity"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ============================================================================================
# Success criteria — the GLUE-CHECKABLE spec (§0.5)
# ============================================================================================
# A criterion is a typed predicate over a stats dict, NOT free text. The stats dict is the typed
# view the harness passes at check time (a LATER phase assembles it): persona, objectives, and the
# skill manifest. `Criterion.check(stats)` returns a bool from deterministic comparison — the model
# has no say in whether it passed.
#
# Supported (grind-proof primitives; a criterion names a PATH into stats and an OP + threshold):
#   path : dotted path into the stats dict, e.g. "persona.level" or "skills.trusted_count"
#   op   : one of ">=", ">", "==", "<=", "<", "in", "contains"
#   value: the threshold / expected value
# `all_of` / `any_of` compose criteria so a quest can demand a conjunction (a level AND a
# calibration floor) — the Administrator (5.2) will emit these, glue evaluates them.

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": lambda a, b: a is not None and a >= b,
    ">": lambda a, b: a is not None and a > b,
    "<=": lambda a, b: a is not None and a <= b,
    "<": lambda a, b: a is not None and a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "in": lambda a, b: a in b if b is not None else False,
    "contains": lambda a, b: b in a if a is not None else False,
}


# The adjudicatable criteria vocabulary — the ONLY stat paths a quest's success_criteria may
# reference, because these are the ONLY paths the engine actually checks against: eidos._quest_stats
# builds exactly this dict from glue-settled, monotonic FACTS (a manifest/ledger/store count, never
# a tools_used attempt — §0.5). It is a deliberately THIN rulebook, NOT the rich dossier the
# Administrator reasons FROM: dossier readouts like skill_economy.authored, pitfall_health.*,
# level.sleeps_since_level, calibration_by_domain.* are analyst vocabulary and DO NOT resolve here,
# so a criterion naming one can never pass — it sits ACTIVE forever and bricks the mastery gate
# (this was live: every LLM-authored Administrator quest was un-adjudicatable). A drift guard
# (tests/test_admin_criteria_vocab) asserts every path here resolves in a real _quest_stats dict, so
# the rulebook and the referee can never diverge again.
ADJUDICATABLE_PATHS: dict[str, str] = {
    "skills.live_count":       "skills currently LIVE in the manifest",
    "skills.trusted_count":    "skills promoted to TRUSTED",
    "expectations.total":      "predictions ever placed (the calibration ledger)",
    "sleeps.total":            "completed NAP cycles ever (dreams do not count)",
    "quests.passed":           "quests adjudicated PASS ever",
    "persona.xp":              "current XP",
    "persona.level":           "current level",
    "persona.goals_completed": "self-chosen objectives finished",
    "persona.total_ticks":     "lifetime ticks lived",
    "commission.confirmed_total": "commission tasks CONFIRMED ever (operator verdict or measured claim)",
    "commission.open":         "commission tasks currently open",
}


def criteria_paths(criteria: Any) -> list[str]:
    """Every leaf `path` a criteria tree references (walking all_of/any_of), for vocabulary
    checking. Malformed/None → []. A leaf with a non-string path contributes nothing."""
    out: list[str] = []
    if not isinstance(criteria, dict):
        return out
    if "all_of" in criteria or "any_of" in criteria:
        for kid in (criteria.get("all_of") or criteria.get("any_of") or []):
            out.extend(criteria_paths(kid))
    elif isinstance(criteria.get("path"), str):
        out.append(criteria["path"])
    return out


def _dig(stats: dict, path: str) -> Any:
    """Resolve a dotted path into the typed stats dict. Missing → None (a criterion over an absent
    stat simply cannot pass — glue never guesses)."""
    cur: Any = stats
    for part in (path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


@dataclass
class Criterion:
    """One glue-checkable predicate over the typed stats dict (§0.5).

    Leaf form:   Criterion(path="persona.level", op=">=", value=3)
    Compound:    Criterion(all_of=[...]) / Criterion(any_of=[...])
    """
    path: str = ""
    op: str = ">="
    value: Any = None
    all_of: Optional[list["Criterion"]] = None
    any_of: Optional[list["Criterion"]] = None

    def check(self, stats: dict) -> bool:
        if self.all_of is not None:
            return all(c.check(stats) for c in self.all_of)
        if self.any_of is not None:
            return any(c.check(stats) for c in self.any_of)
        fn = _OPS.get(self.op)
        if fn is None:
            return False
        actual = _dig(stats, self.path)
        if actual is None:
            return False   # a criterion over an absent stat can never pass — glue never guesses
        return bool(fn(actual, self.value))

    def to_dict(self) -> dict:
        if self.all_of is not None:
            return {"all_of": [c.to_dict() for c in self.all_of]}
        if self.any_of is not None:
            return {"any_of": [c.to_dict() for c in self.any_of]}
        return {"path": self.path, "op": self.op, "value": self.value}

    @staticmethod
    def from_dict(d: dict) -> "Criterion":
        if not isinstance(d, dict):
            return Criterion(op="==", value=object())  # unsatisfiable — corrupt criteria never pass
        if "all_of" in d:
            return Criterion(all_of=[Criterion.from_dict(x) for x in d.get("all_of") or []])
        if "any_of" in d:
            return Criterion(any_of=[Criterion.from_dict(x) for x in d.get("any_of") or []])
        return Criterion(path=d.get("path", ""), op=d.get("op", ">="), value=d.get("value"))


# ============================================================================================
# Quest schema
# ============================================================================================
@dataclass
class Quest:
    id: str
    directive: str                       # terse, impersonal — the voice (§7)
    success_criteria: Criterion          # GLUE-CHECKABLE (§0.5), never free text
    reward: dict = field(default_factory=lambda: {"kind": REWARD_XP, "amount": 0})
    tier: int = 1                        # difficulty / level tier (level-gates are a LATER phase)
    expiry_ts: Optional[float] = None    # epoch seconds; None = no expiry (e.g. hidden achievements)
    hidden: bool = False                 # achievement: offered+hidden is not rendered; reveals on pass
    kind: str = "quest"                  # "quest" | "daily" — daily quests are recurring drill slots
    grants_unit: str = ""                # TOOL_PROGRESSION issuance-grant pattern: the tool UNIT this
                                         # quest's ISSUANCE grants (the System's window that names the
                                         # tool IS the moment it starts existing). "" = grants nothing.
                                         # This engine only PERSISTS the binding; unlocks.grant() is
                                         # the single writer that acts on it (seam, not a call here).
    state: str = OFFERED
    created_ts: str = field(default_factory=_now)
    closed_ts: Optional[str] = None
    outcome: Optional[str] = None        # freeform close note (glue-set, not self-report)
    stall_sleeps: int = 0                # consecutive sleeps with zero criterion movement (K-sleep
                                         # abandon path); reset whenever any criterion value moves
    stall_probe: Optional[dict] = None   # the criteria leaf values measured at the last sleep —
                                         # the movement baseline (glue-measured, never self-report)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "directive": self.directive,
            "success_criteria": self.success_criteria.to_dict(),
            "reward": self.reward,
            "tier": self.tier,
            "expiry_ts": self.expiry_ts,
            "hidden": self.hidden,
            "kind": self.kind,
            "grants_unit": self.grants_unit,
            "state": self.state,
            "created_ts": self.created_ts,
            "closed_ts": self.closed_ts,
            "outcome": self.outcome,
            "stall_sleeps": self.stall_sleeps,
            "stall_probe": self.stall_probe,
        }

    @staticmethod
    def from_dict(d: dict) -> "Quest":
        return Quest(
            id=d["id"],
            directive=d.get("directive", ""),
            success_criteria=Criterion.from_dict(d.get("success_criteria") or {}),
            reward=d.get("reward") or {"kind": REWARD_XP, "amount": 0},
            tier=int(d.get("tier", 1)),
            expiry_ts=d.get("expiry_ts"),
            hidden=bool(d.get("hidden", False)),
            kind=d.get("kind", "quest"),
            grants_unit=str(d.get("grants_unit", "") or ""),
            state=d.get("state", OFFERED),
            created_ts=d.get("created_ts") or _now(),
            closed_ts=d.get("closed_ts"),
            outcome=d.get("outcome"),
            stall_sleeps=int(d.get("stall_sleeps", 0) or 0),
            stall_probe=d.get("stall_probe"),
        )

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expiry_ts is None:
            return False
        return (now if now is not None else time.time()) >= self.expiry_ts


# ============================================================================================
# Persistence — single writer, bounded jsonl + monthly archive (house pattern)
# ============================================================================================
def _live_path(config) -> Path:
    return config.workspace / "quests.jsonl"


def _archive_path(config) -> Path:
    return config.state_dir / f"{ARCHIVE_PREFIX}{time.strftime('%Y%m')}.jsonl"


class QuestStore:
    """The single writer of quest state. Whole-file rewrite (the quest set is small — one active,
    a short queue, a bounded terminal tail), atomic via temp+replace. Rotation matches
    memory.truncate_observations / pressures.PressureLedger: when the live file crosses max_bytes,
    the oldest TERMINAL quests roll into a dated monthly archive and the live file keeps only the
    live/queued set plus a recent terminal tail."""

    def __init__(self, config, *, max_bytes: int = QUESTS_MAX_BYTES):
        self.config = config
        self.max_bytes = int(max_bytes)

    # --- read ---------------------------------------------------------------------------------
    def load(self) -> list[Quest]:
        path = _live_path(self.config)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out: list[Quest] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Quest.from_dict(json.loads(line)))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
        return out

    def active(self) -> Optional[Quest]:
        for q in self.load():
            if q.state == ACTIVE:
                return q
        return None

    def queue(self) -> list[Quest]:
        return [q for q in self.load() if q.state == OFFERED]

    def passed_count(self) -> int:
        """Adjudicated PASSES on the books — the honest fact behind a `quests.passed` criterion
        (§0.5: a pass is a glue-closed state in THIS store, never a tools_used attempt). Counts the
        live file plus every rotated monthly archive so rotation can't walk the number backwards."""
        n = sum(1 for q in self.load() if q.state == PASSED)
        try:
            for arc in self.config.state_dir.glob(f"{ARCHIVE_PREFIX}*.jsonl"):
                for line in arc.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if json.loads(line).get("state") == PASSED:
                            n += 1
                    except (ValueError, json.JSONDecodeError):
                        continue
        except OSError:
            pass
        return n

    # --- write --------------------------------------------------------------------------------
    def save(self, quests: list[Quest]) -> None:
        """Atomically rewrite the whole quest set, rotating terminal overflow to the archive first."""
        self.config.workspace.mkdir(parents=True, exist_ok=True)
        quests = self._rotate_if_needed(quests)
        path = _live_path(self.config)
        tmp = path.with_suffix(".jsonl.tmp")
        body = "\n".join(json.dumps(q.to_dict(), ensure_ascii=False) for q in quests)
        tmp.write_text(body + ("\n" if body else ""), encoding="utf-8")
        tmp.replace(path)

    def _rotate_if_needed(self, quests: list[Quest]) -> list[Quest]:
        """When the serialized set would exceed max_bytes, archive the OLDEST terminal quests and
        drop them from the live file. Live/queued quests are never archived; a small terminal tail
        stays for context. Returns the (possibly trimmed) list to persist."""
        body = "\n".join(json.dumps(q.to_dict(), ensure_ascii=False) for q in quests)
        if len(body.encode("utf-8")) < self.max_bytes:
            return quests
        live = [q for q in quests if q.state not in _TERMINAL]
        terminal = [q for q in quests if q.state in _TERMINAL]
        terminal.sort(key=lambda q: q.closed_ts or q.created_ts)
        keep_tail = terminal[-10:]          # declared: keep the last 10 closed quests inline
        archived = terminal[:-10]
        if archived:
            try:
                self.config.state_dir.mkdir(parents=True, exist_ok=True)
                with open(_archive_path(self.config), "a", encoding="utf-8", errors="replace") as f:
                    for q in archived:
                        f.write(json.dumps(q.to_dict(), ensure_ascii=False) + "\n")
            except OSError:
                pass
        return live + keep_tail


# ============================================================================================
# Reward sink — the payout seam (§0.5: payout through the standard XP path)
# ============================================================================================
def reward_xp_amount(reward: Optional[dict]) -> int:
    """The XP LEG of any reward, read in ONE place (TOOL_PROGRESSION: "it pays" pays limbs, not
    just XP — so a grant reward can carry both legs). A plain XP reward carries it as `amount`;
    an unlock/capacity reward may carry an `xp` field riding alongside, e.g.
    {"kind": "unlock", "what": "workshop", "xp": 50}. The XP leg always pays through the standard
    sink path; the unlock/capacity leg stays the grant seam's job (this engine never mints one)."""
    reward = reward or {}
    key = "amount" if reward.get("kind", REWARD_XP) == REWARD_XP else "xp"
    try:
        return int(reward.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def default_reward_sink(config, quest: "Quest", persona: Optional[dict] = None) -> None:
    """Apply a quest's reward via the standard XP path — the XP leg of ANY kind pays through
    persona.award_xp (reward_xp_amount reads it), so an unlock reward with an `xp` field still
    pays its XP. The `unlock`/`capacity` LEG is recorded on the quest (state persists it) and is
    the grant seam's job — this engine flags the intent; it does not unlock a tier. Requires a
    persona dict for XP payout; if none is supplied the caller wires the sink itself."""
    amount = reward_xp_amount(quest.reward)
    if amount > 0 and persona is not None:
        try:
            import persona as persona_mod
            # config MUST ride along: a config-less award recomputes level-from-XP, silently
            # bypassing the mastery gate (the maiden walk showed Lv.1↔Lv.3 flicker from this).
            persona_mod.award_xp(persona, amount, reason=f"quest:{quest.id}", config=config)
        except Exception:  # noqa: BLE001 - reward payout is best-effort, never brick the loop
            pass
    # 4.3b: a PASSED quest is mastery evidence (class pays 0 XP — the reward legs above are the
    # payout; the portfolio records the adjudicated fact). Best-effort, flag-gated inside.
    try:
        import mastery
        mastery.record_evidence(config, persona, "quest_passed", quest.id,
                                title=quest.directive or quest.id)
    except Exception:  # noqa: BLE001
        pass


# ============================================================================================
# The System — engine over the store
# ============================================================================================
class System:
    """The quest engine. One active quest at a time; state-driven cadence; glue adjudication.

    `reward_sink(config, quest)` is the payout seam — defaults to `default_reward_sink` bound so it
    calls persona.award_xp. `propose()` is the ONLY hook the Administrator (5.2) needs to enqueue.
    """

    def __init__(self, config, *, reward_sink: Optional[Callable[[Any, "Quest"], None]] = None):
        self.config = config
        self.store = QuestStore(config)
        self.reward_sink = reward_sink or (lambda cfg, q: default_reward_sink(cfg, q))

    # --- enqueue (the Administrator's only hook, §7a) ----------------------------------------
    def propose(self, quest: "Quest") -> "Quest":
        """Enqueue a proposed quest (state=offered). The seam the Administrator (5.2) enqueues
        through — it authors quests; this engine issues/tracks/adjudicates them. De-dups by id."""
        quests = self.store.load()
        if any(q.id == quest.id for q in quests):
            return next(q for q in quests if q.id == quest.id)
        quest.state = OFFERED
        quests.append(quest)
        self.store.save(quests)
        return quest

    def sweep_offered(self, *, now: Optional[float] = None) -> list["Quest"]:
        """Expire QUEUED (offered) quests whose deadline passed without ever being issued.

        check()/expire_if_due() only watch the ACTIVE quest; offered rows had no sweeper, so a
        dead offer sat in the queue forever — and could be promoted long after its deadline
        (observed live: three auto-issued quests stuck 'offered' days past expiry_ts). Runs at
        the queue's natural service point (issue_next) — event-driven, no timer. Returns the
        swept quests so the caller can surface them."""
        now = time.time() if now is None else now
        swept: list[Quest] = []
        quests = self.store.load()
        for q in quests:
            if q.state == OFFERED and q.is_expired(now):
                q.state = EXPIRED
                q.closed_ts = _now()
                q.outcome = "expired while offered (never engaged)"
                swept.append(q)
        if swept:
            self.store.save(quests)
        return swept

    # --- cadence: issue the next quest (the not-coddled state gate, §7) ----------------------
    def issue_next(self, *, sleeps_since_close: int, condition: str) -> Optional["Quest"]:
        """Promote one queued quest to ACTIVE — but ONLY when the not-coddled cadence permits:
          1. no quest is currently active (BIBLE L-2: exactly one at a time), AND
          2. the prior quest closed AND ≥ SLEEPS_REQUIRED sleep cycles have passed since
             (`sleeps_since_close` is fed in — this engine never reads the clock), AND
          3. condition is healthy — NOT in _HEALTHY_BLOCK (RECOVERY). Read from glue by the caller.
        Returns the newly active quest, or None (silence — the correct default, §7).

        The caller feeds `sleeps_since_close` (a counter it advances on each sleep boundary) and
        `condition` (glue.compute_condition). Event-driven, not scheduled (ARCH #1)."""
        self.sweep_offered()                  # a dead offer must never be promoted
        if self.store.active() is not None:
            return None                       # one active quest at a time — silence
        if condition in _HEALTHY_BLOCK:
            return None                       # silence is reserved for genuine RECOVERY
        if int(sleeps_since_close) < SLEEPS_REQUIRED:
            return None                       # mandatory digestion between challenges
        queue = self.store.queue()
        if not queue:
            return None
        # FIFO within the queue; the Administrator decides ordering by proposing in order.
        nxt = queue[0]
        quests = self.store.load()
        for q in quests:
            if q.id == nxt.id:
                q.state = ACTIVE
                break
        self.store.save(quests)
        return next(q for q in self.store.load() if q.id == nxt.id)

    # --- adjudication (glue judges; self-report never counts, §0.5) --------------------------
    def check(self, quest: "Quest", stats: dict, *, now: Optional[float] = None) -> dict:
        """Adjudicate the ACTIVE quest against the typed `stats` dict. GLUE judges — the criteria
        predicate is evaluated over typed state, never self-report.

        Order of resolution:
          - criteria met  → PASS: apply reward via the sink, close PASSED, return {passed, quest}.
          - expiry reached (unmet) → EXPIRED: close, return an episode-shaped failure-lite record.
          - otherwise      → still active, no change.

        Returns {"passed": bool, "expired": bool, "quest": Quest, "reveal": bool, "episode": dict?}.
        `reveal` is True on the tick a HIDDEN quest passes (the achievement announces on completion)."""
        now = time.time() if now is None else now
        result: dict = {"passed": False, "expired": False, "quest": quest, "reveal": False}

        if quest.state != ACTIVE:
            return result

        if quest.success_criteria.check(stats):
            self._close(quest, PASSED, outcome="criteria met (glue-adjudicated)")
            try:
                self.reward_sink(self.config, quest)
            except Exception:  # noqa: BLE001 - a payout failure must not brick adjudication
                pass
            result["passed"] = True
            result["reveal"] = bool(quest.hidden)   # hidden achievement announces on completion
            result["quest"] = quest
            return result

        if quest.is_expired(now):
            self._close(quest, EXPIRED, outcome="expired before criteria met")
            result["expired"] = True
            result["quest"] = quest
            result["episode"] = self._failure_lite_episode(quest)
            return result

        return result

    def expire_if_due(self, stats: Optional[dict] = None,
                      *, now: Optional[float] = None) -> Optional[dict]:
        """Close the active quest as EXPIRED if its expiry has passed and criteria are unmet.
        The 'ignoring a quest is itself recorded' path (§7) — call it on the sleep boundary or when
        the queue is being serviced. Returns the failure-lite episode record, or None.

        If `stats` is given, a quest whose criteria are ALREADY met is passed instead of expired
        (so a just-completed quest at the deadline still pays out). Otherwise expiry wins."""
        q = self.store.active()
        if q is None:
            return None
        now = time.time() if now is None else now
        if stats is not None and q.success_criteria.check(stats):
            r = self.check(q, stats, now=now)
            return None if r.get("passed") else r.get("episode")
        if q.is_expired(now):
            self._close(q, EXPIRED, outcome="expired / ignored before criteria met")
            return self._failure_lite_episode(q)
        return None

    def abandon_if_stalled(self, stats: Optional[dict]) -> Optional[dict]:
        """The K-sleep abandon path (TOOL_PROGRESSION stall handling): called ONCE per sleep
        boundary. Measures the active quest's criteria leaf values against the typed stats dict
        (glue-measured — the same _dig adjudication uses, never self-report) and compares them to
        the values measured at the LAST sleep. Zero movement → the stall counter advances; any
        movement → it resets. At QUEST_STALL_SLEEPS consecutive stalled sleeps the quest closes
        FAILED — the creature is demonstrably not moving this needle, and holding the line frozen
        just starves the quest line. Returns the failure-lite episode on an abandon, else None."""
        q = self.store.active()
        if q is None or stats is None:
            return None
        probe = {p: _dig(stats, p) for p in criteria_paths(q.success_criteria.to_dict())}
        if q.stall_probe is not None and probe == q.stall_probe:
            q.stall_sleeps += 1
        else:
            q.stall_sleeps = 0
            q.stall_probe = probe
        if q.stall_sleeps >= QUEST_STALL_SLEEPS:
            self._close(q, FAILED,
                        outcome=f"abandoned: zero criterion movement across "
                                f"{q.stall_sleeps} consecutive sleeps")
            return self._failure_lite_episode(q, fail_kind="quest_abandoned",
                                              summary=f"quest abandoned stalled: {q.directive}")
        self._persist(q)
        return None

    def _persist(self, quest: "Quest") -> None:
        """Write a mutated (still-live) quest back to the store — the non-closing sibling of
        _close, for bookkeeping fields like the stall counters."""
        quests = self.store.load()
        for i, q in enumerate(quests):
            if q.id == quest.id:
                quests[i] = quest
                break
        self.store.save(quests)

    def _close(self, quest: "Quest", state: str, *, outcome: str) -> None:
        quest.state = state
        quest.closed_ts = _now()
        quest.outcome = outcome
        self._persist(quest)

    def _failure_lite_episode(self, quest: "Quest", *, fail_kind: str = "quest_expired",
                              summary: str = "") -> dict:
        """An EPISODE-SHAPED failure-lite record (§7: a failed/ignored quest becomes an episode).
        This engine RETURNS it; it does NOT write episodes itself (episodes.py owns that store).
        Shape mirrors episodes.py's {situation→action→outcome→fix} so the caller can hand it
        straight to the episodic store."""
        return {
            "key": f"quest|{quest.id}",
            "obj": "system_quest",
            "step": quest.directive,
            "tool": "quest",
            "sig": quest.id,
            "fail_kind": fail_kind,
            "success": False,
            "summary": summary or f"quest expired unmet: {quest.directive}",
            "tier": quest.tier,
            "ts": _now(),
        }


# ============================================================================================
# Daily quests — recurring drill slots (§7). Factory of quest OBJECTS + criteria HOOKS only;
# the actual drills (scar retests / calibration / backup) are wired by a LATER phase.
# ============================================================================================
# Each daily kind maps to a criteria hook: a name for the glue-checkable stat the drill will set,
# plus the op/threshold. The drill machinery (extinction trials, calibration harness, backup
# restore-verify) writes that stat; this engine only issues the quest and adjudicates the stat.
_DAILY_KINDS: dict[str, dict] = {
    # scar_retest: an extinction trial (pitfall #4) — the drill records a pass into
    # skills.scar_retest_passed; the quest demands it be true today.
    "scar_retest": {
        "directive": "Retest a scar. Re-attempt a past failure to prove it no longer binds you.",
        "criterion": {"path": "drills.scar_retest_passed", "op": "==", "value": True},
        "reward": {"kind": REWARD_XP, "amount": 10},
    },
    # calibration_drill: a confidence-vs-outcome calibration exercise; the drill sets a score.
    "calibration_drill": {
        "directive": "Run a calibration drill. Predict, then measure; close the gap.",
        "criterion": {"path": "drills.calibration_score", "op": ">=", "value": 0.7},
        "reward": {"kind": REWARD_XP, "amount": 10},
    },
    # backup_verify: a restore-verify of a backup (pitfall maintenance); the drill sets a flag.
    "backup_verify": {
        "directive": "Verify a backup. Restore it and confirm it is whole.",
        "criterion": {"path": "drills.backup_verified", "op": "==", "value": True},
        "reward": {"kind": REWARD_XP, "amount": 8},
    },
}

DAILY_KINDS = tuple(_DAILY_KINDS.keys())


def daily_quest(kind: str, *, tier: int = 1, expiry_ts: Optional[float] = None,
                id_suffix: str = "") -> Quest:
    """Build a recurring drill quest of `kind` (one of DAILY_KINDS). Returns just the quest object
    with its glue-checkable criterion hook — the drill that SETS the criterion's stat is wired by a
    LATER phase. `expiry_ts` defaults to end-of-day-style behavior chosen by the caller; a daily
    quest that lapses records a failure-lite episode like any other (not-coddled)."""
    spec = _DAILY_KINDS.get(kind)
    if spec is None:
        raise ValueError(f"unknown daily quest kind: {kind!r} (known: {', '.join(DAILY_KINDS)})")
    qid = f"daily_{kind}" + (f"_{id_suffix}" if id_suffix else "")
    return Quest(
        id=qid,
        directive=spec["directive"],
        success_criteria=Criterion.from_dict(spec["criterion"]),
        reward=dict(spec["reward"]),
        tier=tier,
        expiry_ts=expiry_ts,
        hidden=False,
        kind="daily",
    )


# ============================================================================================
# Rendering — the System's terse register (a LATER phase injects it into context)
# ============================================================================================
def render_active(quest: Optional["Quest"]) -> str:
    """Render the active quest as a terse, distinct 'System' register string for context (§7).
    Impersonal — not Dean, not self-talk. A HIDDEN quest is NOT rendered while offered/active
    (achievements announce only on completion). Returns '' when there is nothing to show — a LATER
    phase injects this block; this module never touches eidos.py/context.py.

    The register is deliberately unmistakable (bracketed, uppercase SYSTEM header) so the creature
    reads it as an external authority, distinct from operator chat and its own thoughts."""
    if quest is None or quest.state != ACTIVE:
        return ""
    if quest.hidden:
        return ""    # hidden while active — reveals on pass via render_reveal / check(reveal=True)
    lines = [
        "╔══ SYSTEM ══════════════════════════════════════",
        f"║ QUEST [T{quest.tier}]  {quest.directive}",
        f"║ REWARD  {_reward_str(quest.reward)}",
    ]
    if quest.expiry_ts is not None:
        lines.append(f"║ EXPIRES {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(quest.expiry_ts))}")
    lines.append("╚════════════════════════════════════════════════")
    return "\n".join(lines)


def render_reveal(quest: "Quest") -> str:
    """The completion announcement for a HIDDEN quest — 'the System sees everything' (§7). A LATER
    phase surfaces this the tick check() returns reveal=True."""
    return "\n".join([
        "╔══ SYSTEM ══════════════════════════════════════",
        f"║ ACHIEVEMENT UNLOCKED  {quest.directive}",
        f"║ REWARD  {_reward_str(quest.reward)}",
        "╚════════════════════════════════════════════════",
    ])


def _reward_str(reward: dict) -> str:
    reward = reward or {}
    kind = reward.get("kind", REWARD_XP)
    if kind == REWARD_XP:
        return f"{reward.get('amount', 0)} XP"
    # A non-XP reward may carry an XP leg alongside (reward_xp_amount) — the window states BOTH
    # legs, because both are what the System actually pays.
    xp_leg = reward_xp_amount(reward)
    tail = f" +{xp_leg} XP" if xp_leg > 0 else ""
    if kind == REWARD_UNLOCK:
        # The unit's NAME stays sealed until it is paid (§0 no-teasing: this string renders in
        # the creature's window every tick from issuance to pass — "unlock: workshop" would
        # pre-announce a locked rung for days). The System promises SOMETHING, never what.
        return f"a new ability{tail}"
    if kind == REWARD_CAPACITY:
        return f"+{reward.get('amount', 1)} capacity: {reward.get('what', '?')}{tail}"
    return str(kind)
