"""The Commission — a standing order the creature works between check-ins (COMMISSION_PLAN.md).

A commission is a long-horizon work order living ABOVE the objective gate: objectives rotate and
park on the scale of hours; the commission persists across sleeps, level-ups, and restarts. Three
surfaces, three ownership rules:

  - BRIEF   (`workspace/commission/brief.md`) — the operator's spec. Outside the creature's home,
             so it is read-only *by placement*; injected into context, char-capped.
  - NOTES   (`workspace/home/commission_notes.md`) — the creature's own free-form scratch, kept
             with its existing file tools. This module never touches it.
  - TODO    (`workspace/commission/commission.json`) — typed tasks, SINGLE WRITER = this engine in
             the eidos process, driven by the `commission_add` / `commission_done` tools.

The honesty rule (the doctrine's central one): the creature marking a task done is a CLAIM, not a
settlement. A task pays only on ground truth — either a checkable claim in the expectation
ledger's vocabulary (glue settles it), or an operator verdict from the chat box (`/commission
done 3 nice work`). Verdicts cross the process boundary as one-file-per-verdict in
`workspace/commission/verdicts/` (dashboard writes, this engine consumes-and-deletes) — the
interventions pattern, no shared-file races.

Discipline (PILLARS_PLAN §0): mechanism, not behavior — this builds a bounded typed store, a
claim/verdict settlement path, and a payout seam; "keeps crunching between check-ins" is what a
creature running it does. Ships DARK behind `config.pillars_commission_enabled` (default False):
a pure library until the cutover wires it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from atomicio import replace_with_retry

logger = logging.getLogger("eidos.commission")

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
COMMISSION_MAX_LIVE = 24        # declared: bound on open + done_claimed tasks — a todo the creature
                                # can actually hold in mind; past it, add refuses (finish or drop
                                # something first — the objective-economy lesson, no unbounded lists).
COMMISSION_XP_CONFIRMED = 15    # declared: pay per CONFIRMED task — above skill reuse (8) so
                                # commissioned work outearns tinkering, below a genesis quest (25+)
                                # so steady labor never outearns growth milestones.
COMMISSION_FEED = 0.04          # declared: energy fed to the metabolism reserve per confirmed task —
                                # two fully-novel skill-authorings' worth (0.02 each): work earns the
                                # food the tools it took cost. Same reserve authoring spends from.
BRIEF_MAX_CHARS = 3000          # declared: the injected brief cap (~750 tokens) — a spec, not a
                                # novel; the operator's file may be longer, the creature sees the head.
RENDER_MAX_TASKS = 8            # declared: TODO block shows at most this many live tasks — the
                                # context strip stays a strip; the store holds the rest.
TITLE_MAX_CHARS = 160           # declared: a task title is one line — detail carries the rest.
DETAIL_MAX_CHARS = 600          # declared: bound on the free-text detail persisted per task.
RUNS_CLAIM_TIMEOUT_S = 30       # declared: wall bound on executing a `runs:<command>` claim at the
                                # settle beat — long enough for a real test run or program launch,
                                # short enough that one hung claim can't stall the tick loop.
RUNS_CMD_MAX_CHARS = 300        # declared: a runs-claim is one command line, not a script — anything
                                # longer belongs in a file the command invokes.
EXEMPLAR_NOTE_CHARS = 140       # declared: the exemplar line quotes evidence/verdict head-capped —
                                # a standard to imitate, not a transcript.

# Task lifecycle states.
OPEN = "open"                   # live — the creature's work queue
DONE_CLAIMED = "done_claimed"   # the creature says it's done — a claim awaiting ground truth
CONFIRMED = "confirmed"         # settled TRUE (operator verdict or glue-settled claim) — paid
REJECTED = "rejected"           # operator said not-done — reopened copy carries the note
DROPPED = "dropped"             # operator withdrew the task — closed unpaid, no fault
_LIVE = (OPEN, DONE_CLAIMED)
_TERMINAL = (CONFIRMED, DROPPED)   # rejected tasks are REOPENED in place, so never terminal

# Operator verdict kinds (the only words the dashboard channel may write).
VERDICTS = frozenset({"confirm", "reject", "drop"})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dir(config) -> Path:
    return config.workspace / "commission"


def _store_path(config) -> Path:
    return _dir(config) / "commission.json"


def brief_path(config) -> Path:
    return _dir(config) / "brief.md"


def verdicts_dir(config) -> Path:
    return _dir(config) / "verdicts"


def enabled(config) -> bool:
    return bool(getattr(config, "pillars_commission_enabled", False))


def parse_runs_claim(claim: str) -> Optional[str]:
    """A `runs:<command>` claim — the strongest claim shape: at settle time the ENGINE executes the
    command (creature-home cwd, RUNS_CLAIM_TIMEOUT_S bound) and exit 0 IS the confirmation; a
    non-zero exit REOPENS the task carrying the output tail as feedback. "I ran it" becomes
    ground truth instead of a promise. Returns the command, or None if this isn't a runs claim."""
    s = (claim or "").strip()
    if not s.lower().startswith("runs:"):
        return None
    cmd = s[len("runs:"):].strip()
    if not cmd or len(cmd) > RUNS_CMD_MAX_CHARS:
        return None
    return cmd


