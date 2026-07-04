"""Pillars 7: generals — scoped LLM minds on mission contracts (PILLARS_PLAN §5b, PILLARS_TODO
Phase 7). NOT biomimetic (plan §5): the mind-in-a-can under-utilizes its substrate; a general is
plain engineering — a delegated, budgeted, disposable second context on the same model.

A general is a scoped mind on ONE mission. The contract is the whole relationship:

  Mission {
    objective:    exactly one,
    context_pack: monarch-compiled (relevant engrams via memory-manager recall + granted skills +
                  constraints — a MECHANICAL compile, never a general browsing the monarch's memory),
    grant:        attenuated capability set — only what the mission needs, validated ⊆ the
                  monarch's own (and NEVER a spawn-class capability: generals spawn nothing),
    budget:       { tokens, energy, wall_clock },
    report:       typed findings schema, grammar-constrained BOTH directions — the general's
                  decode is GBNF-constrained to the report shape (build_report_grammar), and
                  ingestion re-validates the SAME schema + bounds (validate_report), so a
                  malformed report is unrepresentable outbound and dropped-with-log inbound,
    escalation:   conditions that wake the monarch — published as reliable-class bus events
  }

SUBSTRATES (I9 — one interface, three deployments; the 0.5 spike froze the ranking):
  - SlotShareAdapter   — PRIMARY: llama.cpp `--parallel` slots on the house gemma model, through
                         an INJECTABLE client callable (tests mock it; nothing here ever opens a
                         socket). Enforces `config.pillars_max_generals` admission (the DERIVED
                         cap: empirical 8-slot ceiling @8k − 2 headroom − the mind's slot) and
                         carries the arbiter seam: `gate(mind_busy)` holds admission during a
                         latency-critical mind burst and releases into idle gaps — event-driven
                         (a Condition notify), never a poll.
  - CPUSmallAdapter    — stub: NO small GGUF is present on Sprinter today (only gemma4-12b +
                         qwen-27b), and the spike verdict says slot-sharing likely makes this
                         tier unnecessary; it raises SubstrateUnavailable until a model exists.
  - RemoteGangliaAdapter — stub: the ZMQ transport already exists (nervous/transport.ZmqTransport
                         links two NervousBuses over DEALER<->DEALER); this adapter is the seam
                         that will bind a mission to a remote ganglion's bus when Pi agents land.

SCHEDULING (the 0.5 doctrine — no global beat, no timers): llama.cpp's iteration-level
continuous batching needs temporal OVERLAP of active generations, not synchronized dispatch.
`dispatch_cohort` therefore launches missions born together (a decomposed objective's
sub-missions) concurrently and lets the shared server's decode loop coalesce them for free.
GPU-seconds are priced HONESTLY from the spike numbers (~9%/slot per-slot throughput hit,
~1.8× aggregate at 2-way): a batched cohort is measurably cheaper per mission than serial
dispatch, so the creature drifts toward clustering delegations on its own — pressure, not a
scripted beat (§0).

REPORTS ARE AFFERENTS: a valid report enters the bus as a fungible percept and competes for
workspace admission like any sense; escalations publish reliable-class. Distilled findings
persist as engrams `provenance='told'` with a confidence DISCOUNT (second-hand is second-hand).
Generals are EPHEMERAL — dissolved from the roster at mission end, no persistent identity.
Generals SPAWN NOTHING (grant validation rejects spawn-class capabilities structurally; the
report schema carries proposals, never actions — irreversible acts are propose-only, the
dashboard pattern recursed one level down).

DELEGATION COMPETENCE IS LEARNED, NOT CONFIGURED: every mission settles as a credit/loss episode
engram keyed on the task's SHAPE (a normalized objective signature in stats), committed through
the Consolidator (§I6). Ordinary recall then biases future delegate-vs-do-it-myself choices —
a garbage report writes a weak episode, and that shape's episodes rank lower on the next compile.
Any XP-facing outcome carries `delegated: True` (level_gates already excludes delegated outcomes
from every mastery counter — pitfall #8; this module just sets the field).

Ships DARK behind `config.pillars_generals_enabled` (default False): with the flag off every
entrypoint is a no-op. Pure LIBRARY — not imported by eidos.py or the tick loop; tests drive it
with a mock substrate. The LLM substrate is an INJECTABLE callable; this module never talks to
any live service.

Doctrine bindings (PILLARS_PLAN §0):
  §0.2  Mechanism, not behavior: this builds contracts, adapters, a pricing curve, and a
        settlement path; "knowing what to delegate" is what a creature recalling the episodes does.
  §0.4  Every constant is derived from the spike measurements or a DECLARED knob with a one-line
        justification (below).
  §I6   All long-term writes (findings, episodes) go through the Consolidator — the single writer.
  §I9   One mission contract, three substrate deployments behind one adapter interface.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import bets
import engram
from engram import Consolidator, EncodedAt, Engram
from grammar import _JSON_RULES  # the house GBNF json rules — one json grammar, not a second copy

logger = logging.getLogger("eidos.missions")

# --- Measured constants (0.5 slot-sharing spike, 2026-07-03, Sprinter RTX 5080 / gemma4-12b) ------
SPIKE_BASE_TOKS_PER_S = 77.8    # measured: single-slot decode rate — the serial baseline the
                                # pricing curve is anchored to.
SLOT_THROUGHPUT_HIT = 0.09      # measured: per-slot throughput loss per EXTRA concurrent slot
                                # (77.8 → 70.8 tok/s at 2-way ≈ 9%; ~45 at 6-way sits on this line).
MIN_PER_SLOT_RATE = 8.0         # declared: floor on the modeled per-slot rate so the pricing curve
                                # stays finite beyond the measured range (the admission cap keeps
                                # real cohorts inside it; this only guards the arithmetic).

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) ------------
ENERGY_PER_GPU_SECOND = 0.001   # declared: metabolic price of one GPU-second of general decode —
                                # a serial 5-general research load (~13 s) costs ~0.013 of the 0..1
                                # reserve: felt (delegation is never free) but never crippling.
TOLD_CONFIDENCE_DISCOUNT = 0.8  # declared: a general's finding is SECOND-HAND (§M-2 source
                                # monitoring) — its reported confidence is discounted 20% at
                                # ingestion, so a told fact never outranks the same fact experienced.
MISSION_CREDIT = 0.15           # declared: strength lift of a successful delegation episode above
                                # the neutral seed — mirrors bets.STRONG_CREDIT (a settled mission
                                # is causal evidence about the task shape, not co-presence).
MISSION_LOSS = 0.15             # declared: symmetric strength drop for a failed/garbage mission —
                                # a bad delegation must bias the next choice as hard as a good one.
SHAPE_TOKENS = 6                # declared: how many leading normalized tokens key a task shape —
                                # enough to separate "research X" from "summarize Y", short enough
                                # that wording variants of the same errand still collide.
ADMISSION_TIMEOUT_S = 30.0      # declared: default bounded wait for a slot (a bounded blocking
                                # acquire per ARCH #1, never a poll) — past it the mission is
                                # admission-denied rather than queueing forever.
KILL_JOIN_GRACE_S = 1.0         # declared: bounded wait for a killed general's worker thread to
                                # unwind after kill() signals it — event-driven (kill IS the
                                # signal; join blocks on thread exit), bounded so a wedged
                                # substrate can never wedge the monarch (ARCH #2).
MAX_FINDINGS = 8                # declared: bound on findings per report — a general DISTILLS
                                # (plan §5b: "distilled findings"); more than 8 is a dump, and the
                                # grammar bounds the array so the 12B cannot ramble one out.
MAX_PROPOSALS = 8               # declared: bound on proposals per report — same distillation
                                # doctrine; the monarch triages 8, it drowns in 80.
FINDING_MAX_CHARS = 500         # declared: per-finding body cap (the administrator's mstring
                                # doctrine: live smokes showed the 12B rambling in unbounded
                                # string rules) — 500 chars is a distilled paragraph.
PROPOSAL_MAX_CHARS = 500        # declared: per-proposal cap — a proposal is a suggestion line,
                                # not a plan document; same bounded-string doctrine.

# Spawn-class capabilities a grant may NEVER carry (generals spawn nothing — plan §5b safety).
# Structural, not honor-system: validate_grant rejects them regardless of what the monarch holds.
FORBIDDEN_GRANTS = frozenset({
    "spawn_general", "spawn_shadow", "dispatch_mission", "dispatch_cohort",
    "delegate", "create_skill", "propose_self_edit",
})


class MissionError(ValueError):
    """A malformed mission contract."""


class GrantError(MissionError):
    """A grant that exceeds the monarch's own capabilities or carries a spawn-class one."""


