"""Seed eiDOS's long-term knowledge store with bootstrapping self-knowledge.

The curated nuggets themselves live in **preserved_nuggets.toml** — the small,
hand-edited database of "what eiDOS should ALWAYS know" (its identity, the
infrastructure, hard-won lessons, the operating-manual pointer). This module is
just the loader: it reads that TOML and writes each nugget into the knowledge
store, tagged as a bootstrap seed. Edit the TOML to change what a fresh eiDOS
starts knowing; you don't touch this file.

Beyond Charlie's letter (the nuggets), a newborn ALSO inherits from the LATEST
HEIRLOOM VOLUME (heirlooms/, written by legacy.py at the previous creature's
retirement — WISDOM_PLAN §6). Every heirloom item is stamped provenance
`inherited` and strength-discounted (the told/inherited floor the engram economy
already uses for nuggets), so the species gets smarter when the individual
resets — but a new body must still EARN each inherited claim (§5's
faster-decay-until-verified rule) rather than trust it blind. Inherited reflexes
are written DISARMED, as `proposed` (WIS1 across generations: a new body re-earns
its own automation, it never wakes up with an ancestor's reflexes armed).

Run after a workspace reset:  python seed_knowledge.py
                              python seed_knowledge.py --no-heirloom   (nuggets only)
"""

import hashlib
import json
import os
import sys

KDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KDIR)

from config import load_config  # noqa: E402
import knowledge  # noqa: E402

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

PRESERVED_PATH = os.path.join(KDIR, "preserved_nuggets.toml")
# Machine-local nuggets that must NEVER enter git (device credentials, keys). Same format,
# gitignored; absent on a fresh clone, which is fine.
LOCAL_PATH = os.path.join(KDIR, "preserved_nuggets.local.toml")


def load_nuggets(path: str = PRESERVED_PATH, optional: bool = False):
    """Load a preserved nuggets database -> list of (category, tags, content).

    Degrades gracefully: returns [] (with a printed warning, unless `optional`) if the
    file is missing or unparseable, so a reset can't crash on a bad edit — the operator
    just sees "seeded 0/0" and investigates.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if not optional:
            print(f"  ! preserved nuggets database not found: {path} (seeding nothing)")
        return []
    except Exception as e:  # noqa: BLE001 - corrupt TOML, etc.
        print(f"  ! failed to parse {path}: {e} (seeding nothing)")
        return []
    out = []
    for n in data.get("nugget", []):
        content = (n.get("content") or "").strip()
        if not content:
            continue
        out.append((n.get("category", "facts"), list(n.get("tags", [])), content))
    return out


# Curated bootstrap knowledge, loaded from preserved_nuggets.toml at import time.
NUGGETS = load_nuggets()


# =================================================================================================
# Heirloom inheritance (WISDOM_PLAN §6) — the reader half of the lineage contract (legacy.py writes)
# =================================================================================================
# The idempotency src marker for an inherited engram, in the SAME shape memory_manager's importer
# uses (stats["src"], stats["src_id"]) so a re-seed recognizes an already-imported heirloom and the
# consolidator's dedup carries. "heirloom" distinguishes these from "nuggets" imports.
_HEIRLOOM_SRC = "heirloom"


def _heirloom_src_id(kind: str, body_repr: str) -> str:
    """A stable content-hash id for a heirloom record (records have no persistent id of their own),
    so re-running seed never duplicates an inherited engram (idempotency ledger)."""
    h = hashlib.sha1(f"{kind}\x00{body_repr}".encode("utf-8", "replace")).hexdigest()
    return f"{kind}:{h[:16]}"


def import_heirloom(cfg, *, out_dir: str = "heirlooms") -> dict:
    """Import the LATEST heirloom volume into the newborn's memory economy (WISDOM_PLAN §6).

    Returns {"engrams": n_new, "reflexes": n_proposed, "volume": name|None}. Fail-open / best-effort
    — a missing shelf, a corrupt volume, an absent engram economy: none of it crashes a reseed
    (the newborn just inherits nothing and starts from the nuggets). Idempotent: engrams carry an
    src marker + go through the consolidator's dedup; reflexes are keyed by id in reflexes.json.

      - strategy/procedure/fact/error engrams → engrams stamped provenance `inherited`, seeded at
        the told/inherited strength FLOOR (the exact discount the nugget importer uses), and thereby
        subject to §5's faster-decay-until-verified rule.
      - reflexes → written to state/reflexes.json as status `proposed`, NEVER armed (WIS1 across
        generations): a new body must re-earn its own automation.
    """
    import legacy

    result = {"engrams": 0, "reflexes": 0, "volume": None}
    volume = legacy.latest_heirloom(out_dir)
    if volume is None:
        return result
    result["volume"] = volume.name
    header, records = legacy.read_heirloom(volume)

    engram_records = [r for r in records if r.get("kind") != "reflex"]
    reflex_records = [r for r in records if r.get("kind") == "reflex"]

    result["engrams"] = _import_heirloom_engrams(cfg, engram_records)
    result["reflexes"] = _import_heirloom_reflexes(cfg, reflex_records, header)
    return result


def _import_heirloom_engrams(cfg, engram_records: list) -> int:
    """Write inherited engrams through the consolidator with provenance='inherited' + the strength
    floor — the SAME path memory_manager.import_nuggets uses for Charlie's letter. Idempotent via a
    content-hash src marker; the consolidator's pattern-separation dedup carries near-restatements."""
    if not engram_records:
        return 0
    try:
        import engram
        from engram import Consolidator, Engram, INHERITED_STRENGTH_FLOOR
    except Exception as e:  # noqa: BLE001 - engram economy optional in a bare env
        print(f"  ! heirloom engram import skipped (engram economy unavailable): {e}")
        return 0

    consolidator = Consolidator(cfg)
    # The idempotency ledger: (src, src_id) markers already in long-term (mirrors the importer).
    seen = set()
    for e in consolidator.store.load():
        src = e.stats.get("src")
        sid = e.stats.get("src_id")
        if src and sid:
            seen.add((str(src), str(sid)))

    valid_kinds = getattr(engram, "KINDS", {"strategy", "procedure", "fact", "error"})
    egs = []
    for r in engram_records:
        kind = str(r.get("kind", "fact"))
        if kind not in valid_kinds:
            kind = "fact"
        body = r.get("body")
        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        body = body.strip()
        if not body:
            continue
        src_id = _heirloom_src_id(kind, body)
        if (_HEIRLOOM_SRC, src_id) in seen:
            continue
        egs.append(Engram(
            kind=kind,
            body=body,
            provenance="inherited",             # a heirloom IS the inherited letter from a prior self
            strength=INHERITED_STRENGTH_FLOOR,  # the told/inherited discount (seeded above neutral,
                                                # but §5 decays it faster until THIS body verifies it)
            stats={"src": _HEIRLOOM_SRC, "src_id": src_id},
        ))
        seen.add((_HEIRLOOM_SRC, src_id))
    return consolidator.commit_many(egs)


