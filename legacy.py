"""legacy.py — lineage: the species gets smarter when the individual resets (WISDOM_PLAN §6).

A fresh slate today resets wisdom to hand-curated nuggets. This module makes RETIREMENT a
PUBLICATION event: before a creature is wiped, distill its REPLAY-VALIDATED corpus — the
strategy/procedure/fact guardrails that earned their keep, its scars, and its reflex registry —
into a versioned, committed `heirlooms/<creature-name>-<YYYYMMDD>.jsonl`. The next newborn seeds
from `preserved_nuggets.toml` (Charlie's letter) PLUS the latest heirloom volume, every item
stamped provenance `inherited` and strength-discounted (seed_knowledge.py owns the import half).

WHAT SURVIVES (the cross-stream selection contract — §6, §5, §2):
  - strategy / procedure / fact engrams that EARNED it: replay-validated (stats["replay_learned"]
    > 0, the §2 counterfactual-replay stream writes this) OR positive bet utility
    (stats["credit_sum"] > 0, the recall-bet ledger). Absent keys = unvalidated → NOT exported
    (WIS1: only earned experience is published — read the keys defensively, absent means "this
    stream hasn't validated it", never "assume yes").
  - SCARS: error engrams whose strength is above the scar floor (a hard-won failure lesson is
    worth inheriting even without a replay stat — a scar IS its own validation).
  - the REFLEX REGISTRY (state/reflexes.json if present), carried WITH provenance so a new body
    can see what its ancestor automated — but reflexes arrive DISARMED on import (WIS1 across
    generations; that discount is seed_knowledge's job, not this exporter's).

Each exported record is decision-shaped and self-describing:
  {kind, body, provenance_chain, stats_summary, exported_ts}
The volume opens with a HEADER line ({"header": {...generational metrics...}}) so a reader (and
the world-library book, wave-2) knows whose bookshelf this is and how far they got.

BOUNDED (WIS8): at most `HEIRLOOM_MAX_RECORDS` (~500), best-utility-first — the export is a
distillation, not a memory dump. BEST-EFFORT: a corrupt store, a missing workspace, an
unreadable persona — none of it blocks a slate. The exporter degrades to whatever it can read
and never raises to its caller (fresh_slate.sh warns loudly on a None/exception but slates
anyway).

Read-only on the retiring creature's stores. Writes ONLY under heirlooms/ (repo-side, committed).
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

# The retiring corpus lives in the engram long-term store; scars are error engrams there.
import engram

# ---------------------------------------------------------------------------------------------
# Declared bounds / thresholds (WIS8 — capped store; the numbers are documented, not magic)
# ---------------------------------------------------------------------------------------------
HEIRLOOM_MAX_RECORDS = 500     # declared: the volume cap — best-utility-first, then stop. A
                               # heirloom is a distilled bookshelf, not a full memory dump.
SCAR_FLOOR = 0.55              # declared: an error engram at or above this strength is a SCAR
                               # worth inheriting (a hard-won failure lesson that survived decay).
                               # Below it: a stale/weak failure not worth carrying to a new body.
_EXPORTABLE_KINDS = {"strategy", "procedure", "fact"}  # the utility-gated kinds (§6). Scars
                                                       # (error) go through their own floor gate.


# ---------------------------------------------------------------------------------------------
# Utility scoring — "how much did this memory EARN?" (best-first ordering + the export gate)
# ---------------------------------------------------------------------------------------------
def _stat_float(stats: dict, key: str) -> float:
    """A stat read defensively: absent/None/garbage → 0.0. The cross-stream keys (replay_learned,
    credit_sum) are written by OTHER streams; here we only read them, and an absent key means
    'that stream has not validated this engram', never a crash."""
    try:
        return float((stats or {}).get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _replay_learned(eg: "engram.Engram") -> float:
    return _stat_float(getattr(eg, "stats", {}), "replay_learned")


def _credit_sum(eg: "engram.Engram") -> float:
    return _stat_float(getattr(eg, "stats", {}), "credit_sum")


def _is_validated(eg: "engram.Engram") -> bool:
    """The §6 selection rule for the utility-gated kinds: replay-validated OR positive bet utility.
    Absent keys = unvalidated (defensive read; WIS1 — earned experience only)."""
    return _replay_learned(eg) > 0.0 or _credit_sum(eg) > 0.0


def _is_scar(eg: "engram.Engram", floor: float = SCAR_FLOOR) -> bool:
    """A scar: an error engram whose strength survived above the floor (a durable failure lesson)."""
    try:
        return eg.kind == "error" and float(eg.strength) >= float(floor)
    except (TypeError, ValueError):
        return False


def _export_utility(eg: "engram.Engram") -> float:
    """Best-first ordering key: how much this engram earned. Replay teaching + bet credit + a
    strength tiebreak (a scar with no replay stat still ranks by how deep it cut)."""
    try:
        s = float(eg.strength)
    except (TypeError, ValueError):
        s = 0.0
    return _replay_learned(eg) + _credit_sum(eg) + s


# ---------------------------------------------------------------------------------------------
# Provenance chain — "who taught this, and where did THEY get it?"
# ---------------------------------------------------------------------------------------------
def _provenance_chain(eg: "engram.Engram", ancestor: str) -> dict:
    """The lineage breadcrumb carried on every exported record: the ancestor who is publishing it,
    plus that engram's ORIGINAL provenance grade (experienced | told | inherited | dreamed). An
    already-inherited engram publishing forward records the chain honestly — a rumor passed down a
    second generation is still a rumor."""
    return {"ancestor": ancestor, "original_provenance": getattr(eg, "provenance", "experienced")}


def _stats_summary(eg: "engram.Engram") -> dict:
    """The earned-usefulness digest that travels with the record — enough for §5's
    faster-decay-until-verified rule to reason about an inherited claim without the full engram."""
    stats = getattr(eg, "stats", {}) or {}
    return {
        "strength": round(float(getattr(eg, "strength", 0.0) or 0.0), 4),
        "confidence": round(float(getattr(eg, "confidence", 0.0) or 0.0), 4),
        "replay_learned": _replay_learned(eg),
        "credit_sum": round(_credit_sum(eg), 4),
        "recall_count": int(_stat_float(stats, "recall_count")),
    }


# ---------------------------------------------------------------------------------------------
# Generational metrics — the heirloom header (whose bookshelf, and how far they got)
# ---------------------------------------------------------------------------------------------
def _creature_name(config) -> str:
    """The retiring creature's name — persona.json is the source of truth; fail-open to 'eidos'."""
    try:
        import persona
        p = persona.load_persona(config.workspace)
        name = (p.get("name") or "").strip()
        if name:
            return name
    except Exception:  # noqa: BLE001 - a missing/broken persona must not block an export
        pass
    return "eidos"