def load_brief(config) -> str:
    """The operator's standing order, head-capped for context. Missing file → "" (no commission)."""
    try:
        text = brief_path(config).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[:BRIEF_MAX_CHARS]


# ============================================================================================
# The task — one typed unit of commissioned work
# ============================================================================================
@dataclass
class Task:
    id: int
    title: str
    detail: str = ""
    claim: str = ""                  # "" = operator-settled; else claim vocabulary (incl. runs:)
    state: str = OPEN
    evidence: str = ""               # the creature's done-claim note (what to look at)
    verdict_note: str = ""           # the operator's words at settle time (feedback on reject)
    job: str = ""                    # the workshop (delegate) job that built it, if any — the
    #                                  revision hook: a rejected build resumes THAT session
    created_ts: str = field(default_factory=_now)
    claimed_ts: Optional[str] = None
    closed_ts: Optional[str] = None
    updated_ts: Optional[str] = None   # last state change — the lessons job's cursor key

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "detail": self.detail, "claim": self.claim,
                "state": self.state, "evidence": self.evidence,
                "verdict_note": self.verdict_note, "job": self.job,
                "created_ts": self.created_ts,
                "claimed_ts": self.claimed_ts, "closed_ts": self.closed_ts,
                "updated_ts": self.updated_ts}

    @staticmethod
    def from_dict(d: dict) -> "Task":
        return Task(id=int(d["id"]), title=str(d.get("title", "")),
                    detail=str(d.get("detail", "")), claim=str(d.get("claim", "")),
                    state=str(d.get("state", OPEN)), evidence=str(d.get("evidence", "")),
                    verdict_note=str(d.get("verdict_note", "")),
                    job=str(d.get("job", "")),
                    created_ts=d.get("created_ts") or _now(),
                    claimed_ts=d.get("claimed_ts"), closed_ts=d.get("closed_ts"),
                    updated_ts=d.get("updated_ts"))


@dataclass
class Settlement:
    """One task settling: how it settled ('claim' = glue-measured, 'operator' = a chat verdict),
    what happened, and the payout the CALLER owes (this library never touches persona/metabolism —
    the wiring pays through the existing single writers)."""
    task: Task
    how: str                          # "claim" | "operator"
    outcome: str                      # CONFIRMED | REJECTED | DROPPED
    note: str = ""
    xp: int = 0
    feed: float = 0.0