def _import_heirloom_reflexes(cfg, reflex_records: list, header: dict) -> int:
    """Write inherited reflexes to state/reflexes.json as status `proposed`, NEVER armed (WIS1
    across generations). Idempotent: a reflex already present (by id) is not re-added. Carries the
    ancestor's provenance so the operator can see what came from where. Best-effort — a write
    failure warns but never crashes the reseed."""
    if not reflex_records:
        return 0
    state_dir = cfg.state_dir
    path = state_dir / "reflexes.json"

    # Load any existing registry (defensive to stream A's shape: bare list or {"reflexes": [...]}).
    existing_list = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            existing_list = list(raw.get("reflexes", []))
        elif isinstance(raw, list):
            existing_list = list(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        existing_list = []

    have_ids = {r.get("id") for r in existing_list if isinstance(r, dict) and r.get("id")}
    ancestor = (header or {}).get("creature", "")
    added = 0
    for r in reflex_records:
        body = r.get("body")
        if not isinstance(body, dict):
            continue
        rid = body.get("id")
        if rid and rid in have_ids:
            continue
        reflex = dict(body)
        reflex["status"] = "proposed"        # DISARMED — a new body re-earns its own automation (WIS1)
        # Zero out the ancestor's firing counters — this body has fired it zero times.
        reflex["fired_count"] = 0
        reflex["failed_count"] = 0
        reflex["inherited_from"] = ancestor  # provenance: whose reflex this was
        existing_list.append(reflex)
        if rid:
            have_ids.add(rid)
        added += 1

    if not added:
        return 0
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"reflexes": existing_list}, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"  ! heirloom reflex import failed to write {path}: {e}")
        return 0
    return added


def main():
    no_heirloom = "--no-heirloom" in sys.argv[1:]

    cfg = load_config(os.path.join(KDIR, "config.toml"))
    # re-read fresh so edits take effect immediately; local (gitignored, secrets) seeds too
    nuggets = load_nuggets() + load_nuggets(LOCAL_PATH, optional=True)
    n = 0
    for cat, tags, content in nuggets:
        try:
            # Mark as a bootstrap seed so the context layer can tell seeds (rarely need surfacing)
            # apart from LEARNED facts (the agent's own discoveries, always surfaced in the world model).
            knowledge.store_entry(cfg, content, tags, cat, source_goal="seed")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! failed ({cat} {tags}): {e}")
    print(f"seeded {n}/{len(nuggets)} knowledge nuggets into {cfg.knowledge_dir}")

    # Inherit from the previous creature's heirloom volume, unless told not to (WISDOM_PLAN §6).
    if no_heirloom:
        print("  (--no-heirloom: skipping lineage inheritance)")
        return
    try:
        r = import_heirloom(cfg)
    except Exception as e:  # noqa: BLE001 - inheritance is best-effort; a reseed must never crash on it
        print(f"  ! heirloom inheritance failed (continuing): {e}")
        return
    if r["volume"]:
        print(f"inherited from {r['volume']}: {r['engrams']} engrams (provenance=inherited), "
              f"{r['reflexes']} reflex proposals (disarmed)")
    else:
        print("  (no heirloom volume on the shelf — starting from nuggets only)")


if __name__ == "__main__":
    main()