class MalformedReport(ValueError):
    """A report that fails the typed findings schema — dropped with log, never ingested."""


class SubstrateUnavailable(RuntimeError):
    """The requested substrate tier is not available on this body (I8: bind to what the body has)."""


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


# =================================================================================================
# The task shape — what delegation episodes key on
# =================================================================================================
def mission_shape(objective: str) -> str:
    """Normalized task-shape signature: the bets.py signature convention (lowercase, digits→#,
    punctuation→space — port/count variants collapse) truncated to the leading SHAPE_TOKENS
    tokens. Two wordings of the same errand share a shape; recall over the episodes carrying it
    is the learned delegate-vs-do-it-myself model."""
    sig = bets._norm_sig(objective or "")
    return " ".join(sig.split()[:SHAPE_TOKENS])


# =================================================================================================
# The mission contract (plan §5b schema)
# =================================================================================================
@dataclass
class Budget:
    """The mission's hard ceilings. tokens caps the decode (passed to the substrate as
    max_tokens AND re-checked at settlement), energy caps the honest GPU-second price,
    wall_clock_s caps real time before the general is killed."""
    tokens: int
    energy: float
    wall_clock_s: float

    def validate(self) -> "Budget":
        if int(self.tokens) <= 0:
            raise MissionError("budget.tokens must be a positive int")
        if float(self.energy) <= 0:
            raise MissionError("budget.energy must be positive")
        if float(self.wall_clock_s) <= 0:
            raise MissionError("budget.wall_clock_s must be positive")
        return self


