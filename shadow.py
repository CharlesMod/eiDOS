"""Pillars 6 — Shadows: scripted CPU workers (PILLARS_PLAN.md §5a).

NOT biomimetic, and not trying to be: a shadow = **a trusted skill + a loop + a budget + a
lease**, detached into a killable subprocess. No LLM anywhere in it. The mind-in-a-can
under-utilizes a 20-thread CPU; shadows are those hands.

Schema (plan §5a):
    body:     ref to a TRUSTED skill/composition — trust before delegation, validated at spawn.
    loop:     bus_subscription(topics) | schedule | watch_condition — event-driven, never
              poll-sleep (the monarch tick IS the event that evaluates triggers; nothing here
              ever sleeps waiting for anything).
    budget:   {energy_per_day, max_runtime_s, max_actions_per_hr}.
    lease:    expiry renewed ONLY by a live monarch tick (`ShadowRoster.tick()`) — the dead-man
              switch: monarch stays dead → the shadow winds down at expiry.
    mailbox:  results published to the bus as NervousEvents — but report by EXCEPTION
              (pitfall #7): routine output routes to a bounded, sleep-digestible summary store;
              ONLY anomalies (failures, budget events, watch-condition fires) publish salient.
    standing: {outcomes, strikes} — rent-negative standing accrues strikes; strikes →
              auto-dissolve + an error engram (via the Consolidator, fail-open).

Doctrine (plan §5a bullets):
  - **Shadows are organs on the bus**: their output competes for admission through the salience
    gate like any sense; the monarch never blocks on one; a crashed shadow is a severed nerve
    (I5) — logged, standing hit, never a raised exception into the caller.
  - **Rent must be paid**: each shadow draws a metabolic stipend (drains the reserve); delivered
    results earn credit against upkeep; one that stops earning starves visibly until dissolving
    it relieves the pressure.
  - **Shadows spawn nothing** — asserted STRUCTURALLY: the body runs via the Phase 1.2 killable
    subprocess pool (`skills.run_skill_killable`), whose namespace is `skill_atoms.build_atoms`
    — which contains NO skill-creation or shadow-spawn atom, and no roster handle ever crosses
    into the subprocess. Belt-and-braces: the body's source is scanned for spawn-capable names
    at spawn AND before every run (the active version can change via edit_skill); a violation
    fails soft — recorded, struck, never executed.

Execution REUSES the Phase 1.2 pool (skills.run_skill_killable): one-shot, hard-killable
subprocess per invocation — a hang dies with the process, never freezing a tick.

Dark behind `config.pillars_shadows_enabled`: flag off → spawn/tick are no-ops.
Capacity = `config.pillars_shadow_capacity` (grows on demonstrated stewardship, not level).
Proprioception seam: `roster()` returns each shadow's state (dashboard + felt state — exposed
here, wired at cutover). `dissolve(shadow_id)` backs the dashboard's dissolve button.
"""

import ast
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from config import Config
from tools import ToolResult
from skills import run_skill_killable, _load_manifest, _skill_file, _active_version
from skill_atoms import ATOM_NAMES
from nervous.event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION

logger = logging.getLogger("eidos.shadow")

STATE_NAME = "shadows.json"           # roster persistence, under config.state_dir
DIGEST_NAME = "shadow_digest.jsonl"   # the sleep-digestible routine-output store (bounded)

# --- Declared knobs (§0.4: every constant carries its one-line justification) -------------------
_MAX_STRIKES = 3                 # three independent faults before dissolution — a crash LOOP or a
                                 # starving WEEK dissolves, one bad moment does not.
_DEFAULT_LEASE_S = 900.0         # 15 min: dozens of monarch ticks fit inside, so a live monarch
                                 # never lets it lapse; a dead one winds shadows down within the hour.
_DEFAULT_ENERGY_PER_DAY = 0.05   # default stipend: 5% of the reserve per shadow-day — felt, but a
                                 # single earner can't starve the creature by itself.
_DEFAULT_MAX_RUNTIME_S = 30.0    # default per-invocation wall-clock — matches the historical skill
                                 # watchdog scale (a shadow body is a skill, not a batch job).
_RUNTIME_CEILING_S = 180.0       # hard ceiling on ANY declared max_runtime — the same ceiling the
                                 # async bash model uses (cmd_async_ceiling_s); no body outlives it.
