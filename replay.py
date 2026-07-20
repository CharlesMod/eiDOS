"""WISDOM_PLAN §2 + §5 — counterfactual replay and utility-grounded curation: the loop that makes
memory IMPROVE decisions instead of merely accumulating them.

The two halves form one loop, run at the sleep window (from compaction.py's dream cycle, each behind
its own `[wisdom]` flag):

  - REPLAY (§2, `wisdom_replay_enabled`): deliberate practice during sleep. Sample a bounded batch of
    replayable episodes — FAILURE episodes whose fix was LATER VERIFIED (WIS1: we test whether memory
    teaches the *verified* answer, not an imagined one). For each, reconstruct the decision prompt AS
    IT WOULD BE TODAY — the recorded situation PLUS today's real recall for it (the whole point: does
    current memory teach the right move?) — and make ONE grammar-constrained action call. NOTHING is
    executed (WIS4: replay is counterfactual, never causal — no tool runs in a dream). The replayed
    action is SCORED by action-signature against the recorded ground truth:
      matches the verified fix     → "learned"    (the recalled memories demonstrably taught it)
      matches the original failure → "unlearned"  (the guardrail that should have fired didn't)
      matches neither              → "divergent"  (recorded, never settled — honesty about the unknown)
    Settlement (WIS1) rides the EXISTING bet-ledger path (bets.BetLedger.settle_replay): a learned
    replay credits the recalled memories and tallies `replay_learned`; an unlearned one debits and
    tallies `replay_unlearned`. The per-sleep tally `{learned, unlearned, divergent}` is appended to
    a bounded `state/replay_history.jsonl` — D3's real "wakes up smarter" number.

  - CURATION (§5, `wisdom_curation_enabled`): extends the sleep engine's SHY prune with a utility
    signal. A long-term memory's utility record = §2's replay tallies + §2.3's bet credit
    (`credit_sum`). Empty-or-negative utility across `wisdom_curation_grace_sleeps` sleeps →
    ACCELERATED decay REGARDLESS of recall frequency (being surfaced a lot and never helping is the
    garden's noise failure mode); positive utility → the slow-decay protection scars get. Inherited
    memories decay faster until they verify. Below the floor a memory is DEMOTED TO ARCHIVE, never
    deleted (supersede-not-delete). A one-line report goes to the dream log.

THE VERIFIED-FIX RULE (WIS1, mechanical + conservative — documented so it is auditable):
An episode is replayable when the RAW episode ledger (`episodes.jsonl` — harness-adjudicated
`{situation → action → outcome}` records, `success` is glue's typed channel, never narration) shows,
for one situation KEY, BOTH a FAILED action signature AND — at a LATER tick, same situation key — a
SUCCEEDED action signature. That later success IS the verified fix (the recovery actually happened and
was adjudicated to work — the exact recovery `episodes.recall` already derives). The failed signature
is the "original failing action"; the succeeded signature is the "verified fix". We do NOT trust the
model's claim that a fix works, a dreamed distillation, or an open/uncorroborated recovery — only a
failure that DEMONSTRABLY recovered in-situ counts. This is the most conservative rule the existing
machinery supports without inventing a new verified-fix ledger.

DARK by config: both halves are gated (`wisdom_replay_enabled` / `wisdom_curation_enabled`, default
False). With a flag off its function is a byte-identical no-op — the two flags are independent (WIS7).
Bounded throughout (WIS8): batch-capped replay, bounded history file, atomic writes, fail-open reads.

Doctrine bindings: WIS1 (adjudicated-only), WIS4 (counterfactual — never executes), WIS7 (flag-dark,
byte-identical off), WIS8 (bounded). ARCHITECTURE_PRINCIPLES #4 (the system never lies): a skip is a
typed, logged reason ({"skipped": ...}) — never a success-wrapped nothing.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("eidos.replay")

# --- Declared knobs (each a labeled design knob with its one-line justification) -----------------
REPLAY_MIN_RESERVE = 0.15          # declared: replay costs K LLM calls (K = wisdom_replay_batch); it
                                   # is skipped entirely when the metabolic reserve is at/below this
                                   # (WIS: "skipped when the metabolic reserve is low"). 0.15 matches
                                   # the sleep-arousal floor the body already treats as "run nothing
                                   # optional" — deliberate practice is a luxury a starving body skips.
REPLAY_RECALL_BUDGET_CHARS = 2000  # declared: char budget for the recall block reconstructed into a
                                   # replay prompt — half the manager's default (a replay tests the
                                   # DECISION-shaping slice of recall, not the whole working-memory
                                   # repopulation a live tick needs), kept bounded so one replay prompt
                                   # can't balloon the K-call spend.
REPLAY_HISTORY_MAX = 400           # declared: bound on state/replay_history.jsonl (WIS8; matches the
                                   # bet log's BETS_PERSIST_MAX) — ~months of nightly replay reports
                                   # at one line per sleep, auditable without unbounded growth.
REPLAY_SCAN_WINDOW = 1200          # declared: how far back the episode ledger is scanned for
                                   # replayable material (mirrors episodes._RECALL_WINDOW so replay
                                   # sees the same recent horizon recall does).

# §5 curation knobs -------------------------------------------------------------------------------
CURATION_DECAY_ACCEL = 3.0         # declared: multiplier on the per-sleep SHY decay for a memory that
                                   # has gone empty-or-negative-utility past its grace window — noise
                                   # fades ~3× faster than an ordinary trace (the same 3× ratio the
                                   # error-kind SLOW-decay privilege uses, inverted: proven-useless is
                                   # forgotten as fast as proven-useful is kept).
CURATION_DECAY_PROTECT = 0.25      # declared: multiplier on the per-sleep decay for a memory with
                                   # POSITIVE utility — it keeps the same slow-decay protection scars
                                   # get (error engrams already decay ~3× slower; a proven-useful fact
                                   # earns the same shelter, ¼ the ordinary rate).
CURATION_INHERITED_ACCEL = 2.0     # declared: inherited memories that have NOT yet earned utility
                                   # decay faster than an ordinary un-earned memory (an inherited claim
                                   # this creature can't verify is a rumor, §5) — 2× baseline until a
                                   # single positive utility event proves it, at which point it takes
                                   # the protected rate like any other earner.
CURATION_ARCHIVE_FLOOR = 0.05      # declared: strength at/below which a memory is DEMOTED to the
                                   # archive tier (never deleted — supersede-not-delete). Just above 0
                                   # so a memory has to be genuinely spent, not merely weak, to leave
                                   # the active store; the archived copy is recoverable.
ARCHIVE_STAT = "archived_tick"     # the stats mark a demoted memory carries (the archive tier is a
                                   # flag on the engram, mirroring the store's supersede discipline —
                                   # no second store).
GRACE_STAT = "curation_grace"      # per-memory grace counter: consecutive sleeps of empty/negative
                                   # utility (reset to 0 the moment utility turns positive).


# =================================================================================================
# The verified-fix rule (WIS1) — pick replayable episodes from the raw adjudicated episode ledger.
# =================================================================================================
def _episodes_path(config) -> Path:
    return config.workspace / "episodes.jsonl"


def _read_episodes(config, *, limit: int = REPLAY_SCAN_WINDOW) -> list[dict]:
    """Best-effort read of the raw episode ledger, oldest-first, most-recent `limit` (episodes.py's
    read convention: skip corrupt lines, never raise)."""
    try:
        lines = _episodes_path(config).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def replayable_episodes(config) -> list[dict]:
    """Every replayable episode in the current ledger (WIS1's verified-fix rule, above).

    Groups adjudicated episode records by situation KEY; a situation is replayable when it holds a
    FAILED action signature AND — at a strictly later tick — a SUCCEEDED action signature. The failed
    record is the decision-to-replay; the later success is the VERIFIED FIX. Returns one dict per
    replayable situation:
        {key, situation, fail_sig, fail_tool, fix_sig, fix_tool, fail_tick, fix_tick, fail_summary}
    Deterministic, pure read — no model, no store mutation."""
    eps = _read_episodes(config)
    if not eps:
        return []
    # Per situation key: the earliest FAILED record and, separately, the earliest LATER SUCCEED whose
    # signature differs from the failure (a fix that IS the failing sig taught nothing new).
    by_key: dict[str, list[dict]] = {}
    for e in eps:
        key = str(e.get("key", ""))
        if not key:
            continue
        by_key.setdefault(key, []).append(e)
    out: list[dict] = []
    for key, recs in by_key.items():
        recs.sort(key=lambda r: (int(r.get("tick", 0) or 0)))
        fail = next((r for r in recs if not r.get("success")), None)
        if fail is None:
            continue
        fail_tick = int(fail.get("tick", 0) or 0)
        fail_sig = str(fail.get("sig") or fail.get("tool") or "")
        # The verified fix: the FIRST success AFTER the failure whose signature differs (a genuine
        # recovery, not a re-log of the same action). Same situation key = same problem recovered.
        fix = next((r for r in recs
                    if r.get("success")
                    and int(r.get("tick", 0) or 0) > fail_tick
                    and str(r.get("sig") or r.get("tool") or "") != fail_sig), None)
        if fix is None:
            continue
        out.append({
            "key": key,
            "situation": key,
            "fail_sig": fail_sig,
            "fail_tool": str(fail.get("tool") or ""),
            "fail_summary": str(fail.get("summary") or ""),
            "fail_kind": str(fail.get("fail_kind") or ""),
            "fail_tick": fail_tick,
            "fix_sig": str(fix.get("sig") or fix.get("tool") or ""),
            "fix_tool": str(fix.get("tool") or ""),
            "fix_tick": int(fix.get("tick", 0) or 0),
        })
    return out


# =================================================================================================
# Sampling (bias: recent + high-strength + never-replayed)
# =================================================================================================
def _episode_engram_for(manager, situation: str):
    """The long-term `episode` engram carrying this situation key (memory_manager stamps it in
    stats['situation']), or None. This is the atom whose stats track replay ('replayed', tallies)."""
    try:
        for e in manager.store.load():
            if e.kind == "episode" and str(e.stats.get("situation", "")) == situation:
                return e
    except Exception:  # noqa: BLE001 - a store read must never break sampling
        return None
    return None


def _sample(candidates: list[dict], manager, *, batch: int) -> list[dict]:
    """Choose up to `batch` replayable episodes, biased toward recent + high-strength + NEVER-replayed.
    Deterministic (no RNG): the bias is a sort key, so a test can predict the pick. The engram behind
    each situation supplies strength + the `replayed` count; a situation with no engram yet ranks as
    never-replayed at neutral strength (it still deserves a first replay)."""
    scored: list[tuple] = []
    for c in candidates:
        eg = _episode_engram_for(manager, c["situation"])
        replayed = int(eg.stats.get("replayed", 0)) if eg is not None else 0
        strength = float(eg.strength) if eg is not None else 0.5
        # Never-replayed first (replayed asc), then recent (fail_tick desc), then strong (desc).
        scored.append((replayed, -int(c.get("fail_tick", 0)), -strength, c))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in scored[: max(0, int(batch))]]


# =================================================================================================
# Prompt reconstruction (recorded situation + TODAY's recall) + the one grammar-constrained call.
# =================================================================================================
def reconstruct_prompt(config, episode: dict, manager, *, recall_engrams=None) -> tuple:
    """Rebuild the decision prompt for a replayable episode AS IT WOULD BE TODAY: the RECORDED
    situation plus the recall the creature would get for it RIGHT NOW (real memory_manager recall —
    that is what makes replay TEST whether current memory teaches). Returns (messages, recalled_ids):
    `recalled_ids` are the long-term engram ids that recall surfaced — the memories a learned verdict
    credits (WIS1's settlement targets). `recall_engrams` may be injected (tests); else pulled live."""
    situation = episode["situation"]
    step = situation.split("|", 1)[1] if "|" in situation else situation
    if recall_engrams is None:
        try:
            recall_engrams = manager.recall(step, situation=situation,
                                             budget_chars=REPLAY_RECALL_BUDGET_CHARS)
        except Exception:  # noqa: BLE001 - a recall fault yields an empty block, not a crash
            recall_engrams = []
    recalled_ids = [e.id for e in (recall_engrams or [])]
    recall_block = "\n".join(f"- {e.body}" for e in (recall_engrams or [])) or "(no recall)"
    system = (
        "You are eiDOS deciding your next action. Emit ONE tool call in the "
        "<tool>NAME</tool><args>{...}</args> form. This is a DECISION — choose the single best action."
    )
    user = (
        f"Situation: {step}\n\n"
        f"## Before you act — your precedents (verify they transfer):\n{recall_block}\n\n"
        "Choose the one action to take now."
    )
    return ([{"role": "system", "content": system},
             {"role": "user", "content": user}], recalled_ids)


def _action_grammar(config) -> Optional[str]:
    """The EXISTING action grammar, constrained to the live tool registry (reuse — WIS1 no new
    action language). Fail-open to None (unconstrained) if the registry can't be built here — the
    scorer normalizes whatever comes back the same way, so an unconstrained emission still scores."""
    try:
        import grammar as _grammar
        from tools import visible_tools
        names = list(visible_tools(config).keys())    # the ONE accessor the live loop's grammar reads
        if not names:
            return None
        return _grammar.tick_grammar_cached(names)
    except Exception:  # noqa: BLE001 - grammar is an optimization; scoring works without it
        return None


def _replayed_action_signature(text: str) -> str:
    """Parse the replayed model output into an action signature via the EXISTING seams: parser
    extracts the tool call, bets.action_signature normalizes it (the same ground-truth signature the
    live loop and the bet ledger compute). '' when nothing parseable came back."""
    import bets
    try:
        import parser as _parser
        call = _parser.parse_tool_call(text or "")
    except Exception:  # noqa: BLE001
        call = None
    if call is None:
        return ""
    return bets.action_signature(getattr(call, "tool", ""), getattr(call, "args", None))


# =================================================================================================
# Scoring (three-way, mechanical) — signature match against RECORDED ground truth.
# =================================================================================================
def score_replay(replayed_sig: str, episode: dict) -> str:
    """Compare the replayed action signature to the recorded ground truth (WIS1, three-way):
      matches the VERIFIED FIX     → 'learned'
      matches the ORIGINAL FAILURE → 'unlearned'
      neither                      → 'divergent'
    Uses bets.signature_match (containment, the same 'provably followed' test the strong bet channel
    uses). The fix is checked FIRST: a replay that reproduces the fix is a learn even if the fix
    happens to share tokens with the failure."""
    import bets
    if not replayed_sig:
        return "divergent"
    if bets.signature_match(episode.get("fix_sig", ""), replayed_sig):
        return "learned"
    if bets.signature_match(episode.get("fail_sig", ""), replayed_sig):
        return "unlearned"
    return "divergent"


# =================================================================================================
# The reserve gate + LLM reachability (skip discipline — WIS: sleep never hangs on replay).
# =================================================================================================
def _reserve_ok(config) -> bool:
    """True unless the metabolic reserve is at/below REPLAY_MIN_RESERVE. Fail-open: no reserve on
    record (a fresh box, a test) reads as full — replay runs. A genuinely low reserve skips replay."""
    try:
        path = config.state_dir / "metabolism.json"
        d = json.loads(path.read_text(encoding="utf-8"))
        return float(d.get("energy", 1.0)) > REPLAY_MIN_RESERVE
    except (OSError, ValueError, TypeError):
        return True


# =================================================================================================
# The replay job (one bounded pass; called from the sleep/dream window behind wisdom_replay_enabled).
# =================================================================================================
def run_replay(config, *, manager=None, llm: Optional[Callable[..., str]] = None,
               ledger=None, tick: int = 0) -> dict:
    """One bounded counterfactual-replay pass. Returns a small JSON-able report dict. Skips GRACEFULLY
    (a typed {"skipped": reason}, never a hang or a lie) when the flag is off, the reserve is low, the
    LLM is unreachable, or there is no replayable material.

    `manager`  — a MemoryManager (recall + the engram store). Constructed from config if omitted.
    `llm`      — a callable (messages, *, grammar=None) -> str (the mock in tests; llm.complete live).
                 None ⇒ skip (the sleep must never hang on an unreachable mind, WIS §2 budget).
    `ledger`   — a bets.BetLedger for settlement (constructed from config if omitted).
    `tick`     — the current tick, stamped into the history line and the settlement recency anchor."""
    if not getattr(config, "wisdom_replay_enabled", False):
        return {"skipped": "flag off"}
    if not _reserve_ok(config):
        return {"skipped": "low reserve"}
    if llm is None:
        return {"skipped": "no llm"}

    if manager is None:
        from memory_manager import MemoryManager
        manager = MemoryManager(config)
    if ledger is None:
        import bets
        ledger = bets.BetLedger(config)

    candidates = replayable_episodes(config)
    if not candidates:
        return {"skipped": "no replayable episodes", "learned": 0, "unlearned": 0, "divergent": 0}

    batch = int(getattr(config, "wisdom_replay_batch", 4))
    chosen = _sample(candidates, manager, batch=batch)
    grammar = _action_grammar(config)

    learned = unlearned = divergent = 0
    episode_ids: list[str] = []
    for ep in chosen:
        messages, recalled_ids = reconstruct_prompt(config, ep, manager)
        try:
            # WIS4: this NEVER executes an action — it only asks the model what it WOULD do, and scores
            # the answer against recorded ground truth. No execute_tool, no side effect.
            output = llm(messages, grammar=grammar)
        except Exception as e:  # noqa: BLE001 - an unreachable mind mid-batch ends the batch cleanly,
            #                                     never hangs the sleep (WIS §2 budget: sleep is safe).
            logger.warning("replay: llm call failed, ending batch: %s", e)
            break
        verdict = score_replay(_replayed_action_signature(output or ""), ep)
        eg = _episode_engram_for(manager, ep["situation"])
        if eg is not None:
            # Track the replay on the episode engram (never-replayed bias reads this) + the tally.
            try:
                manager.consolidator.bump_stats(eg.id, {"replayed": 1})
            except Exception as e:  # noqa: BLE001
                logger.warning("replay: could not mark episode replayed: %s", e)
            episode_ids.append(eg.id)
        if verdict == "learned":
            learned += 1
            # Settlement (WIS1): the recalled memories demonstrably taught the verified fix.
            try:
                ledger.settle_replay(tick=tick, engram_ids=recalled_ids, learned=True)
                if eg is not None:
                    manager.consolidator.bump_stats(eg.id, {"replay_learned": 1})
            except Exception as e:  # noqa: BLE001 - settlement is best-effort; the report still lands
                logger.warning("replay: learned settlement failed: %s", e)
        elif verdict == "unlearned":
            unlearned += 1
            # The failed-guardrail path: the memories that should have steered away instead taught the
            # old failure — they take the loss and the `replay_unlearned` tally.
            try:
                ledger.settle_replay(tick=tick, engram_ids=recalled_ids, learned=False)
                if eg is not None:
                    manager.consolidator.bump_stats(eg.id, {"replay_unlearned": 1})
            except Exception as e:  # noqa: BLE001
                logger.warning("replay: unlearned settlement failed: %s", e)
        else:
            divergent += 1   # divergent records only — no settlement (honesty about the unscorable).

    report = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tick": int(tick),
              "learned": learned, "unlearned": unlearned, "divergent": divergent,
              "episode_ids": episode_ids}
    _append_history(config, report)
    return report


def _append_history(config, report: dict) -> None:
    """Append one replay report to the bounded state/replay_history.jsonl (WIS8; atomic trim). This is
    D3's real number — the learned-rate trend across sleeps."""
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        path = config.state_dir / "replay_history.jsonl"
        try:
            existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            existing = []
        existing = [ln for ln in existing if ln.strip()]
        existing.append(json.dumps(report, ensure_ascii=False))
        existing = existing[-REPLAY_HISTORY_MAX:]
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("replay: history append failed: %s", e)


# =================================================================================================
# §5 — utility-grounded curation (extends the sleep engine's SHY prune, behind wisdom_curation_enabled)
# =================================================================================================
def _utility_of(e) -> float:
    """A memory's demonstrated-utility signal: §2's replay tallies + §2.3's decaying bet credit.
    Positive = it has helped (learned in replay, or net-positive settled wagers); ≤0 = empty or
    net-negative (never helped, or recalled-into-failure — the garden's noise)."""
    stats = e.stats or {}
    learned = int(stats.get("replay_learned", 0) or 0)
    unlearned = int(stats.get("replay_unlearned", 0) or 0)
    credit = float(stats.get("credit_sum", 0.0) or 0.0)
    return (learned - unlearned) + credit


def curate(config, *, base_decay: Optional[float] = None) -> dict:
    """Utility-grounded curation over the long-term store (§5), applied at the sleep window on top of
    the SHY per-sleep decay. Returns a one-line-worthy report dict {scanned, protected, accelerated,
    demoted, store, report}. Byte-identical no-op when `wisdom_curation_enabled` is off.

    The rule (per long-term engram):
      - utility > 0                 → grace reset to 0; PROTECTED decay (scars' slow rate).
      - utility ≤ 0                 → grace += 1. Past `wisdom_curation_grace_sleeps` (or immediately
                                      for an un-earned INHERITED memory) → ACCELERATED decay, REGARDLESS
                                      of recall_count (surfaced-a-lot-and-never-helping IS the noise).
                                      Within grace → the ordinary base decay (a chance to earn).
      - after decay, strength ≤ CURATION_ARCHIVE_FLOOR → DEMOTE to archive (a stats mark; the engram is
        kept, supersede-not-delete) so it stops ranking into recall but remains recoverable.
    All writes go through the single writer (§I6) via the consolidator's module-level bridge."""
    if not getattr(config, "wisdom_curation_enabled", False):
        return {"skipped": "flag off"}
    import engram as _eg
    from engram import Consolidator
    cons = Consolidator(config)
    entries = cons.store.load()
    if not entries:
        return {"scanned": 0, "protected": 0, "accelerated": 0, "demoted": 0, "store": 0,
                "report": "curation: empty store"}

    if base_decay is None:
        try:
            from nervous.sleep import STRENGTH_DECAY_PER_SLEEP as _d
            base_decay = float(_d)
        except Exception:  # noqa: BLE001 - fall back to the same documented SHY constant value
            base_decay = 0.03
    grace_window = int(getattr(config, "wisdom_curation_grace_sleeps", 10))

    protected = accelerated = demoted = 0
    for e in entries:
        if e.stats.get(ARCHIVE_STAT):   # already archived — leave it (it no longer ranks into recall)
            continue
        util = _utility_of(e)
        if util > 0:
            e.stats[GRACE_STAT] = 0
            decay = base_decay * CURATION_DECAY_PROTECT
            protected += 1
        else:
            grace = int(e.stats.get(GRACE_STAT, 0) or 0) + 1
            e.stats[GRACE_STAT] = grace
            inherited_unearned = (e.provenance == "inherited")
            if grace > grace_window:
                decay = base_decay * CURATION_DECAY_ACCEL
                accelerated += 1
            elif inherited_unearned:
                # An inherited claim decays faster until it verifies — a rumor this body can't check.
                decay = base_decay * CURATION_INHERITED_ACCEL
                accelerated += 1
            else:
                decay = base_decay   # still within grace: the ordinary rate, a chance to earn utility
        e.strength = max(0.0, min(1.0, e.strength - decay))
        if e.strength <= CURATION_ARCHIVE_FLOOR:
            e.stats[ARCHIVE_STAT] = int(time.time())   # demote-to-archive: kept, no longer recalled
            demoted += 1

    _eg._commit_to_store(cons.store, entries)   # single writer (§I6): one rewrite for the whole pass
    report = (f"curation: {demoted} demoted, {protected} reinforced, "
              f"{accelerated} accelerated, store {len(entries)}")
    return {"scanned": len(entries), "protected": protected, "accelerated": accelerated,
            "demoted": demoted, "store": len(entries), "report": report}