@dataclass
class Mission:
    """One contract. Exactly one objective; everything else is what the monarch compiled,
    granted, budgeted, and demands back."""
    objective: str
    context_pack: dict
    grant: list[str]
    budget: Budget
    escalation: list[str] = field(default_factory=list)
    report_grammar: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def shape(self) -> str:
        return mission_shape(self.objective)

    def validate(self, monarch_capabilities) -> "Mission":
        """Raise MissionError/GrantError if the contract is malformed. `objective` must be
        exactly ONE non-empty string — a list of objectives is a cohort of missions, not one."""
        if not isinstance(self.objective, str):
            raise MissionError("a mission carries exactly ONE objective (a string, not a "
                               f"{type(self.objective).__name__}) — decompose into a cohort instead")
        if not self.objective.strip():
            raise MissionError("mission objective must be non-empty")
        if not isinstance(self.context_pack, dict):
            raise MissionError("context_pack must be a dict (the monarch-compiled pack)")
        self.grant = validate_grant(self.grant, monarch_capabilities)
        self.budget.validate()
        if not isinstance(self.escalation, list) or not all(isinstance(c, str) for c in self.escalation):
            raise MissionError("escalation must be a list of condition strings")
        if not self.report_grammar:
            self.report_grammar = build_report_grammar()
        return self


def validate_grant(grant, monarch_capabilities) -> list[str]:
    """The attenuation check: the grant must be ⊆ the monarch's own capability set (a delegate
    can never hold what its principal does not), and may NEVER include a spawn-class capability
    (generals spawn nothing — checked structurally here, before any dispatch, regardless of what
    the monarch itself holds)."""
    granted = [str(g) for g in (grant or [])]
    forbidden = sorted(set(granted) & FORBIDDEN_GRANTS)
    if forbidden:
        raise GrantError(f"spawn-class capabilities can never be granted to a general: {forbidden}")
    own = set(monarch_capabilities or [])
    excess = sorted(set(granted) - own)
    if excess:
        raise GrantError(f"grant exceeds the monarch's own capabilities: {excess}")
    return granted


# =================================================================================================
# The context pack — monarch-compiled, mechanical (plan §5b)
# =================================================================================================
def compile_context_pack(manager, objective: str, *, situation: Optional[str] = None,
                         granted_skills=(), constraints=()) -> dict:
    """Compile the mission's context pack MECHANICALLY: relevant engrams via the memory manager's
    ordinary recall cascade (relevance × strength, exploration slot and all — the general sees
    what the monarch would have recalled, nothing more), plus the granted skills and standing
    constraints. The general never browses the monarch's memory; it gets a pack."""
    recalled = manager.recall(objective, situation=situation) if manager is not None else []
    return {
        "engrams": [{"id": e.id, "kind": e.kind, "body": e.body,
                     "confidence": e.confidence, "provenance": e.provenance}
                    for e in recalled],
        "skills": list(granted_skills),
        "constraints": list(constraints),
    }


def compile_mission(manager, objective: str, *, monarch_capabilities, budget: Budget,
                    grant=(), escalation=(), situation: Optional[str] = None,
                    granted_skills=(), constraints=()) -> Mission:
    """The monarch's one-call mission compiler: pack + grant + budget + report grammar, validated."""
    pack = compile_context_pack(manager, objective, situation=situation,
                                granted_skills=granted_skills, constraints=constraints)
    m = Mission(objective=objective, context_pack=pack, grant=list(grant),
                budget=budget, escalation=list(escalation))
    return m.validate(monarch_capabilities)


def mission_prompt(mission: Mission) -> str:
    """Render the outbound half of the contract. Mechanical — the objective, the compiled pack,
    the constraints, and the report demand. The report SHAPE is enforced by the grammar, not
    begged for here; this line just tells the general what the sampler will hold it to."""
    lines = [f"MISSION: {mission.objective.strip()}", ""]
    engrams = mission.context_pack.get("engrams") or []
    if engrams:
        lines.append("CONTEXT (recalled by your principal — second-hand to you):")
        lines += [f"- [{e['kind']}] {e['body']}" for e in engrams]
        lines.append("")
    skills = mission.context_pack.get("skills") or []
    if skills:
        lines.append("GRANTED SKILLS: " + ", ".join(str(s) for s in skills))
    if mission.grant:
        lines.append("GRANTED CAPABILITIES: " + ", ".join(mission.grant))
    constraints = mission.context_pack.get("constraints") or []
    if constraints:
        lines.append("CONSTRAINTS:")
        lines += [f"- {c}" for c in constraints]
    lines += ["",
              f"BUDGET: {mission.budget.tokens} tokens, {mission.budget.wall_clock_s:.0f}s wall clock.",
              "Respond with ONLY the report JSON: findings (body + confidence), proposals "
              "(suggestions for your principal — you execute nothing), escalate (bool)."]
    return "\n".join(lines)