_DEFAULT_MAX_ACTIONS_PER_HR = 60 # default action cap: one run/min sustained — plenty for a watcher,
                                 # a bound for a flood.
_ACTIONS_WINDOW_S = 3600.0       # the max_actions/hr accounting window (it IS the "/hr").
_RESULT_CREDIT_ENERGY = 0.01     # a delivered result earns 1/5 of the default daily stipend, so a
                                 # shadow must deliver ~5 results/day to pay its default rent.
_RENT_SETTLE_S = 3600.0          # rent-negative standing is judged at most hourly — a slow hour is
                                 # not a strike; a starving day accrues them.
_RENT_STRIKE_DEBT_DAYS = 1.0     # the debt that earns a rent strike: one full unearned day of
                                 # stipend — aligned to the stipend's own unit, not a second guess.
_DIGEST_MAX_ITEMS = 200          # bound on the routine-output store (pitfall #7's summary lane) —
                                 # sleep digests a bounded window, never an unbounded log.
_ANOMALY_SALIENCE = 0.7          # anomalies compete strongly at the salience gate but stay below
                                 # the reliable floor — an exception report is loud, not sovereign.
_BUS_DRAIN_MAX_PER_TICK = 5      # bound on subscription events consumed per shadow per tick, so an
                                 # event flood can't monopolize a tick with one shadow's work.

# Spawn-capable names a shadow body must never reference (belt-and-braces on top of the structural
# guarantee that the subprocess namespace contains no such callable). Checked by AST name/attr scan.
NO_SPAWN_NAMES = frozenset({
    "create_skill", "edit_skill", "rollback_skill", "apply_promotion",
    "propose_self_edit", "spawn", "ShadowRoster",
})

_LOOP_TYPES = ("schedule", "bus_subscription", "watch_condition")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------------------------------------------
# Structural no-spawn checks
# ------------------------------------------------------------------------------------------------

def assert_atoms_spawn_nothing() -> None:
    """The structural invariant, asserted: the atom vocabulary a shadow body executes with contains
    NO skill-creation / shadow-spawn atom. Raises AssertionError if the vocabulary ever grows one —
    a tripwire for future edits to skill_atoms, run by the test gate."""
    overlap = set(ATOM_NAMES) & NO_SPAWN_NAMES
    assert not overlap, f"atom namespace grew spawn-capable atoms: {overlap}"