def _slugify(name: str) -> str:
    """A filesystem-safe volume slug from the creature name (lowercase alnum + hyphens)."""
    slug = "".join(c if c.isalnum() else "-" for c in (name or "").lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "eidos"


def _generational_metrics(config, ancestor: str) -> dict:
    """The header block: name, birth/retire ts, level, goals_completed, quests passed. Every field
    fails open independently — a corrupt persona still yields a header with the fields it could read."""
    metrics: dict[str, Any] = {
        "creature": ancestor,
        "birth_ts": None,
        "retire_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": None,
        "goals_completed": None,
        "quests_passed": None,
        "heirloom_version": 1,
    }
    try:
        import persona
        p = persona.load_persona(config.workspace)
        metrics["birth_ts"] = p.get("born")
        metrics["level"] = p.get("level")
        metrics["goals_completed"] = p.get("goals_completed")
    except Exception:  # noqa: BLE001
        pass
    try:
        import quests
        metrics["quests_passed"] = quests.QuestStore(config).passed_count()
    except Exception:  # noqa: BLE001 - quests store optional / unreadable
        pass
    return metrics


# ---------------------------------------------------------------------------------------------
# The reflex registry — carried with provenance (armed on THIS body → proposed on the NEXT)
# ---------------------------------------------------------------------------------------------
def _reflex_registry(config) -> list[dict]:
    """Read state/reflexes.json defensively (stream A owns reflexes.py; the file may be absent, and
    its exact shape may evolve). Returns the list of reflex records to carry. NEVER raises: a
    missing/corrupt registry contributes nothing to the volume."""
    path = config.state_dir / "reflexes.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    # Accept either a bare list or a {"reflexes": [...]} envelope (defensive to stream A's choice).
    if isinstance(raw, dict):
        reflexes = raw.get("reflexes", [])
    elif isinstance(raw, list):
        reflexes = raw
    else:
        return []
    return [r for r in reflexes if isinstance(r, dict)]


# ---------------------------------------------------------------------------------------------
# The exporter
# ---------------------------------------------------------------------------------------------
def _select_engrams(entries: list, *, scar_floor: float = SCAR_FLOOR) -> list:
    """Apply the §6 selection rule and return best-utility-first, capped. Two gates:
      - utility-gated kinds (strategy/procedure/fact): replay-validated OR positive bet utility.
      - scars (error kind): strength ≥ scar_floor.
    Everything else is dropped. Ordering is by export utility, so a bounded volume keeps the best."""
    selected = []
    for eg in entries:
        kind = getattr(eg, "kind", "")
        if kind in _EXPORTABLE_KINDS:
            if _is_validated(eg):
                selected.append(eg)
        elif kind == "error":
            if _is_scar(eg, scar_floor):
                selected.append(eg)
    selected.sort(key=_export_utility, reverse=True)
    return selected


def export_heirloom(config, out_dir: str = "heirlooms") -> Optional[Path]:
    """Distill the retiring creature's replay-validated corpus into a committed heirloom volume.

    Returns the Path of the written volume, or None if there was genuinely nothing to publish
    (no validated engrams, no scars, no reflexes — a creature that never earned anything leaves no
    bookshelf). BEST-EFFORT: this catches its own errors around each store and NEVER raises to the
    caller — a failed distillation must never block a fresh slate (fresh_slate.sh warns loudly on a
    None/exception but slates anyway).

    `out_dir` is repo-relative (anchored at this module's directory) unless absolute — the volume
    lives on the repo side, committed, as the creature lineage's bookshelf.
    """
    ancestor = _creature_name(config)

    # Read the retiring corpus (read-only; fail-open to empty on a corrupt/missing store).
    try:
        entries = engram.LongTermStore(config).load()
    except Exception:  # noqa: BLE001 - a corrupt long-term store yields no engram records
        entries = []

    selected = _select_engrams(entries)

    records: list[dict] = []
    exported_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for eg in selected:
        records.append({
            "kind": getattr(eg, "kind", "fact"),
            "body": getattr(eg, "body", ""),
            "provenance_chain": _provenance_chain(eg, ancestor),
            "stats_summary": _stats_summary(eg),
            "exported_ts": exported_ts,
        })

    # Reflex registry — carried with provenance, subject to the same volume cap.
    for r in _reflex_registry(config):
        records.append({
            "kind": "reflex",
            "body": r,   # the whole reflex record (trigger/action/provenance/status) is the body
            "provenance_chain": {"ancestor": ancestor,
                                 "original_provenance": "experienced",
                                 "source_status": r.get("status", "")},
            "stats_summary": {"fired_count": int(r.get("fired_count", 0) or 0),
                              "failed_count": int(r.get("failed_count", 0) or 0)},
            "exported_ts": exported_ts,
        })

    # Bound the volume (WIS8). Engrams already sort best-first; append reflexes, then cap the whole.
    if len(records) > HEIRLOOM_MAX_RECORDS:
        records = records[:HEIRLOOM_MAX_RECORDS]

    if not records:
        return None   # nothing earned its way onto the bookshelf

    header = {"header": _generational_metrics(config, ancestor)}
    header["header"]["record_count"] = len(records)

    # Resolve the output dir (repo-side, committed). Anchor a relative path at this module's dir so
    # it works regardless of the process CWD (fresh_slate.sh cd's to the repo root, but be robust).
    base = Path(out_dir)
    if not base.is_absolute():
        base = Path(os.path.dirname(os.path.abspath(__file__))) / out_dir
    try:
        base.mkdir(parents=True, exist_ok=True)
        _ensure_readme(base)
    except OSError:
        return None

    stamp = time.strftime("%Y%m%d", time.gmtime())
    volume = base / f"{_slugify(ancestor)}-{stamp}.jsonl"

    # Write: full content built in memory, one write. jsonl: header first, then records.
    lines = [json.dumps(header, ensure_ascii=False)]
    lines += [json.dumps(rec, ensure_ascii=False) for rec in records]
    try:
        volume.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return volume


# ---------------------------------------------------------------------------------------------
# The bookshelf README (created alongside the first volume — explains what heirlooms ARE)
# ---------------------------------------------------------------------------------------------
_README_TEXT = """\
# heirlooms/ — the creature lineage's bookshelf

Each file here is a **retirement volume**: when a creature is retired (`scripts/fresh_slate.sh`),
`legacy.py` distills its REPLAY-VALIDATED corpus — the strategy guardrails and procedures that
earned their keep, its scars (hard-won failure lessons), high-utility facts, and its reflex
registry — into a versioned `heirlooms/<creature-name>-<YYYYMMDD>.jsonl` BEFORE the wipe.

The next newborn seeds from `preserved_nuggets.toml` (Charlie's letter) **plus the latest heirloom
volume** (`seed_knowledge.py`). Every inherited item is stamped provenance `inherited`,
strength-discounted (the told/inherited floor), and decays faster until this new body VERIFIES it
for itself (an inherited claim it can't confirm is a rumor). Inherited reflexes arrive **disarmed,
as proposals** — a new body must re-earn its own automation (WIS1 across generations).

This is how **the species gets smarter when the individual resets** (WISDOM_PLAN §6). These files
are committed lineage — do NOT gitignore them. They are the bookshelf a new mind is born beside.

## Format
JSON Lines. The FIRST line is a header:

    {"header": {"creature": "...", "birth_ts": "...", "retire_ts": "...", "level": N,
                "goals_completed": N, "quests_passed": N, "heirloom_version": 1,
                "record_count": N}}

Each subsequent line is one heirloom record:

    {"kind": "strategy|procedure|fact|error|reflex",
     "body": "...",
     "provenance_chain": {"ancestor": "...", "original_provenance": "experienced|told|inherited"},
     "stats_summary": {"strength": ..., "credit_sum": ..., "replay_learned": ...},
     "exported_ts": "..."}
"""


def _ensure_readme(base: Path) -> None:
    """Create heirlooms/README.md if absent (explains the bookshelf). Best-effort."""
    readme = base / "README.md"
    if readme.exists():
        return
    try:
        readme.write_text(_README_TEXT, encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# Import-side helpers (shared with seed_knowledge.py — the reader half of the lineage contract)
# ---------------------------------------------------------------------------------------------
def latest_heirloom(out_dir: str = "heirlooms") -> Optional[Path]:
    """The newest heirloom volume (by filename, which is date-stamped), or None if the shelf is
    empty. Anchored at this module's dir when `out_dir` is relative (matches export_heirloom)."""
    base = Path(out_dir)
    if not base.is_absolute():
        base = Path(os.path.dirname(os.path.abspath(__file__))) / out_dir
    try:
        volumes = sorted(base.glob("*.jsonl"))
    except OSError:
        return None
    return volumes[-1] if volumes else None


def read_heirloom(path: Path) -> tuple[dict, list[dict]]:
    """Parse a heirloom volume → (header, records). Fail-open: a missing/corrupt file or a bad
    header line yields ({}, []) or the records it could read. NEVER raises."""
    header: dict = {}
    records: list[dict] = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}, []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if i == 0 and isinstance(obj, dict) and "header" in obj:
            header = obj.get("header") or {}
            continue
        if isinstance(obj, dict) and "kind" in obj:
            records.append(obj)
    return header, records