# =================================================================================================
# The typed report — grammar-constrained BOTH directions
# =================================================================================================
def build_report_grammar() -> str:
    """The GBNF for the general's report. Constrained decoding makes the report contract
    STRUCTURAL on the way out (the sampler cannot emit a malformed report, an action field, or
    anything but findings/proposals/escalate); `validate_report` re-checks the identical schema
    and bounds on the way in.

    Bounded-string doctrine (administrator.py, model-in-the-loop smokes 2026-07): the 12B rambles
    in open `jstring` rules and fills open `jobject` rules with gibberish keys, so every free-text
    rule here is a bounded `schar` repetition (bodies/proposals ≤ FINDING/PROPOSAL_MAX_CHARS) and
    every structured value is constrained to its REAL shape — findings/proposals arrays are
    bounded, and confidence is a [0,1] number AT THE SAMPLER (`conf`), never an open jnumber."""
    return "\n".join([
        'root ::= jws "{" jws "\\"findings\\"" jws ":" jws findings "," jws '
        '"\\"proposals\\"" jws ":" jws proposals "," jws '
        '"\\"escalate\\"" jws ":" jws jbool "}" jws',
        f'findings ::= "[" jws ( finding ( "," jws finding ){{0,{MAX_FINDINGS - 1}}} )? "]" jws',
        'finding ::= "{" jws "\\"body\\"" jws ":" jws fstring "," jws '
        '"\\"confidence\\"" jws ":" jws conf "}" jws',
        f'proposals ::= "[" jws ( pstring ( "," jws pstring ){{0,{MAX_PROPOSALS - 1}}} )? "]" jws',
        'jbool ::= ( "true" | "false" ) jws',
        # Bounded free-text rules (never an open jstring):
        f'fstring ::= "\\"" schar{{1,{FINDING_MAX_CHARS}}} "\\"" jws',
        f'pstring ::= "\\"" schar{{1,{PROPOSAL_MAX_CHARS}}} "\\"" jws',
        'schar ::= [^"\\\\\\x7F\\x00-\\x1F] | "\\\\" ( ["\\\\bfnrt/] | "u" jhex jhex jhex jhex )',
        # Confidence constrained to [0,1] at the sampler (a real sub-shape, not open jnumber):
        'conf ::= ( "0" ( "." [0-9]{1,3} )? | "1" ( "." "0"{1,3} )? ) jws',
        _JSON_RULES.strip(),   # jws / jhex (the house json whitespace + hex rules)
    ])


_REPORT_KEYS = {"findings", "proposals", "escalate"}
_FINDING_KEYS = {"body", "confidence"}


def validate_report(text: str) -> dict:
    """Parse + validate a report against the SAME schema and bounds the grammar enforces (the
    inbound half of "constrained both directions"). EXACT keys only — an extra key (an action
    channel a substrate might smuggle) is malformed, so propose-only is structural. Raises
    MalformedReport; the caller drops-with-log, never ingests."""
    try:
        report = json.loads(text or "")
    except (ValueError, json.JSONDecodeError) as e:
        raise MalformedReport(f"report is not valid JSON: {e}") from e
    if not isinstance(report, dict) or set(report.keys()) != _REPORT_KEYS:
        raise MalformedReport(f"report must carry exactly the keys {sorted(_REPORT_KEYS)}")
    findings = report["findings"]
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise MalformedReport(f"findings must be a list of at most {MAX_FINDINGS}")
    for f in findings:
        if not isinstance(f, dict) or set(f.keys()) != _FINDING_KEYS:
            raise MalformedReport(f"each finding must carry exactly the keys {sorted(_FINDING_KEYS)}")
        body = f["body"]
        if not isinstance(body, str) or not body.strip() or len(body) > FINDING_MAX_CHARS:
            raise MalformedReport(f"finding body must be a non-empty string ≤{FINDING_MAX_CHARS} chars")
        c = f["confidence"]
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not (0.0 <= float(c) <= 1.0):
            raise MalformedReport(f"finding confidence must be a number in [0,1], got {c!r}")
    proposals = report["proposals"]
    if (not isinstance(proposals, list) or len(proposals) > MAX_PROPOSALS
            or not all(isinstance(p, str) and len(p) <= PROPOSAL_MAX_CHARS for p in proposals)):
        raise MalformedReport(f"proposals must be ≤{MAX_PROPOSALS} strings of ≤{PROPOSAL_MAX_CHARS} "
                              "chars (propose-only — never actions)")
    if not isinstance(report["escalate"], bool):
        raise MalformedReport("escalate must be a bool")
    return report


# =================================================================================================
# Honest GPU pricing (the 0.5 spike's declared curve)
# =================================================================================================
def per_slot_rate(cohort_n: int) -> float:
    """Modeled per-slot decode rate at n-way concurrency: the measured single-slot rate less the
    measured ~9% hit per extra slot (77.8 → 70.8 at 2-way; ~45 at 6-way sits on this line)."""
    n = max(1, int(cohort_n))
    return max(MIN_PER_SLOT_RATE, SPIKE_BASE_TOKS_PER_S * (1.0 - SLOT_THROUGHPUT_HIT * (n - 1)))


def price_gpu_seconds(tokens: int, cohort_n: int) -> float:
    """Per-mission GPU-second share for `tokens` decoded in a cohort of `cohort_n`. The cohort's
    generations OVERLAP (continuous batching), so the whole cohort occupies the GPU for
    tokens/per_slot_rate(n) wall-seconds and each mission owes 1/n of it. Serial (n=1) pays
    tokens/77.8 each — batched is honestly cheaper per mission (~5 s batched vs ~13 s serial for
    5 generals in the spike), which is the pressure that clusters delegations without a beat."""
    n = max(1, int(cohort_n))
    wall_s = max(0, int(tokens)) / per_slot_rate(n)
    return wall_s / n