def _spawn_violations(source: str) -> list[str]:
    """AST scan of a body's source for references to spawn-capable names (bare names AND attribute
    access like `skills.create_skill`). Returns the names found; empty == clean. A parse failure is
    itself a violation — an unreadable body is not a trustable body."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ["<unparseable source>"]
    found: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in NO_SPAWN_NAMES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in NO_SPAWN_NAMES:
            found.add(node.attr)
    return sorted(found)


# ------------------------------------------------------------------------------------------------
# The roster — the monarch-side manager (shadows have no API back into it)
# ------------------------------------------------------------------------------------------------

class ShadowRoster:
    """Owns the shadow records, their leases, their economics, and their execution. Every entry
    point is guarded (I5): a shadow fault is logged and recorded in standing — never an exception
    into the caller's tick. The roster exposes NO spawn API to shadow bodies: bodies run in the
    killable subprocess pool with the atom namespace only; nothing of this class crosses over."""

    def __init__(self, config: Config, bus=None, consolidator=None, metabolism=None):
        self.config = config
        self.bus = bus
        self._consolidator = consolidator
        self._metabolism = metabolism
        self._subs: dict = {}      # shadow_id -> bus Subscription (bus_subscription loops)
        self._state: dict = {"shadows": {}}
        self._load()
        self._resubscribe()

    # ---- config gates ----------------------------------------------------------------
    def _enabled(self) -> bool:
        return bool(getattr(self.config, "pillars_shadows_enabled", False))

    def _capacity(self) -> int:
        return int(getattr(self.config, "pillars_shadow_capacity", 1))

    # ---- persistence (survives monarch restart; the lease decides what resumes) -------
    def _state_path(self) -> Path:
        return self.config.state_dir / STATE_NAME

    def _digest_path(self) -> Path:
        return self.config.state_dir / DIGEST_NAME

    def _load(self) -> None:
        try:
            self._state = json.loads(self._state_path().read_text(encoding="utf-8"))
            if not isinstance(self._state.get("shadows"), dict):
                self._state = {"shadows": {}}
        except (OSError, ValueError):
            self._state = {"shadows": {}}

    def _save(self) -> None:
        try:
            p = self._state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            logger.warning("shadow roster save failed: %s", e)

    def _resubscribe(self) -> None:
        """Re-attach bus subscriptions for live bus_subscription shadows after a restart —
        Subscription objects don't persist; the roster record is the durable part."""
        if self.bus is None:
            return
        for sid, sh in self._state["shadows"].items():
            if sh.get("status") == "live" and sh["loop"].get("type") == "bus_subscription":
                self._subscribe(sid, sh)

    def _subscribe(self, sid: str, sh: dict) -> None:
        try:
            topics = {(Kind(k), Modality(m)) for k, m in sh["loop"].get("topics") or []}
            self._subs[sid] = self.bus.subscribe(topics=topics)
        except Exception as e:  # noqa: BLE001 - a bad subscription is a severed nerve, not a crash
            logger.warning("shadow %s could not subscribe: %s", sid, e)

    def _unsubscribe(self, sid: str) -> None:
        sub = self._subs.pop(sid, None)
        if sub is not None and self.bus is not None:
            try:
                self.bus.unsubscribe(sub)
            except Exception:  # noqa: BLE001
                pass

    # ---- spawn (trust before delegation) ----------------------------------------------
    def spawn(self, body: str, loop: dict, budget: Optional[dict] = None,
              lease_s: Optional[float] = None) -> dict:
        """Create a shadow. Refuses (soft — a dict, never an exception): flag off, capacity full,
        untrusted body, invalid loop, spawn-capable references in the body source."""
        if not self._enabled():
            return {"ok": False, "reason": "shadows are disabled (pillars_shadows_enabled=false)"}

        live = [s for s in self._state["shadows"].values() if s.get("status") == "live"]
        if len(live) >= self._capacity():
            return {"ok": False, "reason": f"capacity full ({len(live)}/{self._capacity()}) — "
                                           f"dissolve a shadow before spawning another"}

        # Trust before delegation: only a TRUSTED skill may become a body.
        ent = _load_manifest(self.config).get("skills", {}).get(body)
        if ent is None:
            return {"ok": False, "reason": f"no such skill '{body}'"}
        if ent.get("status") != "trusted":
            return {"ok": False, "reason": f"skill '{body}' is {ent.get('status') or 'unknown'}, "
                                           f"not trusted — a shadow body must have EARNED trust first"}

        # Shadows spawn nothing: scan the body source for spawn-capable references.
        viol = self._body_violations(body)
        if viol:
            return {"ok": False, "reason": f"body '{body}' references spawn-capable names "
                                           f"{viol} — shadows spawn nothing", "violations": viol}

        loop = dict(loop or {})
        ltype = loop.get("type")
        if ltype not in _LOOP_TYPES:
            return {"ok": False, "reason": f"loop.type must be one of {_LOOP_TYPES}, got {ltype!r}"}
        if ltype == "schedule" and not (float(loop.get("every_s") or 0) > 0):
            return {"ok": False, "reason": "schedule loop needs every_s > 0"}
        if ltype == "bus_subscription":
            try:
                topics = [(Kind(k).value, Modality(m).value) for k, m in loop.get("topics") or []]
            except (ValueError, TypeError):
                return {"ok": False, "reason": "bus_subscription loop needs topics=[[kind, modality], ...]"}
            if not topics:
                return {"ok": False, "reason": "bus_subscription loop needs at least one topic"}
            loop["topics"] = topics

        b = dict(budget or {})
        budget = {
            "energy_per_day": max(0.0, float(b.get("energy_per_day", _DEFAULT_ENERGY_PER_DAY))),
            "max_runtime_s": min(_RUNTIME_CEILING_S,
                                 max(1.0, float(b.get("max_runtime_s", _DEFAULT_MAX_RUNTIME_S)))),
            "max_actions_per_hr": max(1, int(b.get("max_actions_per_hr", _DEFAULT_MAX_ACTIONS_PER_HR))),
        }

        now = time.time()
        dur = max(0.001, float(lease_s if lease_s is not None else _DEFAULT_LEASE_S))
        sid = _new_id()
        sh = {
            "id": sid,
            "body": body,
            "loop": loop,
            "budget": budget,
            "lease": {"duration_s": dur, "expires_at": now + dur},
            "status": "live",
            "standing": {"outcomes": {"ok": 0, "fail": 0}, "strikes": 0, "strike_reasons": [],
                         "violations": [], "credit_earned": 0.0, "upkeep_paid": 0.0,
                         "last_rent_strike": 0.0},
            "created": _now_iso(),
            "last_run_ts": None,
            "last_upkeep_ts": now,
            "watch_baseline": None,
            "run_ts": [],
        }
        self._state["shadows"][sid] = sh
        if ltype == "bus_subscription" and self.bus is not None:
            self._subscribe(sid, sh)
        self._save()
        logger.info("shadow %s spawned: body=%s loop=%s", sid, body, ltype)
        return {"ok": True, "shadow_id": sid}

    def _body_violations(self, body: str) -> list[str]:
        """Spawn-capable names referenced by the body's ACTIVE version source. Re-checked before
        every run — edit_skill can change the active version after spawn."""
        try:
            src = _skill_file(self.config, body, _active_version(self.config, body)) \
                .read_text(encoding="utf-8")
        except OSError:
            return ["<unreadable source>"]
        return _spawn_violations(src)

    # ---- the monarch tick: lease renewal, economics, triggers -------------------------
    def tick(self, now: Optional[float] = None) -> dict:
        """One live monarch tick over the roster. THIS is the only thing that renews a lease —
        the dead-man switch: no ticks (dead monarch) → leases lapse → shadows wind down.
        Never raises (I5); returns a summary dict for the ledger/dashboard."""
        if not self._enabled():
            return {}
        now = time.time() if now is None else float(now)
        summary = {"renewed": 0, "ran": 0, "wound_down": [], "dissolved": [], "anomalies": 0}
        try:
            for sid, sh in list(self._state["shadows"].items()):
                if sh.get("status") != "live":
                    continue
                try:
                    self._tick_one(sid, sh, now, summary)
                except Exception as e:  # noqa: BLE001 - a shadow fault is a severed nerve (I5)
                    logger.warning("shadow %s tick fault (severed nerve): %s", sid, e)
                    self._record_outcome(sh, ok=False)
                    self._strike(sh, f"tick fault: {type(e).__name__}", now, summary)
            self._save()
        except Exception as e:  # noqa: BLE001 - the roster must never wound the monarch's tick
            logger.warning("shadow roster tick fault: %s", e)
        return summary

    def _tick_one(self, sid: str, sh: dict, now: float, summary: dict) -> None:
        # 1. Lease: expired means the monarch was silent past expiry — wind down, do NOT renew.
        if now > float(sh["lease"]["expires_at"]):
            self._wind_down(sid, sh, now, summary)
            return
        sh["lease"]["expires_at"] = now + float(sh["lease"]["duration_s"])
        summary["renewed"] += 1

        # 2. Rent: stipend drains the reserve; results earned credit against it (ledger below).
        self._charge_upkeep(sh, now)
        self._settle_rent(sh, now, summary)
        if self._maybe_auto_dissolve(sid, sh, now, summary):
            return

        # 3. Trigger: event-driven — the tick evaluates; nothing ever poll-sleeps.
        for args in self._due_runs(sid, sh, now, summary):
            self._run_shadow(sid, sh, args, now, summary)
            if sh.get("status") != "live":
                return
        self._maybe_auto_dissolve(sid, sh, now, summary)

    # ---- economics on metabolism -------------------------------------------------------
    def _charge_upkeep(self, sh: dict, now: float) -> None:
        elapsed = max(0.0, now - float(sh.get("last_upkeep_ts") or now))
        sh["last_upkeep_ts"] = now
        upkeep = float(sh["budget"]["energy_per_day"]) * (elapsed / 86400.0)
        if upkeep <= 0.0:
            return
        sh["standing"]["upkeep_paid"] = round(float(sh["standing"]["upkeep_paid"]) + upkeep, 9)
        self._drain_reserve(upkeep)

    def _drain_reserve(self, amount: float) -> None:
        """Drain the metabolic reserve (the stipend is FELT — same reserve the tick loop charges).
        Fail-open: a missing reserve never blocks the roster."""
        try:
            met = self._metabolism
            if met is None:
                from nervous.metabolism import Metabolism
                self.config.state_dir.mkdir(parents=True, exist_ok=True)
                met = self._metabolism = Metabolism(config=self.config)
            met.feed(-abs(float(amount)))
            met._save()
        except Exception as e:  # noqa: BLE001
            logger.warning("shadow upkeep drain failed (%.6f): %s", amount, e)

    @staticmethod
    def _balance(sh: dict) -> float:
        return round(float(sh["standing"]["credit_earned"]) - float(sh["standing"]["upkeep_paid"]), 9)

    def _settle_rent(self, sh: dict, now: float, summary: dict) -> None:
        """Rent-negative standing accrues strikes — at most one per settlement window, once the
        debt passes a full unearned day of stipend. The starving state is visible in roster()."""
        debt_limit = float(sh["budget"]["energy_per_day"]) * _RENT_STRIKE_DEBT_DAYS
        if self._balance(sh) >= -debt_limit:
            return
        if now - float(sh["standing"].get("last_rent_strike") or 0.0) < _RENT_SETTLE_S:
            return
        sh["standing"]["last_rent_strike"] = now
        self._strike(sh, "rent-negative: upkeep unearned past a full day's stipend", now, summary)

    # ---- triggers ------------------------------------------------------------------------
    def _due_runs(self, sid: str, sh: dict, now: float, summary: dict) -> list[dict]:
        """Evaluate this shadow's loop; returns the arg-dicts to run NOW, bounded by the action
        budget. An attempted overrun is a budget event: recorded, salient, struck (once/window)."""
        ltype = sh["loop"]["type"]
        due: list[dict] = []
        if ltype == "schedule":
            last = sh.get("last_run_ts")
            if last is None or now - float(last) >= float(sh["loop"]["every_s"]):
                due.append({"trigger": "schedule", **(sh["loop"].get("args") or {})})
        elif ltype == "watch_condition":
            # The body IS the probe: run it each tick; a CHANGED observation is the fire (below).
            due.append({"trigger": "watch", **(sh["loop"].get("args") or {})})
        elif ltype == "bus_subscription":
            sub = self._subs.get(sid)
            if sub is not None and self.bus is not None:
                for _ in range(_BUS_DRAIN_MAX_PER_TICK):
                    ev = self.bus.recv(sub, timeout=0)
                    if ev is None:
                        break
                    self.bus.ack(ev)
                    due.append({"trigger": "bus_event", "event": ev.to_wire(),
                                **(sh["loop"].get("args") or {})})

        # Action budget: prune the rolling window, cap what runs, strike an overrun attempt.
        window = [t for t in sh.get("run_ts") or [] if now - t < _ACTIONS_WINDOW_S]
        sh["run_ts"] = window
        allowed = max(0, int(sh["budget"]["max_actions_per_hr"]) - len(window))
        if len(due) > allowed:
            skipped = len(due) - allowed
            due = due[:allowed]
            self._publish_anomaly(sh, "budget_violation",
                                  {"which": "max_actions_per_hr", "skipped": skipped})
            summary["anomalies"] += 1
            if now - float(sh["standing"].get("last_budget_strike") or 0.0) >= _ACTIONS_WINDOW_S:
                sh["standing"]["last_budget_strike"] = now
                self._strike(sh, "budget violation: max_actions_per_hr exceeded", now, summary)
        return due

    # ---- execution (the Phase 1.2 killable pool, reused) ----------------------------------
    def _run_shadow(self, sid: str, sh: dict, args: dict, now: float, summary: dict) -> None:
        """One body invocation. A crashing shadow is a severed nerve (I5): the runner returns,
        logs, hits standing — no exception propagates to the tick."""
        # Belt-and-braces no-spawn check at RUN time (the active version may have changed).
        viol = self._body_violations(sh["body"])
        if viol:
            sh["standing"]["violations"].append(
                {"ts": _now_iso(), "names": viol, "what": "spawn_attempt"})
            self._publish_anomaly(sh, "spawn_violation", {"names": viol})
            summary["anomalies"] += 1
            self._strike(sh, f"body references spawn-capable names {viol}", now, summary)
            return

        sh["run_ts"].append(now)
        sh["last_run_ts"] = now
        summary["ran"] += 1
        try:
            res = run_skill_killable(self.config, sh["body"], args,
                                     float(sh["budget"]["max_runtime_s"]))
        except Exception as e:  # noqa: BLE001 - severed nerve: never let a body wound the tick
            logger.warning("shadow %s body raised through the pool (severed nerve): %s", sid, e)
            res = ToolResult(output=f"[shadow body fault] {type(e).__name__}: {e}",
                             full_output_path=None, success=False, duration_s=0.0,
                             fail_kind="crash")

        if res.success:
            self._record_outcome(sh, ok=True)
            sh["standing"]["credit_earned"] = round(
                float(sh["standing"]["credit_earned"]) + _RESULT_CREDIT_ENERGY, 9)
            self._route_result(sh, args, res, summary)
        else:
            self._record_outcome(sh, ok=False)
            self._publish_anomaly(sh, "run_failed",
                                  {"fail_kind": res.fail_kind, "output": str(res.output)[:500]})
            summary["anomalies"] += 1
            what = ("budget violation: max_runtime_s exceeded (killed)"
                    if res.fail_kind == "timeout" else f"body failed ({res.fail_kind})")
            self._strike(sh, what, now, summary)

    def _route_result(self, sh: dict, args: dict, res: ToolResult, summary: dict) -> None:
        """Report by exception (pitfall #7): a watch-condition CHANGE fires salient; everything
        routine lands in the bounded sleep digest — never eating the attention it freed."""
        out = str(res.output or "")
        if sh["loop"]["type"] == "watch_condition":
            baseline = sh.get("watch_baseline")
            sh["watch_baseline"] = out
            if baseline is not None and out != baseline:
                self._publish_anomaly(sh, "watch_fired",
                                      {"was": str(baseline)[:300], "now": out[:300]})
                summary["anomalies"] += 1
                return
        self._digest_append({"ts": _now_iso(), "shadow": sh["id"], "body": sh["body"],
                             "trigger": args.get("trigger"), "output": out[:500]})

    @staticmethod
    def _record_outcome(sh: dict, ok: bool) -> None:
        sh["standing"]["outcomes"]["ok" if ok else "fail"] += 1

    # ---- strikes / dissolution -------------------------------------------------------------
    def _strike(self, sh: dict, reason: str, now: float, summary: dict) -> None:
        sh["standing"]["strikes"] = int(sh["standing"]["strikes"]) + 1
        sh["standing"]["strike_reasons"].append({"ts": _now_iso(), "reason": reason})
        logger.info("shadow %s strike %d: %s", sh["id"], sh["standing"]["strikes"], reason)

    def _maybe_auto_dissolve(self, sid: str, sh: dict, now: float, summary: dict) -> bool:
        if sh.get("status") == "live" and int(sh["standing"]["strikes"]) >= _MAX_STRIKES:
            reasons = "; ".join(r["reason"] for r in sh["standing"]["strike_reasons"][-_MAX_STRIKES:])
            self._dissolve_record(sid, sh, f"auto-dissolved at {_MAX_STRIKES} strikes: {reasons}")
            self._error_engram(sh, reasons)
            summary["dissolved"].append(sid)
            summary["anomalies"] += 1
            return True
        return False

    def _error_engram(self, sh: dict, reasons: str) -> None:
        """An auto-dissolve leaves a scar: an `error` engram through the Consolidator (the single
        long-term writer). Fail-open — memory of the failure is wanted, never load-bearing."""
        try:
            from engram import Engram, Consolidator
            cons = self._consolidator or Consolidator(self.config)
            cons.commit(Engram(
                kind="error",
                body=(f"shadow '{sh['body']}' ({sh['id']}) auto-dissolved after "
                      f"{sh['standing']['strikes']} strikes: {reasons}"),
                provenance="experienced"))
        except Exception as e:  # noqa: BLE001
            logger.warning("shadow %s error-engram commit failed (fail-open): %s", sh["id"], e)

    def _wind_down(self, sid: str, sh: dict, now: float, summary: dict) -> None:
        """The dead-man switch fired: the monarch was silent past lease expiry. The shadow stops
        drawing stipend, stops triggering, and waits dissolved-in-place for review."""
        sh["status"] = "wound_down"
        sh["wound_down_at"] = _now_iso()
        self._unsubscribe(sid)
        self._publish_anomaly(sh, "lease_expired", {"expired_at": sh["lease"]["expires_at"]})
        summary["wound_down"].append(sid)
        summary["anomalies"] += 1
        logger.info("shadow %s wound down (lease expired — dead-man switch)", sid)

    def dissolve(self, shadow_id: str, reason: str = "operator") -> dict:
        """Dissolve a shadow (the dashboard button / the monarch's own call). Relieves the rent
        pressure: a dissolved shadow draws no stipend. Soft on every failure path."""
        sh = self._state["shadows"].get(shadow_id)
        if sh is None:
            return {"ok": False, "reason": f"no such shadow '{shadow_id}'"}
        if sh.get("status") == "dissolved":
            return {"ok": True, "shadow_id": shadow_id, "already": True}
        self._dissolve_record(shadow_id, sh, reason)
        self._save()
        return {"ok": True, "shadow_id": shadow_id}

    def _dissolve_record(self, sid: str, sh: dict, reason: str) -> None:
        sh["status"] = "dissolved"
        sh["dissolved_at"] = _now_iso()
        sh["dissolve_reason"] = str(reason)[:300]
        self._unsubscribe(sid)
        self._publish_anomaly(sh, "dissolved", {"reason": str(reason)[:300]})
        logger.info("shadow %s dissolved: %s", sid, reason)

    # ---- mailbox: anomalies → salient NervousEvents; routine → the digest --------------------
    def _publish_anomaly(self, sh: dict, what: str, detail: dict) -> None:
        """ONLY anomalies publish (pitfall #7). They ride the bus as ordinary percepts — competing
        for admission through the salience gate like any sense; the monarch never blocks on one."""
        if self.bus is None:
            return
        try:
            # detail first, identity keys last — a detail key can never clobber the anomaly type.
            payload = json.dumps({**detail, "shadow": sh["id"], "body": sh["body"],
                                  "what": what}, ensure_ascii=False).encode("utf-8")
            ev = NervousEvent(SCHEMA_VERSION, f"shadow.{sh['id']}", Kind.percept,
                              Modality.system, Delivery.fungible,
                              salience=_ANOMALY_SALIENCE, t=time.monotonic())
            self.bus.publish(ev, payload)
        except Exception as e:  # noqa: BLE001 - reporting must never wound the roster
            logger.warning("shadow %s anomaly publish failed: %s", sh["id"], e)

    def _digest_append(self, entry: dict) -> None:
        """The sleep-digestible routine lane — bounded: past 2× the cap the file is rewritten to
        the newest _DIGEST_MAX_ITEMS lines, so it can never grow without limit."""
        try:
            p = self._digest_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            lines = p.read_text(encoding="utf-8").splitlines()
            if len(lines) > 2 * _DIGEST_MAX_ITEMS:
                tmp = p.with_suffix(".jsonl.tmp")
                tmp.write_text("\n".join(lines[-_DIGEST_MAX_ITEMS:]) + "\n", encoding="utf-8")
                os.replace(tmp, p)
        except OSError as e:
            logger.warning("shadow digest append failed: %s", e)

    def digest(self, max_items: int = _DIGEST_MAX_ITEMS) -> list[dict]:
        """Read the routine-output digest (newest last) — the sleep engine's consumption surface."""
        try:
            lines = self._digest_path().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines[-max_items:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    # ---- proprioception: the felt roster ------------------------------------------------------
    def roster(self) -> list[dict]:
        """Each shadow's state — the proprioception seam ("my shadows and what they're doing").
        Backs the dashboard roster (with its dissolve button) and the creature's felt state; this
        module exposes it, cutover wires it. `starving` makes rent-negative standing VISIBLE."""
        out = []
        for sid, sh in self._state["shadows"].items():
            balance = self._balance(sh)
            out.append({
                "id": sid,
                "body": sh["body"],
                "loop": sh["loop"]["type"],
                "status": sh["status"],
                "lease_expires_at": sh["lease"]["expires_at"],
                "last_run_ts": sh.get("last_run_ts"),
                "outcomes": dict(sh["standing"]["outcomes"]),
                "strikes": int(sh["standing"]["strikes"]),
                "violations": len(sh["standing"]["violations"]),
                "balance": balance,
                "starving": bool(sh["status"] == "live" and balance < 0.0),
                "budget": dict(sh["budget"]),
            })
        return out