# ============================================================================================
# The engine — single writer of commission.json (eidos process)
# ============================================================================================
class Commission:
    def __init__(self, config):
        self.config = config

    # --- persistence -----------------------------------------------------------------------
    def load(self) -> list[Task]:
        try:
            d = json.loads(_store_path(self.config).read_text(encoding="utf-8"))
            return [Task.from_dict(t) for t in d.get("tasks", [])]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _save(self, tasks: list[Task]) -> None:
        p = _store_path(self.config)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"v": 1, "tasks": [t.to_dict() for t in tasks]},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        replace_with_retry(tmp, p)

    def live(self) -> list[Task]:
        return [t for t in self.load() if t.state in _LIVE]

    def confirmed_total(self) -> int:
        """Lifetime confirmed count — the honest stat quests/bets can one day adjudicate."""
        return sum(1 for t in self.load() if t.state == CONFIRMED)

    # --- the creature's two verbs -----------------------------------------------------------
    def add(self, title: str, *, detail: str = "", claim: str = "") -> Task:
        """Add a task to the commission todo. BOUNDED (COMMISSION_MAX_LIVE live tasks — finish or
        drop something first). A non-empty `claim` must parse in the expectation ledger's checkable
        vocabulary or the add is REFUSED — an ungradeable promise is unrepresentable (the
        Administrator-criteria lesson)."""
        title = (title or "").strip()[:TITLE_MAX_CHARS]
        if not title:
            raise ValueError("a commission task needs a one-line title")
        claim = (claim or "").strip()
        if claim and parse_runs_claim(claim) is None:
            from expectations import parse_claim
            if parse_claim(claim) is None:
                raise ValueError(
                    f"claim {claim!r} is not checkable — use `runs:<command>` (exit 0 confirms), "
                    "`exists:<relpath>`, `not_exists:<relpath>`, or `<stat.path> <op> <number>`, "
                    "or omit it (the operator will judge)")
        tasks = self.load()
        if sum(1 for t in tasks if t.state in _LIVE) >= COMMISSION_MAX_LIVE:
            raise ValueError(
                f"the commission todo is full ({COMMISSION_MAX_LIVE} live tasks) — "
                "finish or drop something before adding more")
        nid = max((t.id for t in tasks), default=0) + 1
        task = Task(id=nid, title=title, detail=(detail or "").strip()[:DETAIL_MAX_CHARS],
                    claim=claim, updated_ts=_now())
        tasks.append(task)
        self._save(tasks)
        return task

    def claim_done(self, task_id: int, *, evidence: str = "", job: str = "") -> Task:
        """The creature claims a task is done. A CLAIM, not a settlement: the task moves to
        done_claimed and waits for ground truth (glue on its claim, or the operator's verdict).
        Nothing pays here. `job` names the workshop (delegate) session that built it, if any —
        a rejection then points back at the session to CONTINUE with the feedback."""
        tasks = self.load()
        t = next((t for t in tasks if t.id == int(task_id)), None)
        if t is None:
            raise ValueError(f"no commission task #{task_id}")
        if t.state != OPEN:
            raise ValueError(f"task #{task_id} is {t.state}, not open")
        t.state = DONE_CLAIMED
        t.evidence = (evidence or "").strip()[:DETAIL_MAX_CHARS]
        if (job or "").strip():
            t.job = job.strip()[:64]
        t.claimed_ts = _now()
        t.updated_ts = _now()
        self._save(tasks)
        return t

    def hot_task(self) -> Optional[Task]:
        """The task most alive right now — recall and attention conditioning key on it. A reopened
        task (operator feedback waiting) outranks everything (his words are the work); else the
        most recently touched open task (the current decomposition front). None when nothing is open."""
        open_tasks = [t for t in self.live() if t.state == OPEN]
        if not open_tasks:
            return None
        fed = [t for t in open_tasks if t.verdict_note]
        pool = fed or open_tasks
        return max(pool, key=lambda t: (t.updated_ts or t.created_ts, t.id))  # id breaks same-second ties

    # --- settlement: glue half --------------------------------------------------------------
    def _execute_runs_claim(self, cmd: str) -> tuple[bool, str]:
        """Run a `runs:` claim's command in the creature's home, bounded. Returns (passed, tail) —
        the tail is the last of the combined output, which becomes the FEEDBACK on failure (the
        error message is the review). No new privilege: the creature's own bash runs anything."""
        import subprocess
        try:
            import tools as _tools
            root = _tools._creature_root(self.config)
            root.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(cmd, shell=True, cwd=str(root),
                               capture_output=True, text=True, errors="replace",
                               timeout=RUNS_CLAIM_TIMEOUT_S)
            tail = (r.stdout + r.stderr).strip()[-DETAIL_MAX_CHARS:]
            return r.returncode == 0, tail or f"exit {r.returncode}"
        except subprocess.TimeoutExpired:
            return False, f"timed out after {RUNS_CLAIM_TIMEOUT_S}s"
        except Exception as e:  # noqa: BLE001 - an unrunnable claim fails with the reason, never raises
            return False, f"could not run: {e}"

    def settle_claims(self, stats: Optional[dict]) -> list[Settlement]:
        """Measure every LIVE task that carries a checkable claim; a claim measuring TRUE settles
        the task CONFIRMED (glue ground truth — even if the creature never said done). Unmeasurable
        defers; false leaves the task where it is (a commission task has no deadline — the operator
        is the backstop). Returns the settlements owed.

        `runs:` claims are the exception on both counts: EXECUTED only when the task is
        DONE_CLAIMED (the claim event is the trigger — one bounded run per done-claim, never a
        per-tick poll of an open task), and a FAILING run REOPENS the task with the output tail as
        feedback — glue rejection, the tightest loop the system has."""
        tasks = self.load()
        out: list[Settlement] = []
        changed = False
        from expectations import parse_claim, evaluate_claim
        for t in tasks:
            if t.state not in _LIVE or not t.claim:
                continue
            cmd = parse_runs_claim(t.claim)
            if cmd is not None:
                if t.state != DONE_CLAIMED:
                    continue                      # runs only on the claim event, never on open
                passed, tail = self._execute_runs_claim(cmd)
                changed = True
                if passed:
                    t.state = CONFIRMED
                    t.closed_ts = _now()
                    t.updated_ts = _now()
                    t.verdict_note = f"ran clean: `{cmd}` (exit 0)"
                    out.append(Settlement(task=t, how="claim", outcome=CONFIRMED,
                                          note=t.verdict_note,
                                          xp=COMMISSION_XP_CONFIRMED, feed=COMMISSION_FEED))
                else:
                    t.state = OPEN                # glue rejection — the error IS the feedback
                    t.claimed_ts = None
                    t.updated_ts = _now()
                    t.verdict_note = f"`{cmd}` failed: {tail}"[:DETAIL_MAX_CHARS]
                    out.append(Settlement(task=t, how="claim", outcome=REJECTED,
                                          note=t.verdict_note))
                continue
            claim = parse_claim(t.claim)
            if claim is None:
                continue
            verdict, actual = evaluate_claim(self.config, claim, stats)
            if verdict is True:
                t.state = CONFIRMED
                t.closed_ts = _now()
                t.updated_ts = _now()
                t.verdict_note = f"claim measured true ({t.claim}; actual: {actual})"
                changed = True
                out.append(Settlement(task=t, how="claim", outcome=CONFIRMED,
                                      note=t.verdict_note,
                                      xp=COMMISSION_XP_CONFIRMED, feed=COMMISSION_FEED))
        if changed:
            self._save(tasks)
        return out

    # --- settlement: operator half ------------------------------------------------------------
    def consume_verdicts(self) -> list[Settlement]:
        """Apply every pending operator verdict (one json file per verdict in
        `commission/verdicts/`, written by the dashboard). Each file is deleted once applied —
        consume-and-delete, the interventions pattern. Unknown ids / malformed files are dropped
        WITH A LOG, never silently. Returns the settlements owed (paid ones carry xp/feed)."""
        vdir = verdicts_dir(self.config)
        try:
            files = sorted(vdir.glob("*.json"))
        except OSError:
            return []
        if not files:
            return []
        tasks = self.load()
        by_id = {t.id: t for t in tasks}
        out: list[Settlement] = []
        changed = False
        for f in files:
            try:
                v = json.loads(f.read_text(encoding="utf-8"))
                verdict = str(v.get("verdict", ""))
                task = by_id.get(int(v.get("task_id", -1)))
                note = str(v.get("note", "")).strip()[:DETAIL_MAX_CHARS]
                if verdict not in VERDICTS or task is None:
                    logger.warning("commission verdict dropped (unknown task/verdict): %s", f.name)
                elif task.state in _TERMINAL:
                    logger.warning("commission verdict on settled task #%s ignored", task.id)
                elif verdict == "confirm":
                    task.state = CONFIRMED
                    task.closed_ts = _now()
                    task.updated_ts = _now()
                    task.verdict_note = note or "confirmed by the operator"
                    out.append(Settlement(task=task, how="operator", outcome=CONFIRMED,
                                          note=task.verdict_note,
                                          xp=COMMISSION_XP_CONFIRMED, feed=COMMISSION_FEED))
                    changed = True
                elif verdict == "reject":
                    task.state = OPEN                 # reopened — the note is the feedback
                    task.claimed_ts = None
                    task.updated_ts = _now()
                    task.verdict_note = note or "rejected by the operator"
                    out.append(Settlement(task=task, how="operator", outcome=REJECTED,
                                          note=task.verdict_note))
                    changed = True
                else:  # drop
                    task.state = DROPPED
                    task.closed_ts = _now()
                    task.updated_ts = _now()
                    task.verdict_note = note or "withdrawn by the operator"
                    out.append(Settlement(task=task, how="operator", outcome=DROPPED,
                                          note=task.verdict_note))
                    changed = True
            except (ValueError, TypeError, OSError) as e:
                logger.warning("commission verdict %s malformed: %s", f.name, e)
            try:
                f.unlink()
            except OSError:
                pass
        if changed:
            self._save(tasks)
        return out

    # --- the context strip --------------------------------------------------------------------
    def render_block(self) -> str:
        """The standing COMMISSION block for context: the brief head + the live todo (bounded).
        Returns "" when there is no brief AND no live tasks — the block simply doesn't exist for
        an uncommissioned creature."""
        brief = load_brief(self.config)
        live = self.live()
        if not brief and not live:
            return ""
        lines = ["COMMISSION (Charlie's standing order — long-horizon; work it between "
                 "everything else, add tasks as you decompose it):"]
        if brief:
            lines.append(brief)
        if live:
            lines.append("COMMISSION TODO (commission_add / commission_done; "
                         "done = a claim Charlie or a measurement confirms):")
            for t in live[:RENDER_MAX_TASKS]:
                mark = "…awaiting confirmation" if t.state == DONE_CLAIMED else "open"
                note = f"  [feedback: {t.verdict_note}]" if (t.state == OPEN and t.verdict_note) else ""
                # The revision hook: a reopened task that names its workshop job points BACK at the
                # session — continuing the builder that has the context beats rebuilding cold.
                fix = (f"  [the '{t.job}' workshop job holds this build — continue it "
                       "with the feedback]" if (t.state == OPEN and t.verdict_note and t.job) else "")
                lines.append(f"  #{t.id} [{mark}] {t.title}{note}{fix}")
            if len(live) > RENDER_MAX_TASKS:
                lines.append(f"  (+{len(live) - RENDER_MAX_TASKS} more)")
        # The exemplar (imitation beats instruction): the last CONFIRMED task, with the evidence
        # that won — the standard every new claim is implicitly measured against.
        done = [t for t in self.load() if t.state == CONFIRMED]
        if done:
            ex = max(done, key=lambda t: (t.closed_ts or "", t.id))   # id breaks same-second ties
            ev = (ex.evidence or "(no evidence given)")[:EXEMPLAR_NOTE_CHARS]
            why = (ex.verdict_note or "")[:EXEMPLAR_NOTE_CHARS]
            lines.append(f"THE BAR (your last confirmed work — match it): "
                         f"✓ #{ex.id} {ex.title} — evidence: {ev}; confirmed: {why}")
        return "\n".join(lines)


# ============================================================================================
# The dashboard's one writer: a verdict file (the ONLY commission write outside the engine)
# ============================================================================================
def write_verdict(config, *, task_id: int, verdict: str, note: str = "") -> Path:
    """Write one operator verdict for the engine to consume next tick. Called by the dashboard's
    chat-command route (a different PROCESS than the engine — hence file handoff, never a shared
    mutable store). Raises on an unknown verdict word so a typo'd command errors at the operator,
    not silently at the creature."""
    verdict = (verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VERDICTS)}")
    vdir = verdicts_dir(config)
    vdir.mkdir(parents=True, exist_ok=True)
    p = vdir / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"task_id": int(task_id), "verdict": verdict,
                               "note": str(note or "").strip(), "ts": _now()},
                              ensure_ascii=False), encoding="utf-8")
    replace_with_retry(tmp, p)
    return p