def price_energy(tokens: int, cohort_n: int) -> float:
    """The metabolic price of a mission's decode: honest GPU-seconds × the declared energy rate."""
    return price_gpu_seconds(tokens, cohort_n) * ENERGY_PER_GPU_SECOND


# =================================================================================================
# Substrate adapters — one interface, three deployments (I9)
# =================================================================================================
class SubstrateAdapter:
    """The one interface a mission runs on. `admit` is the admission gate (bounded blocking
    acquire — ARCH #1), `generate` is the blocking decode (the runner threads it for cohort
    overlap), `kill` cancels a running general, `release` frees its slot (idempotent).
    Grade follows substrate (plan §5b); missions declaring a required grade bind against it (I8)."""

    grade = "unknown"

    def admit(self, mission_id: str, *, timeout: Optional[float] = None) -> bool:
        raise NotImplementedError

    def generate(self, mission_id: str, prompt: str, *, grammar: str, max_tokens: int) -> dict:
        """Blocking decode. Returns {"text": str, "tokens": int} (tokens = completion tokens)."""
        raise NotImplementedError

    def kill(self, mission_id: str) -> None:
        raise NotImplementedError

    def release(self, mission_id: str) -> None:
        raise NotImplementedError


class SlotShareAdapter(SubstrateAdapter):
    """PRIMARY (0.5 spike verdict): parallel slots on the shared house-model server, reached
    through an INJECTABLE client callable `client(prompt, grammar=..., max_tokens=...) ->
    {"text","tokens"}` — production wires an HTTP client at the shared `--parallel` endpoint;
    tests inject a mock; this class never opens a socket itself.

    Admission enforces the DERIVED `pillars_max_generals` cap and carries the ARBITER SEAM:
    `gate(True)` during a latency-critical mind burst HOLDS all new admissions; `gate(False)`
    releases every waiter into the idle gap. Event-driven throughout — waiters block on a
    Condition the gate notifies; there is no polling loop and no timer."""

    grade = "house"   # full house-model grade — same weights as the mind

    def __init__(self, config, client: Callable[..., dict], *, max_generals: Optional[int] = None):
        self.config = config
        self._client = client
        self._cap = int(config.pillars_max_generals if max_generals is None else max_generals)
        self._cond = threading.Condition()
        self._active: set[str] = set()
        self._mind_busy = False

    @property
    def active(self) -> set[str]:
        with self._cond:
            return set(self._active)

    # --- the arbiter seam --------------------------------------------------------------------
    def gate(self, mind_busy: bool) -> None:
        """The arbiter's admission gate: hold during a mind burst, release into the idle gap.
        Cheap by design (plan §5b): the mind usually delegates *then goes quiet waiting*, so
        mind↔general contention is naturally low — this seam covers the burst that isn't."""
        with self._cond:
            self._mind_busy = bool(mind_busy)
            if not self._mind_busy:
                self._cond.notify_all()

    # --- admission (the derived cap) -----------------------------------------------------------
    def admit(self, mission_id: str, *, timeout: Optional[float] = None) -> bool:
        """Bounded blocking acquire of a general slot: proceeds when the mind is idle AND a slot
        is free under the cap. Returns False on timeout (admission denied), never raises."""
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        with self._cond:
            while self._mind_busy or len(self._active) >= self._cap:
                if deadline is None:
                    self._cond.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(timeout=remaining)
            self._active.add(str(mission_id))
            return True

    def generate(self, mission_id: str, prompt: str, *, grammar: str, max_tokens: int) -> dict:
        out = self._client(prompt, grammar=grammar, max_tokens=int(max_tokens))
        if not isinstance(out, dict) or "text" not in out:
            raise SubstrateUnavailable("slot-share client returned no text")
        out.setdefault("tokens", 0)
        return out

    def kill(self, mission_id: str) -> None:
        """Cancel a running general: forward to the client's cancel seam if it has one (llama.cpp
        exposes per-slot cancel), then free the slot either way."""
        cancel = getattr(self._client, "cancel", None)
        if callable(cancel):
            try:
                cancel(mission_id)
            except Exception as e:  # noqa: BLE001 - a kill must always release the slot
                logger.warning("slot-share cancel(%s) raised: %s", mission_id, e)
        self.release(mission_id)

    def release(self, mission_id: str) -> None:
        with self._cond:
            self._active.discard(str(mission_id))
            self._cond.notify_all()


class CPUSmallAdapter(SubstrateAdapter):
    """OPTIONAL lesser-grade tier — NOT AVAILABLE on this body: no small GGUF exists on Sprinter
    today (only gemma4-12b + qwen-27b are present), and the 0.5 spike's slot-sharing numbers mean
    we likely never need it. A CPU tier needs a small model acquired first; until then every call
    raises SubstrateUnavailable so a mission declaring grade 'small' fails loud at bind (I8)."""

    grade = "small"
    _NOTE = ("CPU-small substrate unavailable: no small GGUF on Sprinter (only gemma4-12b + "
             "qwen-27b); acquire a qwen-class small model before enabling this tier — the 0.5 "
             "spike verdict is that slot-sharing likely makes it unnecessary.")

    def admit(self, mission_id: str, *, timeout: Optional[float] = None) -> bool:
        raise SubstrateUnavailable(self._NOTE)

    def generate(self, mission_id: str, prompt: str, *, grammar: str, max_tokens: int) -> dict:
        raise SubstrateUnavailable(self._NOTE)

    def kill(self, mission_id: str) -> None:
        raise SubstrateUnavailable(self._NOTE)

    def release(self, mission_id: str) -> None:
        raise SubstrateUnavailable(self._NOTE)


