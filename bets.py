"""Pillars 2.3: the bet ledger — recall becomes a wager, strength becomes earned
(PILLARS_PLAN §2 M-1 — the keystone; PILLARS_TODO 2.3; decision #5, Dean 2026-07-03).

The recall-utility loop closed: every engram the memory manager injects into a decision is logged
as an OPEN BET on that tick, and when the tick's outcome is adjudicated (glue's typed outcome
channel — the phase-1 fail_kind taxonomy, never the model's narration) the bet SETTLES into the
engram's `strength`. Strength stops being a seeded number and becomes EARNED usefulness. This one
closed loop is most of "memory improving over time".

Settlement is MECHANICAL ONLY (decision #5). Two credit channels:

  - SHARED  — every open bet on the tick gets a small credit on success / debit on failure.
              Co-presence with an outcome is weak evidence, so the coin is small (freeloader risk,
              pitfall #6) — and clique-only co-scorers get even that shrunk (see below).
  - STRONG  — an engram whose recalled FIX the action PROVABLY FOLLOWED gets a large credit
              (or, symmetrically, a large debit when the followed fix failed — a fix that
              provably did not work is the strongest possible evidence against the memory).

THE SIGNATURE-MATCH MECHANISM (what "provably followed" means): a fix signature is a normalized
action description — lowercased, digits collapsed to `#` (the episodes.py/_norm_cmd convention, so
port/version/count variants collapse), punctuation stripped to spaces. An engram carries one either
explicitly (`stats["fix_sig"]`, stamped by whoever encodes an error-pattern/recovery engram) or
implicitly (backtick-quoted spans in its body — the episode-body render puts the acted command in
backticks). The action actually taken is normalized the same way from the EXECUTED tool call
(`action_signature(tool, args)` — harness ground truth, not narration). The follow is PROVEN when
every content token of the fix signature appears in the action signature (containment, not
resemblance — `SIG_MATCH_MIN`), and the fix has at least `SIG_MIN_TOKENS` tokens (a bare tool name
proves nothing). Both sides of the match are computed by the harness; there is no code path by
which the LLM saying "I applied the fix" produces a match.

LLM SELF-REPORT NEVER SETTLES: `settle()` accepts `success` as a strict bool (a string — narration
— raises TypeError), and the glue hook (`glue.settle_bets`) reads the adjudicated outcome from
glue's own outcome log; it exposes no parameter through which a narrated claim could arrive.

STRENGTH UPDATE (§M-1): strength = clamp01(STRENGTH_DEFAULT + credit_sum × emotional_multiplier),
where credit_sum is a per-settlement DECAYING sum (new evidence outweighs old) and the multiplier
is the flashbulb stamp — arousal/valence AT ENCODING amplify earned credit in both directions, but
never create strength by themselves (a multiplier on zero credit is zero). `error`-kind engrams
decay SLOWER (scars persist: re-learning a known landmine is the most expensive forgetting; the
extinction of stale scars is pitfall #4's retest job, not fast decay's). `provenance='inherited'`
engrams keep a strength floor UNLESS contradicted by fresh experience — and a signature-matched
FAILURE (the inherited fix was provably followed and provably failed) IS that contradiction: it
drops the floor (plan M-2 forward-compatible via `stats["contradicted"]` too).

CLIQUE-CREDIT SHRINKAGE (pitfall #6): an engram that has only ever collected SHARED credit, always
alongside the same co-recalled partner(s), is indistinguishable from a freeloader riding a strong
memory's coattails. Its positive shared credit is shrunk (never its debits — shielding a freeloader
from losses would protect the ride). One STRONG settlement or one settlement as the tick's sole
bet is individual evidence and lifts the damper for good.

Ships DARK behind `config.pillars_bet_ledger_enabled` (default False): with the flag off,
`open_bets`/`settle` are inert (no file written, no strength mutated). Pure LIBRARY + one small
flag-gated glue hook — NOT imported by eidos.py/context.py; the cutover phase wires it.

Doctrine bindings (PILLARS_PLAN §0):
  §0.2  Mechanism, not behavior: this builds a wager ledger + mechanical settlement; "memory that
        improves over time" is what a creature running it does.
  §0.4  Every constant is a DECLARED knob with a one-line justification (below).
  §I6   All strength writes go through `Consolidator.update_strength` — the single writer. This
        module never touches the long-term store's files.
  §8    Both pitfall tests pass: the runaway (co-occurrence farming shared credit) is damped by
        clique shrinkage; gaming the strong channel requires actually executing the recorded fix,
        at which point it is not gaming.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import engram
from engram import Consolidator, Engram

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
SHARED_CREDIT = 0.02        # declared: the small shared-outcome coin. Co-presence is weak evidence
                            # (pitfall #6), so one tick moves strength by ~2% — dozens of consistent
                            # co-occurrences to matter, one strong follow to dwarf them all.
STRONG_CREDIT = 0.15        # declared: the provable recalled-fix-follow coin, 7.5× the shared one —
                            # causal evidence must dominate correlational (a single proven follow
                            # outweighs a week of co-presence).
CREDIT_DECAY = 0.90         # declared: per-settlement fade of prior credit (recency weighting —
                            # ~7-settlement half-life): what a memory did lately outweighs what it
                            # did long ago. Event-driven (per settlement), never wall-clock.
ERROR_CREDIT_DECAY = 0.97   # declared: `error`-kind engrams fade ~3× slower (~23-settlement
                            # half-life) — scars persist (§2): a failure pattern's usefulness does
                            # not expire with recency the way a fact's does, and its extinction is
                            # pitfall #4's retest job, not decay's.
EMO_GAIN = 0.5              # declared: a maximally-salient encoding amplifies earned credit ±50%
                            # (flashbulb, §M-1) — enough that high-arousal lessons outlast trivia,
                            # never so much that affect substitutes for evidence.
SIG_MATCH_MIN = 1.0         # declared: fraction of fix tokens that must appear in the action for a
                            # PROVEN follow. 1.0 = containment: "provable" means the whole recorded
                            # fix is present in what actually ran; lowering this would let near-
                            # misses farm the strong channel (§8 gaming test).
SIG_MIN_TOKENS = 2          # declared: a fix signature below this many content tokens (e.g. a bare
                            # tool name like `bash`) matches everything and therefore proves
                            # nothing — it is excluded from the strong channel.
SIG_TOKEN_MIN_LEN = 3       # declared: content tokens are ≥3 chars (the knowledge._overlap /
                            # engram._overlap convention) so glue words don't count as evidence.
MAX_OPEN_PER_TICK = 16      # declared: cap on bets logged per tick — a runaway recall set must not
                            # flood the ledger (recall itself budgets to ~a handful; 16 is headroom).
BETS_PERSIST_MAX = 400      # declared: bound on the persisted bet log (§M-3 no unbounded growth) —
                            # at ≤8 bets/tick that is ~50 ticks of auditable settlement history.
STALE_BET_TICKS = 8         # declared: an open bet older than this many ticks at settlement is
                            # VOIDED (no credit either way) — an outcome that never adjudicated
                            # proves nothing, and stale bets must not settle on an unrelated tick.
CLIQUE_MIN_SETTLES = 3      # declared: shared-only settlements before the freeloader damper can
                            # trigger — below three, "always together" is indistinguishable from
                            # coincidence.
CLIQUE_SHRINK = 0.25        # declared: the freeloader's shared credit multiplier — shrunk to ¼,
                            # not zeroed: a genuine dependency pair would starve at 0, while ¼
                            # breaks a freeloader's compounding (pitfall #6's damper).
CLIQUE_STATE_MAX = 512      # declared: bound on per-engram ledger bookkeeping entries (§M-3);
                            # least-recently-settled evicted first.


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _bets_path(config):
    return config.state_dir / "bets.jsonl"


def _state_path(config):
    return config.state_dir / "bets_state.json"


# =================================================================================================
# The signature mechanism (mechanical "the action provably followed the recalled fix")
# =================================================================================================
def _norm_sig(text: str) -> str:
    """Normalize an action/fix description into a signature: lowercase, digits→`#` (ports/versions/
    counts collapse — the episodes.py loop-detector convention), punctuation→space, whitespace
    collapsed. Deterministic and replayable."""
    s = (text or "").lower()
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[^a-z#._/\- ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def _sig_tokens(sig: str) -> set[str]:
    return {t for t in sig.split() if len(t) >= SIG_TOKEN_MIN_LEN}


def action_signature(tool: str, args: Any = None) -> str:
    """The signature of the action ACTUALLY TAKEN, computed by the harness from the executed tool
    call (ground truth — the model's narration never enters). For bash-like args the command text
    is the signature body; other args serialize deterministically."""
    if isinstance(args, dict):
        text = str(args.get("cmd") or args.get("command")
                   or json.dumps(args, sort_keys=True, ensure_ascii=False))
    elif args is None:
        text = ""
    else:
        text = str(args)
    return _norm_sig(f"{tool or ''} {text}")


def fix_signature_of(e: Engram) -> str:
    """The engram's recalled-fix signature: an explicit `stats['fix_sig']` (stamped at encode time
    by error-pattern/recovery encoders) wins; else the backtick-quoted spans in the body (the
    episode-body render puts the acted command in backticks); else '' (no strong channel)."""
    if isinstance(e.stats, dict):
        raw = e.stats.get("fix_sig")
        if isinstance(raw, str) and raw.strip():
            return _norm_sig(raw)
    spans = re.findall(r"`([^`]+)`", e.body or "")
    if spans:
        return _norm_sig(" ".join(spans))
    return ""


def signature_match(fix_sig: str, action_sig: str) -> bool:
    """True when the action PROVABLY followed the fix: every content token of the fix signature
    (≥ SIG_MATCH_MIN containment) appears in the action signature, and the fix carries at least
    SIG_MIN_TOKENS tokens (a bare tool name proves nothing)."""
    ft = _sig_tokens(_norm_sig(fix_sig))
    at = _sig_tokens(_norm_sig(action_sig))
    if len(ft) < SIG_MIN_TOKENS or not at:
        return False
    return (len(ft & at) / len(ft)) >= SIG_MATCH_MIN


# =================================================================================================
# The emotional-stamp multiplier (§M-1: flashbulb — salience AT ENCODING amplifies earned credit)
# =================================================================================================
def emotional_multiplier(encoded_at) -> float:
    """1 + EMO_GAIN × salience, salience = mean of |arousal| and |valence| at encoding, clamped to
    [0,1]. Amplifies credit in BOTH directions (a high-arousal failure scars deeper too); a neutral
    stamp multiplies by exactly 1. Affect never creates strength — it only scales evidence."""
    try:
        arousal = abs(float(getattr(encoded_at, "arousal", 0.0)))
        valence = abs(float(getattr(encoded_at, "valence", 0.0)))
    except (TypeError, ValueError):
        arousal = valence = 0.0
    salience = _clamp01((arousal + valence) / 2.0)
    return 1.0 + EMO_GAIN * salience


def _fresh_es() -> dict:
    """Per-engram ledger bookkeeping: individual-evidence count, shared-only count, the running
    intersection of co-bettor sets (the 'always with' clique), contradiction mark, recency."""
    return {"solo": 0, "shared": 0, "always_with": None, "contradicted_tick": None, "last_tick": 0}


# =================================================================================================
# The ledger
# =================================================================================================
class BetLedger:
    """Owns the persisted bet log (bounded jsonl, glue's outcomes.jsonl convention) and the
    per-engram bookkeeping sidecar (clique stats + contradiction marks — state the engram schema
    does not carry and the single-writer API does not expose; it lives HERE, in the adjudicator's
    books, not in the memory). All strength writes go through the Consolidator (§I6)."""

    def __init__(self, config, *, consolidator: Optional[Consolidator] = None):
        self.config = config
        self.consolidator = consolidator or Consolidator(config)

    @property
    def store(self):
        return self.consolidator.store

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "pillars_bet_ledger_enabled", False))

    # --- persistence (bounded; atomic temp+replace, house convention) --------------------------
    def _load_bets(self) -> list[dict]:
        try:
            txt = _bets_path(self.config).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        out = []
        for line in txt.splitlines():
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

    def _save_bets(self, rows: list[dict]) -> None:
        rows = rows[-BETS_PERSIST_MAX:]
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = _bets_path(self.config).with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                       encoding="utf-8")
        tmp.replace(_bets_path(self.config))

    def _load_state(self) -> dict:
        try:
            d = json.loads(_state_path(self.config).read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("engrams"), dict):
                return d
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {"engrams": {}}

    def _save_state(self, state: dict) -> None:
        engs = state.get("engrams", {})
        if len(engs) > CLIQUE_STATE_MAX:   # bound: evict least-recently-settled (§M-3)
            keep = sorted(engs.items(), key=lambda kv: int(kv[1].get("last_tick", 0)),
                          reverse=True)[:CLIQUE_STATE_MAX]
            state = {"engrams": dict(keep)}
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = _state_path(self.config).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_state_path(self.config))

    def all_bets(self) -> list[dict]:
        return self._load_bets()

    # --- open (every injected recall is a wager) ------------------------------------------------
    def open_bets(self, tick: int, injected_engrams: list) -> list[dict]:
        """Log every engram injected into this tick's decision as an OPEN bet. Accepts Engram
        objects or ids (ids are resolved against long-term; unknown ids are skipped). Idempotent
        per (tick, engram): re-logging the same injection is a no-op. Bounded per tick
        (MAX_OPEN_PER_TICK) and on disk (BETS_PERSIST_MAX). Inert with the flag off."""
        if not self.enabled:
            return []
        tick = int(tick)
        rows = self._load_bets()
        already = {(int(r.get("tick", -1)), r.get("eid")) for r in rows
                   if r.get("status") == "open"}
        opened: list[dict] = []
        for item in injected_engrams or []:
            if len(opened) >= MAX_OPEN_PER_TICK:
                break
            e = self.store.get(item) if isinstance(item, str) else item
            if not isinstance(e, Engram):
                continue
            if (tick, e.id) in already:
                continue
            rec = {"tick": tick, "eid": e.id, "kind": e.kind, "provenance": e.provenance,
                   "fix_sig": fix_signature_of(e), "status": "open", "ts": time.time()}
            rows.append(rec)
            already.add((tick, e.id))
            opened.append(rec)
        if opened:
            self._save_bets(rows)
        return opened

    # --- settle (MECHANICAL ONLY — decision #5) --------------------------------------------------
    def settle(self, *, tick: int, success: bool, action_sig: str = "") -> list[dict]:
        """Settle every open bet on `tick` against the ADJUDICATED outcome. `success` must be a
        strict bool — the typed outcome channel's verdict; a narrated string raises TypeError
        (LLM self-report never settles, decision #5). `action_sig` is the harness-computed
        signature of the action actually taken (action_signature()), the strong channel's ground.
        Open bets gone stale (older than STALE_BET_TICKS) are voided uncredited. Returns one
        settlement dict per settled bet. Inert with the flag off."""
        if not self.enabled:
            return []
        if not isinstance(success, bool):
            raise TypeError(
                "settle() requires an adjudicated bool outcome — a narrated/self-reported "
                f"outcome ({success!r}) cannot settle a bet (decision #5)")
        tick = int(tick)
        rows = self._load_bets()
        state = self._load_state()
        engs = state.setdefault("engrams", {})

        changed = False
        for r in rows:   # void stale opens: an outcome that never adjudicated proves nothing
            if r.get("status") == "open" and int(r.get("tick", 0)) < tick - STALE_BET_TICKS:
                r["status"] = "void"
                changed = True

        open_now = [r for r in rows if r.get("status") == "open"
                    and int(r.get("tick", -1)) == tick]
        if not open_now:
            if changed:
                self._save_bets(rows)
            return []

        ids = [r["eid"] for r in open_now]
        n = len(open_now)
        settlements: list[dict] = []
        for r in open_now:
            eid = r["eid"]
            es = engs.setdefault(eid, _fresh_es())
            matched = bool(r.get("fix_sig")) and bool(action_sig) \
                and signature_match(r["fix_sig"], action_sig)
            shrunk = False
            if matched:
                credit = STRONG_CREDIT if success else -STRONG_CREDIT
            else:
                credit = SHARED_CREDIT if success else -SHARED_CREDIT
                # Freeloader damper (pitfall #6): shrink POSITIVE shared credit only — shielding
                # a freeloader from debits would protect the ride. Judged on PRIOR history.
                if success and n > 1 and self._is_freeloader(es):
                    credit *= CLIQUE_SHRINK
                    shrunk = True
            applied = self._apply_credit(eid, credit, tick=tick, matched=matched,
                                         success=success, es=es)
            # Clique bookkeeping (after judging, so this settlement doesn't judge itself): a strong
            # match or a sole-bet tick is INDIVIDUAL evidence; else it shared and we intersect the
            # co-bettor set — the surviving intersection is the "always with" clique.
            co = sorted(set(ids) - {eid})
            if matched or n == 1:
                es["solo"] = int(es.get("solo", 0)) + 1
            else:
                es["shared"] = int(es.get("shared", 0)) + 1
                prev = es.get("always_with")
                es["always_with"] = co if prev is None else sorted(set(prev) & set(co))
            es["last_tick"] = tick
            r["status"] = "settled"
            r["settled_tick"] = tick
            r["credit"] = credit
            r["matched"] = matched
            settlements.append({"eid": eid, "tick": tick, "credit": credit,
                                "matched": matched, "shrunk": shrunk,
                                "strength": applied.get("strength")})
        self._save_bets(rows)
        self._save_state(state)
        return settlements

    @staticmethod
    def _is_freeloader(es: dict) -> bool:
        """Clique-only co-scorer (pitfall #6): enough shared-only settlements, zero individual
        evidence, and a non-empty 'always with' clique (some partner present in every one)."""
        return (int(es.get("shared", 0)) >= CLIQUE_MIN_SETTLES
                and int(es.get("solo", 0)) == 0
                and bool(es.get("always_with")))

    def _apply_credit(self, eid: str, credit: float, *, tick: int, matched: bool,
                      success: bool, es: dict) -> dict:
        """Fold one settlement's credit into the engram's strength through the SINGLE WRITER (§I6):
        credit_sum decays per settlement (kind-dependent rate), strength maps deterministically as
        clamp01(default + credit_sum × emotional multiplier), the inherited floor holds unless
        contradicted — and a signature-matched FAILURE on an inherited engram IS the contradiction
        (fresh experience provably refuted the inherited fix; plan M-2)."""
        e = self.store.get(eid)
        if e is None:   # pruned/merged away since the bet opened — the bet settles valueless
            return {"strength": None}
        if matched and not success and e.provenance == "inherited" \
                and es.get("contradicted_tick") is None:
            es["contradicted_tick"] = tick
        decay = ERROR_CREDIT_DECAY if e.kind == "error" else CREDIT_DECAY
        old_sum = float(e.stats.get("credit_sum", 0.0))
        new_sum = old_sum * decay + credit
        target = _clamp01(engram.STRENGTH_DEFAULT + new_sum * emotional_multiplier(e.encoded_at))
        contradicted = es.get("contradicted_tick") is not None or bool(e.stats.get("contradicted"))
        if e.provenance == "inherited" and not contradicted:
            target = max(target, engram.INHERITED_STRENGTH_FLOOR)
        self.consolidator.update_strength(eid, target, recalled_tick=tick,
                                          credit_delta=new_sum - old_sum)
        return {"strength": target}