class RemoteGangliaAdapter(SubstrateAdapter):
    """The distributed-robot path (I9) — STUB. The transport seam already exists:
    nervous/transport.ZmqTransport links two NervousBuses over DEALER<->DEALER with
    auto-reconnect, so a remote ganglion (Pi/Jetson) is a bus peer, not a new protocol. This
    adapter will bind a mission contract to a remote peer's bus (dispatch = a reliable event to
    the ganglion; the report returns as a reliable event into the SAME ingest path as every
    other substrate). Until a ganglion is enrolled it fails loud at bind (I8)."""

    grade = "remote"
    _NOTE = ("remote-ganglia substrate is a seam, not yet a deployment: wire a "
             "nervous.transport.ZmqTransport peer (the transport exists) and carry the mission/"
             "report as reliable bus events; no ganglion is enrolled on this body today.")

    def admit(self, mission_id: str, *, timeout: Optional[float] = None) -> bool:
        raise SubstrateUnavailable(self._NOTE)

    def generate(self, mission_id: str, prompt: str, *, grammar: str, max_tokens: int) -> dict:
        raise SubstrateUnavailable(self._NOTE)

    def kill(self, mission_id: str) -> None:
        raise SubstrateUnavailable(self._NOTE)

    def release(self, mission_id: str) -> None:
        raise SubstrateUnavailable(self._NOTE)


# =================================================================================================
# The runner — cohort dispatch, budget enforcement, ingestion, settlement, dissolution
# =================================================================================================
@dataclass
class MissionResult:
    """One mission's settled outcome. `outcome` is the XP-facing dict and ALWAYS carries
    `delegated: True` (pitfall #8: level_gates drops delegated outcomes from every counter)."""
    mission_id: str
    shape: str
    success: bool
    reason: str = ""
    report: Optional[dict] = None
    finding_ids: list[str] = field(default_factory=list)
    tokens_used: int = 0
    wall_s: float = 0.0
    gpu_seconds: float = 0.0
    energy_charged: float = 0.0
    outcome: Optional[dict] = None


class MissionRunner:
    """Owns the roster of live generals over ONE substrate adapter. Dispatches cohorts
    (concurrent, overlap-emergent), enforces budgets (token / wall / energy — a blower is killed
    through the adapter), ingests reports (afferents through the bus; findings as discounted
    `told` engrams via the single writer), settles every mission as a shape-keyed delegation
    episode, and DISSOLVES the general at mission end (ephemeral — the roster never leaks)."""

    def __init__(self, config, *, adapter: SubstrateAdapter,
                 consolidator: Optional[Consolidator] = None,
                 manager: Any = None, bus: Any = None):
        self.config = config
        self.adapter = adapter
        self.manager = manager
        self.consolidator = consolidator or (manager.consolidator if manager is not None
                                             else Consolidator(config))
        self.bus = bus
        self.roster: dict[str, dict] = {}   # mission_id -> live-general record (ephemeral)
        self.dropped_reports = 0

    @property
    def enabled(self) -> bool:
        """The dark flag: off → every entrypoint no-ops and production is byte-identical."""
        return bool(getattr(self.config, "pillars_generals_enabled", False))

    # --- cohort dispatch (the 0.5 scheduling doctrine) -------------------------------------------
    def dispatch_cohort(self, missions: list[Mission], *, tick: int = 0,
                        admission_timeout_s: float = ADMISSION_TIMEOUT_S) -> list[MissionResult]:
        """Dispatch missions BORN TOGETHER as one cohort: each admitted mission's generation
        starts concurrently, so overlap (and the batched price) is emergent from the shared
        server's continuous batching — no beat, no timer. Blocks (bounded by each mission's own
        wall budget) until the cohort settles; returns one MissionResult per mission."""
        if not self.enabled:
            return []
        results: list[MissionResult] = []
        admitted: list[Mission] = []
        for m in missions or []:
            if not self.adapter.admit(m.id, timeout=admission_timeout_s):
                results.append(MissionResult(mission_id=m.id, shape=m.shape, success=False,
                                             reason="admission_denied"))
                continue
            admitted.append(m)
        if not admitted:
            return results

        cohort_n = len(admitted)
        holders: dict[str, dict] = {m.id: {} for m in admitted}
        threads: dict[str, threading.Thread] = {}
        started = time.monotonic()
        for m in admitted:
            self.roster[m.id] = {"mission": m, "started": started}

            def _run(mm: Mission = m) -> None:
                try:
                    holders[mm.id]["out"] = self.adapter.generate(
                        mm.id, mission_prompt(mm),
                        grammar=mm.report_grammar, max_tokens=mm.budget.tokens)
                except Exception as e:  # noqa: BLE001 - a substrate crash is a mission loss, never a monarch crash
                    holders[mm.id]["err"] = e

            threads[m.id] = threading.Thread(target=_run, name=f"general-{m.id[:8]}", daemon=True)
        for t in threads.values():
            t.start()   # started together: the cohort's generations overlap from the first decode step

        for m in admitted:
            t = threads[m.id]
            remaining = m.budget.wall_clock_s - (time.monotonic() - started)
            t.join(timeout=max(0.0, remaining))
            wall_s = time.monotonic() - started
            if t.is_alive():
                # Budget blower: kill through the adapter, wait boundedly for the thread to unwind.
                self.adapter.kill(m.id)
                t.join(timeout=KILL_JOIN_GRACE_S)
                self._escalate(m, "budget_exceeded:wall_clock")
                results.append(self._settle(m, success=False, reason="wall_clock_exceeded",
                                            tick=tick, wall_s=wall_s, cohort_n=cohort_n))
                continue
            err = holders[m.id].get("err")
            if err is not None:
                self.adapter.release(m.id)
                results.append(self._settle(m, success=False, reason=f"substrate_error:{err}",
                                            tick=tick, wall_s=wall_s, cohort_n=cohort_n))
                continue
            out = holders[m.id].get("out") or {}
            tokens = int(out.get("tokens", 0))
            if tokens > m.budget.tokens:
                # The substrate overran the token ceiling: terminate + settle as a loss.
                self.adapter.kill(m.id)
                self._escalate(m, "budget_exceeded:tokens")
                results.append(self._settle(m, success=False, reason="token_budget_exceeded",
                                            tick=tick, wall_s=wall_s, tokens=tokens,
                                            cohort_n=cohort_n))
                continue
            self.adapter.release(m.id)
            results.append(self._settle_report(m, str(out.get("text", "")), tokens=tokens,
                                               wall_s=wall_s, tick=tick, cohort_n=cohort_n))
        return results

    # --- report ingestion (afferents; discounted told engrams; propose-only) ----------------------
    def _settle_report(self, mission: Mission, text: str, *, tokens: int, wall_s: float,
                       tick: int, cohort_n: int) -> MissionResult:
        try:
            report = validate_report(text)
        except MalformedReport as e:
            # Drop-with-log: a malformed report NEVER reaches memory or the bus (§5b safety) —
            # and it IS the garbage-report loss the delegation episode must remember.
            self.dropped_reports += 1
            logger.warning("mission %s report dropped (malformed): %s", mission.id[:8], e)
            return self._settle(mission, success=False, reason="malformed_report",
                                tick=tick, wall_s=wall_s, tokens=tokens, cohort_n=cohort_n)
        energy = price_energy(tokens, cohort_n)
        if energy > mission.budget.energy:
            self._escalate(mission, "budget_exceeded:energy")
            return self._settle(mission, success=False, reason="energy_budget_exceeded",
                                tick=tick, wall_s=wall_s, tokens=tokens, cohort_n=cohort_n)
        finding_ids = self.ingest_report(mission, report, tick=tick)
        if report.get("escalate"):
            for condition in (mission.escalation or ["general_escalated"]):
                self._escalate(mission, condition)
        return self._settle(mission, success=True, reason="report_ingested", tick=tick,
                            wall_s=wall_s, tokens=tokens, cohort_n=cohort_n,
                            report=report, finding_ids=finding_ids)

    def ingest_report(self, mission: Mission, report: dict, *, tick: int = 0) -> list[str]:
        """Persist a VALIDATED report's findings as `provenance='told'` engrams with the
        confidence DISCOUNT (second-hand, §M-2), through the single writer (§I6), and publish the
        report as an AFFERENT on the bus — a fungible percept that competes for admission like
        any sense (the monarch reads its army the way it reads its senses). Proposals ride the
        afferent payload only: they are proposed, never executed."""
        if not self.enabled:
            return []
        finding_ids: list[str] = []
        best_conf = 0.0
        for f in report.get("findings", []):
            conf = _clamp01(float(f["confidence"]) * TOLD_CONFIDENCE_DISCOUNT)
            best_conf = max(best_conf, conf)
            eg = Engram(kind="fact", body=f["body"], provenance="told", confidence=conf,
                        encoded_at=EncodedAt(tick=int(tick)),
                        stats={"src": "mission_report", "mission_id": mission.id,
                               "mission_shape": mission.shape})
            finding_ids.append(self.consolidator.commit(eg).id)
        self._publish_afferent(mission, report, salience=best_conf)
        return finding_ids

    def _publish_afferent(self, mission: Mission, report: dict, *, salience: float) -> None:
        """The report enters the nervous system as a fungible percept — through the same gate,
        competing on the same salience field, as every other afferent. Fail-open: no bus, no
        publish (a library test without a nervous system still ingests findings)."""
        if self.bus is None:
            return
        try:
            from nervous.event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION
            ev = NervousEvent(schema_version=SCHEMA_VERSION,
                              source_organ=f"general:{mission.id[:8]}",
                              kind=Kind.percept, modality=Modality.system,
                              delivery=Delivery.fungible, salience=float(salience),
                              t=time.monotonic())
            self.bus.publish(ev, json.dumps({"mission_id": mission.id, "shape": mission.shape,
                                             "report": report}, ensure_ascii=False).encode("utf-8"))
        except Exception as e:  # noqa: BLE001 - the bus is an outlet, never a point of failure
            logger.warning("mission %s afferent publish failed: %s", mission.id[:8], e)

    def _escalate(self, mission: Mission, condition: str) -> None:
        """An escalation WAKES the monarch: a RELIABLE-class bus event (never dropped under
        normal backpressure, outranks every fungible percept) naming the fired condition."""
        if self.bus is None:
            return
        try:
            from nervous.event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION
            ev = NervousEvent(schema_version=SCHEMA_VERSION,
                              source_organ=f"general:{mission.id[:8]}",
                              kind=Kind.percept, modality=Modality.system,
                              delivery=Delivery.reliable, salience=1.0, t=time.monotonic())
            self.bus.publish(ev, json.dumps({"mission_id": mission.id, "shape": mission.shape,
                                             "escalation": condition},
                                            ensure_ascii=False).encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("mission %s escalation publish failed: %s", mission.id[:8], e)

    # --- settlement (delegation episodes + delegated XP mark + dissolution) -----------------------
    def _settle(self, mission: Mission, *, success: bool, reason: str, tick: int,
                wall_s: float = 0.0, tokens: int = 0, cohort_n: int = 1,
                report: Optional[dict] = None,
                finding_ids: Optional[list[str]] = None) -> MissionResult:
        """Settle one mission: write the shape-keyed delegation episode (credit or loss), stamp
        the delegated XP mark, and DISSOLVE the general — pop it from the roster, no persistent
        identity, nothing survives but the episode and the ingested findings."""
        outcome = self.settle_mission(mission, success=success, reason=reason, tick=tick)
        self.roster.pop(mission.id, None)   # ephemeral: dissolve at mission end
        return MissionResult(mission_id=mission.id, shape=mission.shape, success=success,
                             reason=reason, report=report, finding_ids=list(finding_ids or []),
                             tokens_used=int(tokens), wall_s=float(wall_s),
                             gpu_seconds=price_gpu_seconds(tokens, cohort_n),
                             energy_charged=price_energy(tokens, cohort_n), outcome=outcome)

    def settle_mission(self, mission: Mission, *, success: bool, reason: str = "",
                       tick: int = 0) -> Optional[dict]:
        """Write the delegation EPISODE through the Consolidator: credit on success, loss on
        failure, keyed on the task shape in stats — so ordinary recall (relevance × strength)
        biases the next delegate-vs-do-it-myself choice mechanically. The returned outcome dict
        carries `delegated: True` (pitfall #8) and is fed to the mastery gates, which drop it."""
        if not self.enabled:
            return None
        shape = mission.shape
        strength = _clamp01(engram.STRENGTH_DEFAULT + (MISSION_CREDIT if success else -MISSION_LOSS))
        verdict = "succeeded within budget" if success else f"failed — {reason or 'unadjudicated'}"
        body = (f"Delegated mission {mission.id[:8]} (shape `{shape}`) to a general: {verdict}. "
                f"Objective: {mission.objective.strip()[:160]}")
        eg = Engram(kind="episode", body=body, provenance="experienced", strength=strength,
                    encoded_at=EncodedAt(tick=int(tick)),
                    stats={"src": "mission", "mission_id": mission.id, "mission_shape": shape,
                           "situation": f"mission|{shape}", "delegated": True,
                           "reason": reason, "success": bool(success)})
        committed = self.consolidator.commit(eg)
        if committed.id != eg.id:
            # A near-identical prior delegation episode absorbed this one. Merge keeps the
            # STRONGER strength, but settlement is fresh ADJUDICATED evidence about the shape —
            # re-pin the survivor to this settlement's strength through the single writer (what
            # this shape did LATELY is the bias that matters).
            self.consolidator.update_strength(committed.id, strength, recalled_tick=int(tick))
        outcome = {"success": bool(success), "delegated": True, "mission_id": mission.id,
                   "shape": shape, "reason": reason, "tick": int(tick)}
        try:
            import level_gates
            # Delegated outcomes NEVER feed a gate counter (pitfall #8) — level_gates drops them
            # on the mark this call sets; calling it anyway keeps the exclusion in ONE place.
            level_gates.record_tier_outcome(self.config, 1, bool(success), delegated=True)
        except Exception as e:  # noqa: BLE001 - XP plumbing is best-effort; settlement never fails on it
            logger.debug("delegated tier-outcome record skipped: %s", e)
        return outcome


def delegation_bias(store, objective: str) -> Optional[float]:
    """The monarch's cheap scalar for "how has delegating THIS SHAPE gone?": mean strength of the
    long-term delegation episodes keyed on the objective's shape, None when the shape has no
    history. Ordinary recall does the real biasing (those episodes rank by relevance × strength
    in every compile); this is the same signal read directly, for a delegate-vs-do-it-myself
    comparison at decision time."""
    shape = mission_shape(objective)
    if not shape:
        return None
    eps = [e for e in store.load()
           if e.kind == "episode" and e.stats.get("mission_shape") == shape]
    if not eps:
        return None
    return sum(float(e.strength) for e in eps) / len(eps)
