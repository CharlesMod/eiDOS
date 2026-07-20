"""world.py — a truthful world for the creature to inhabit (WORLD_PLAN W0).

The world is a RENDERING LAYER OVER REALITY: a mind map, not a fantasy. Every place, object, exit,
and weather line maps to something real in the machine (WORLD_PLAN §0 doctrine). This module holds
NO world-only mutable state except the creature's position (W1) — the graph is DERIVED from the real
stores on every `build_world`. When a referent dies, its entity disappears honestly (W4: no blank
rooms). Every affordance is a REAL registered tool name (W2). Locked doors name their real unlock
condition, read from the unlocks ladder (W6). The render states facts, never feelings (W9).

The typed graph (§2) is canonical; `render_here`/`to_json` are pure VIEWS over it (W3). This file is
the W0 deliverable: schema, derivation, position store, render, flag, bounded fail-open reads.

Design contract (WORLD_PLAN §2) — dataclasses, place ids, function signatures, and JSON shape are
BINDING; two other agents (W1 wiring, W3 dashboard map) build against them. Where the plan is silent
(naming rules, exact state strings, weather wording) this module takes taste latitude; where it
speaks (ids, signatures, bounds) it has none.

Everything here is fail-open (WORLD_PLAN §1/§8): a missing or corrupt store means its place is absent
THIS build (never a crash, never a blank room). Reads are bounded (W8): heavy sources are summarized
counts, no rglob storms, service reachability is a fast TCP connect with a short timeout (never a
hang).
"""
from __future__ import annotations

import json
import os
import random
import socket
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------------------------------
# W8 bounds — the whole world is small on purpose (a mind map, not an encyclopedia).
# ---------------------------------------------------------------------------------------------------
MAX_PLACES = 12
MAX_OBJECTS_PER_PLACE = 8
MAX_NOTICES_PER_PLACE = 3
RENDER_BUDGET_CHARS = 900

POSITION_STATE_NAME = "world_position.json"   # under config.state_dir (skeleton, not creature-readable)
DEFAULT_PLACE = "the_commons"                 # the hub; fail-open home for position (§5)

# Service reachability probe budget — a fast connect, never a hang (W8, ARCHITECTURE #1: an event/
# probe, not a delay). One short timeout per gate; the whole gatehouse derive is bounded by it.
_PROBE_TIMEOUT_S = 0.4


# ===================================================================================================
# §2 — the canonical graph (BINDING CONTRACT). All dataclasses are JSON-serializable via to_json().
# ===================================================================================================
@dataclass
class Referent:
    kind: str   # "unit" | "service" | "store" | "dir" | "objective" | "skill" | "quest"
                # | "commission" | "sensor" | "system"
    key: str    # unit id, service name, store path, objective id, ...


@dataclass
class WorldObject:
    id: str
    name: str                                        # seeded flavor name (stable per germline; §3)
    referent: Referent
    state: str                                       # short REAL state ("healthy", "trusted 12/12")
    detail: str = ""                                 # one line, real facts only
    affordances: list[str] = field(default_factory=list)   # REAL tool names (W2)


@dataclass
class Exit:
    to: str                                          # place id
    open: bool
    locked_reason: str = ""                          # W6: the real unlock condition when open=False


@dataclass
class Place:
    id: str                                          # stable snake_case ("workshop", "the_commons")
    name: str
    kind: str                                        # "hub" | "district" | "plot"
    referent: Referent
    objects: list[WorldObject]
    exits: list[Exit]
    notices: list[str] = field(default_factory=list)   # real events, salience-fed (W2 phase)


@dataclass
class World:
    places: dict[str, Place]
    here: str                                        # current place id
    weather: str                                     # derived: metabolism/solar + sleep pressure
    generated_tick: int


# ===================================================================================================
# The fixed v1 topology (§2 table). ids and referents are FIXED; contents are derived; a district is
# ABSENT when its referent is (W4). Each district maps to an unlocks UNIT (or a store/dir/system).
# ---------------------------------------------------------------------------------------------------
# For unit-backed districts, the unit id is how W6 reads the real unlock condition from the ladder and
# how the_commons decides whether the exit is open (granted) or locked.
# ===================================================================================================
_DISTRICT_UNIT = {          # place id -> unlocks unit id (for grant-gated exits + W6 locked reasons)
    "workshop": "skillcraft",
    "watchtower": "foresight",
    "fields": "resolve",
    "gatehouse": "senses",
    "the_barn": "commission",
}

# Canonical place order for rendering the hub's exits and for to_json determinism.
_PLACE_ORDER = (
    "the_commons", "workshop", "library", "watchtower", "fields",
    "gatehouse", "the_barn", "the_spire", "the_porch", "your_plot",
)


# ===================================================================================================
# Flavor naming (§3) — drawn ONCE from the germline seed, morph-lexicon aware, DETERMINISTIC. Names
# are flavor ONLY: ids/referents/states are real and fixed. No LLM authors world text. Stable across
# builds for the same seed. Fail-open to neutral names when no genome exists.
# ===================================================================================================
# Neutral fallbacks — the plain-English place names (used when there is no germline seed, and as the
# base each morph tints). Kept human and honest; a place is a place.
_NEUTRAL_PLACE_NAMES = {
    "the_commons": "the Commons",
    "workshop": "the Workshop",
    "library": "the Library",
    "watchtower": "the Watchtower",
    "fields": "the Fields",
    "gatehouse": "the Gatehouse",
    "the_barn": "the Barn",
    "the_spire": "the Spire",
    "the_porch": "the Porch",
    "your_plot": "your Plot",
}

# Per-morph tints for a couple of places whose name reads differently for a moth than an otter (§3:
# "an otter's world reads differently from a moth's"). We keep this small and honest — a tint, not a
# parallel fiction (§8). Missing morph / missing key → the neutral name.
_MORPH_PLACE_TINT = {
    "burrower": {"your_plot": "your Burrow", "the_commons": "the Warren"},
    "corvid":   {"your_plot": "your Nest", "the_commons": "the Rookery"},
    "otter":    {"your_plot": "your Holt", "the_commons": "the Bank"},
    "moth":     {"your_plot": "your Cocoon", "the_commons": "the Glade"},
}


def _load_germline(config) -> tuple[Optional[int], str]:
    """(seed, morph) from the creature's genome, fail-open. No genome → (None, DEFAULT_MORPH) so
    naming falls back to neutral. Never raises."""
    try:
        import genome
        g = genome.Genome.load(config)
        if g is None or getattr(g, "seed", None) is None:
            return None, getattr(genome, "DEFAULT_MORPH", "burrower")
        return int(g.seed), str(getattr(g, "morph", None) or genome.DEFAULT_MORPH)
    except Exception:  # noqa: BLE001 — a broken genome read is neutral names, never a crash
        return None, "burrower"


def _place_name(place_id: str, seed: Optional[int], morph: str) -> str:
    """The stable flavor name for a place. Deterministic in (place_id, seed, morph). No seed → the
    neutral name. With a seed we apply the morph tint (a stable per-morph choice), so the same
    germline always reads the same and a moth's world differs from an otter's (§3)."""
    neutral = _NEUTRAL_PLACE_NAMES.get(place_id, place_id)
    if seed is None:
        return neutral
    tint = _MORPH_PLACE_TINT.get(morph, {})
    return tint.get(place_id, neutral)


def _object_namer(seed: Optional[int], morph: str) -> Callable[[str, str], str]:
    """Return a deterministic namer(object_kind, fallback) for world objects. Draws are frozen off a
    Random seeded by (seed, object_kind) so ordering of derivation never shifts a name (frozen-draw
    discipline, §3). No seed → the plain fallback. Morph-aware via the lexicon where it reads well."""
    if seed is None:
        return lambda kind, fallback: fallback
    try:
        import genome
        lex = genome.MORPHS.get(morph, {}).get("lexicon", {})
    except Exception:  # noqa: BLE001
        lex = {}

    def namer(kind: str, fallback: str) -> str:
        # Per-object stable RNG: same (germline, object kind) → same tint every build. Seed with a
        # STRING (a tuple is not a valid random seed type) so the draw is deterministic and portable.
        rng = random.Random(f"{int(seed)}:{kind}")
        # A tiny, honest lexicon flourish for a couple of object kinds; otherwise the plain name.
        if kind == "home_files" and lex.get("home"):
            return f"the {lex['home']}"
        if kind == "coat" and lex.get("coat"):
            return lex["coat"]
        # Neutral flourish: a stable adjective drawn from a fixed pool (deterministic, no LLM).
        pool = ("standing", "quiet", "worn", "steady", "open")
        return f"the {rng.choice(pool)} {fallback}"

    return namer


# ===================================================================================================
# Bounded, fail-open store reads. Each returns a list[WorldObject] (possibly empty). An empty list +
# a "referent present" signal → the place still renders (it exists, just quiet); a hard read failure
# or a truly-absent referent → the place is dropped entirely (W4). We separate "absent" (None) from
# "present but empty" ([]).
# ===================================================================================================
def _sorted_head(items: list, n: int) -> list:
    return list(items)[:n]


def _derive_workshop(config, namer) -> Optional[list[WorldObject]]:
    """skillcraft district — skills from the manifest (status, uses). Absent if the manifest can't be
    read at all. Present-but-empty renders as a quiet workshop."""
    try:
        import skills
        manifest = skills._load_manifest(config) or {}
        entries = (manifest.get("skills") or {})
    except Exception:  # noqa: BLE001
        return None
    live = [(name, e) for name, e in entries.items()
            if isinstance(e, dict) and e.get("status") in ("active", "trusted") and e.get("enabled", True)]
    # Newest-touched first (updated/last_used), bounded by W8.
    live.sort(key=lambda ne: str(ne[1].get("last_used") or ne[1].get("updated") or ""), reverse=True)
    objs: list[WorldObject] = []
    for name, e in _sorted_head(live, MAX_OBJECTS_PER_PLACE):
        inv = int(e.get("invocations", 0) or 0)
        succ = int(e.get("successes", 0) or 0)
        status = str(e.get("status", "active"))
        state = f"{status}, {succ}/{inv} ok" if inv else status
        desc_lines = str(e.get("description") or "").strip().splitlines()
        detail = desc_lines[0][:80] if desc_lines else ""
        objs.append(WorldObject(
            id=f"skill_{name}",
            name=namer("skill", name),
            referent=Referent("skill", name),
            state=state,
            detail=detail,
            affordances=["edit_skill", "rollback_skill", "list_skills"],
        ))
    return objs


def _derive_library(config, namer) -> Optional[list[WorldObject]]:
    """knowledge + engrams district — shelf counts, newest entries (bounded). Absent only if both
    reads fail. Each shelf is one object (a count), plus the newest few knowledge entries."""
    objs: list[WorldObject] = []
    read_any = False
    # Knowledge shelves (counts) + newest.
    try:
        import knowledge
        total = int(knowledge.count_entries(config))
        read_any = True
        objs.append(WorldObject(
            id="shelf_knowledge",
            name=namer("shelf", "knowledge shelf"),
            referent=Referent("store", "knowledge"),
            state=f"{total} entries",
            detail="facts, procedures, errors, reflections",
            affordances=["memorize", "recall"],
        ))
        for e in _sorted_head(knowledge.recent_learned(config, limit=3), 3):
            cat = str(e.get("category", "fact"))
            prev = str(e.get("content_preview") or "").strip().replace("\n", " ")[:60]
            objs.append(WorldObject(
                id=f"know_{e.get('id', '?')}",
                name=namer("entry", f"{cat} entry"),
                referent=Referent("store", f"knowledge:{e.get('id', '?')}"),
                state=f"newest {cat}",
                detail=prev,
                affordances=["recall"],
            ))
    except Exception:  # noqa: BLE001
        pass
    # Engram long-term shelf (count).
    try:
        import engram
        store = engram.LongTermStore(config)
        n = len(store)
        read_any = True
        objs.append(WorldObject(
            id="shelf_engrams",
            name=namer("shelf", "engram shelf"),
            referent=Referent("store", "engram_longterm"),
            state=f"{n} engrams",
            detail="consolidated long-term memory",
            affordances=["recall"],
        ))
    except Exception:  # noqa: BLE001
        pass
    if not read_any:
        return None
    return _sorted_head(objs, MAX_OBJECTS_PER_PLACE)


def _derive_watchtower(config, namer) -> Optional[list[WorldObject]]:
    """foresight district — open predictions (target, deadline, confidence). Absent if the ledger
    can't be constructed. Present-but-empty renders as a quiet watchtower."""
    try:
        import expectations
        ledger = expectations.ExpectationLedger(config)
        opens = ledger.open_predictions()
    except Exception:  # noqa: BLE001
        return None
    objs: list[WorldObject] = []
    for p in _sorted_head(opens, MAX_OBJECTS_PER_PLACE):
        target = str(getattr(p, "target", "") or getattr(p, "statement", ""))[:60]
        conf = float(getattr(p, "confidence", 0.0) or 0.0)
        deadline_ts = getattr(p, "deadline", None)
        when = _fmt_epoch(deadline_ts)
        objs.append(WorldObject(
            id=f"pred_{getattr(p, 'id', '?')}",
            name=namer("prediction", "a wager"),
            referent=Referent("system", f"prediction:{getattr(p, 'id', '?')}"),
            state=f"open, conf {conf:.0%}",
            detail=f"{target} — by {when}" if when else target,
            affordances=["predict"],
        ))
    return objs


def _derive_fields(config, namer) -> Optional[list[WorldObject]]:
    """resolve district — objectives as crops; health from frustration/progress. Absent if the store
    can't be read. Present-but-empty renders as fallow fields."""
    try:
        import objectives
        objs_data = objectives.list_objectives(config)
    except Exception:  # noqa: BLE001
        return None
    live = [o for o in objs_data if o.get("state") in ("active", "blocked")]
    live.sort(key=lambda o: int(o.get("priority", 5) or 5))
    out: list[WorldObject] = []
    for o in _sorted_head(live, MAX_OBJECTS_PER_PLACE):
        frustration = int(o.get("frustration", 0) or 0)
        stall = int(o.get("ticks_since_progress", 0) or 0)
        st = str(o.get("state", "active"))
        # Crop health from real stall/frustration — facts, not feelings (W9).
        if st == "blocked":
            health = "fallow (parked)"
        elif frustration >= 3 or stall >= 5:
            health = f"wilting ({stall} ticks since progress)"
        else:
            health = "growing"
        out.append(WorldObject(
            id=f"obj_{o.get('id', '?')}",
            name=namer("objective", str(o.get("title", "an objective"))[:40]),
            referent=Referent("objective", str(o.get("id", "?"))),
            state=health,
            detail=str(o.get("why", "") or "")[:70],
            affordances=["objective_done", "objective_block", "objective_list"],
        ))
    return out


def _derive_gatehouse(config, namer) -> Optional[list[WorldObject]]:
    """senses/net district — services as gates with real up/down (a fast TCP probe, never a hang).
    The gate services are the host stack from RUNTIME_SPRINTER (llama-swap, embed, dashboard). Always
    present (the services are the machine itself); each gate's state is a live probe."""
    gates = [
        ("gate_llm", "llama-swap", "127.0.0.1", 8080),
        ("gate_embed", "eidos-embed", "127.0.0.1", 8082),
        ("gate_dashboard", "dashboard", "127.0.0.1", 8099),
    ]
    objs: list[WorldObject] = []
    for gid, svc, host, port in gates:
        up = _tcp_reachable(host, port)
        objs.append(WorldObject(
            id=gid,
            name=namer("gate", f"the {svc} gate"),
            referent=Referent("service", svc),
            state="open" if up else "shut",
            detail=f"{host}:{port} — {'reachable' if up else 'unreachable'}",
            affordances=["tcp_probe", "http_probe"],
        ))
    return _sorted_head(objs, MAX_OBJECTS_PER_PLACE)


def _derive_barn(config, namer) -> Optional[list[WorldObject]]:
    """commission district — brief head + open/confirmed tasks. Absent if the commission module can't
    be read. Present-but-empty renders as a quiet barn."""
    try:
        import commission
        comm = commission.Commission(config)
        tasks = comm.load()
    except Exception:  # noqa: BLE001
        return None
    objs: list[WorldObject] = []
    # Brief head as the first object (real facts only).
    try:
        brief = commission.load_brief(config)
        if brief and str(brief).strip():
            head = str(brief).strip().replace("\n", " ")[:80]
            objs.append(WorldObject(
                id="commission_brief",
                name=namer("brief", "the standing order"),
                referent=Referent("commission", "brief"),
                state="posted",
                detail=head,
                affordances=["commission_add", "commission_done"],
            ))
    except Exception:  # noqa: BLE001
        pass
    live = [t for t in tasks if str(getattr(t, "state", "")).lower() in ("open", "done_claimed", "confirmed")]
    for t in _sorted_head(live, MAX_OBJECTS_PER_PLACE - len(objs)):
        st = str(getattr(t, "state", "open")).lower()
        title = str(getattr(t, "title", "a task"))[:50]
        objs.append(WorldObject(
            id=f"task_{getattr(t, 'id', '?')}",
            name=namer("task", title),
            referent=Referent("commission", f"task:{getattr(t, 'id', '?')}"),
            state=st,
            detail=str(getattr(t, "detail", "") or "")[:70],
            affordances=["commission_done", "weigh_options"],
        ))
    return objs


def _derive_spire(config, namer) -> Optional[list[WorldObject]]:
    """quests district — active quest, cadence, System standing. Absent if the quest store can't be
    read. Present-but-empty renders as a silent spire."""
    try:
        import quests
        store = quests.QuestStore(config)
        active = store.active()
        passed = int(store.passed_count())
    except Exception:  # noqa: BLE001
        return None
    objs: list[WorldObject] = [
        WorldObject(
            id="system_standing",
            name=namer("standing", "the System's ledger"),
            referent=Referent("quest", "standing"),
            state=f"{passed} passed",
            detail="quests adjudicated PASS, lifetime",
            affordances=[],
        )
    ]
    if active is not None:
        directive = str(getattr(active, "directive", "an active quest"))[:70]
        tier = int(getattr(active, "tier", 1) or 1)
        objs.append(WorldObject(
            id=f"quest_{getattr(active, 'id', '?')}",
            name=namer("quest", "the active quest"),
            referent=Referent("quest", str(getattr(active, "id", "?"))),
            state=f"active, tier {tier}",
            detail=directive,
            affordances=[],
        ))
    return _sorted_head(objs, MAX_OBJECTS_PER_PLACE)


def _derive_porch(config, namer) -> Optional[list[WorldObject]]:
    """news district — queued news, operator presence state. Absent if the news queue can't be read.
    Present-but-empty renders as a quiet porch."""
    try:
        import news
        nq = news.NewsQueue(config)
        items = nq.items()
    except Exception:  # noqa: BLE001
        return None
    objs: list[WorldObject] = []
    for it in _sorted_head(items, MAX_OBJECTS_PER_PLACE):
        body = str(getattr(it, "body", "") or "").replace("\n", " ")[:70]
        src = str(getattr(it, "source", "news"))
        surfaced = getattr(it, "surfaced_ts", None) is not None
        objs.append(WorldObject(
            id=f"news_{getattr(it, 'id', '?')}",
            name=namer("news", f"a {src} note"),
            referent=Referent("store", f"news:{getattr(it, 'id', '?')}"),
            state="delivered" if surfaced else "queued",
            detail=body,
            affordances=["message"],
        ))
    return objs


def _derive_plot(config, namer) -> Optional[list[WorldObject]]:
    """the creature's own home dir (workspace/home) — its own files (count, newest). Bounded: a single
    non-recursive listdir, count + newest three. Absent only if the home dir cannot be read."""
    try:
        home = Path(config.workspace_dir) / "home"
        if not home.exists():
            # A creature that hasn't made anything yet still HAS a plot (its home is real); render it
            # empty rather than dropping it — but only when the workspace itself is real.
            if not Path(config.workspace_dir).exists():
                return None
            return []
        entries = [p for p in home.iterdir()]
    except Exception:  # noqa: BLE001
        return None
    files = [p for p in entries if p.is_file()]
    count = len(files)
    objs: list[WorldObject] = [
        WorldObject(
            id="plot_summary",
            name=namer("home_files", "your things"),
            referent=Referent("dir", "home"),
            state=f"{count} files",
            detail="everything you have made",
            affordances=["read_file", "write_file", "bash"],
        )
    ]
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:  # noqa: BLE001
        pass
    for p in _sorted_head(files, MAX_OBJECTS_PER_PLACE - 1):
        objs.append(WorldObject(
            id=f"file_{p.name}",
            name=p.name,
            referent=Referent("dir", f"home/{p.name}"),
            state="yours",
            detail="a file you made",
            affordances=["read_file", "write_file"],
        ))
    return _sorted_head(objs, MAX_OBJECTS_PER_PLACE)


# The commons is the hub: its "objects" are a standing line (level/portfolio) drawn from persona.
def _derive_commons(config, namer) -> list[WorldObject]:
    """system:workspace hub — the standing line (level, xp). Always present (the hub anchors the map).
    Fail-open to a bare standing object if persona can't be read."""
    level, xp = 1, 0
    try:
        import persona
        p = persona.load_persona(config.workspace)
        level = int(p.get("level", 1) or 1)
        xp = int(p.get("xp", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return [WorldObject(
        id="standing",
        name=namer("standing", "the standing stone"),
        referent=Referent("system", "persona"),
        state=f"level {level}",
        detail=f"{xp} XP",
        affordances=["check_system", "check_tools"],
    )]


# Derivation table: place id -> (kind, referent, deriver). the_commons/library/the_spire/the_porch/
# your_plot are non-unit-backed; the five in _DISTRICT_UNIT are unit-gated.
_DERIVERS: dict[str, tuple[str, Referent, Callable]] = {
    "the_commons": ("hub", Referent("system", "workspace"), _derive_commons),
    "workshop": ("district", Referent("unit", "skillcraft"), _derive_workshop),
    "library": ("district", Referent("store", "knowledge+engrams"), _derive_library),
    "watchtower": ("district", Referent("unit", "foresight"), _derive_watchtower),
    "fields": ("district", Referent("unit", "resolve"), _derive_fields),
    "gatehouse": ("district", Referent("unit", "senses"), _derive_gatehouse),
    "the_barn": ("district", Referent("commission", "commission"), _derive_barn),
    "the_spire": ("district", Referent("quest", "quests"), _derive_spire),
    "the_porch": ("district", Referent("store", "news"), _derive_porch),
    "your_plot": ("plot", Referent("dir", "workspace_home"), _derive_plot),
}


# ===================================================================================================
# W6 — locked doors are honest: read the real unlock condition from the unlocks ladder.
# ===================================================================================================
def _granted_units(config) -> set[str]:
    """The set of unlocks units currently granted, fail-open to empty. A district whose unit is NOT
    here is behind a locked exit from the_commons (W6)."""
    try:
        import unlocks
        state = unlocks.UnlockState(config)
        return set(state.granted.keys())
    except Exception:  # noqa: BLE001
        return set()


def _criterion_text(crit) -> str:
    """Human-readable unlock condition from a quests.Criterion, using the ladder's own vocabulary
    (quests.ADJUDICATABLE_PATHS). Walks all_of/any_of. Never invents mystery text (W6)."""
    try:
        import quests
        paths = getattr(quests, "ADJUDICATABLE_PATHS", {})
    except Exception:  # noqa: BLE001
        paths = {}

    def leaf(c) -> str:
        human = paths.get(getattr(c, "path", ""), getattr(c, "path", "") or "a condition")
        op = getattr(c, "op", ">=")
        val = getattr(c, "value", None)
        return f"{human} {op} {val}"

    if crit is None:
        return ""
    all_of = getattr(crit, "all_of", None)
    any_of = getattr(crit, "any_of", None)
    if all_of:
        return " and ".join(_criterion_text(c) for c in all_of)
    if any_of:
        return " or ".join(_criterion_text(c) for c in any_of)
    return leaf(crit)


def _locked_reason(unit_id: str) -> str:
    """The real, honest unlock condition for a not-yet-granted unit (W6). Prefers the criterion text;
    for issuance-granted units (criterion=None) names the granting seam via the announce line. Never
    empty for a real unit."""
    try:
        import unlocks
        u = unlocks.unit(unit_id)
    except Exception:  # noqa: BLE001
        u = None
    if u is None:
        return f"unit '{unit_id}' not yet granted"
    crit = getattr(u, "criterion", None)
    if crit is not None:
        cond = _criterion_text(crit)
        svc = getattr(u, "requires_service", None)
        if svc:
            cond = f"{cond} (and the {svc} service must answer)" if cond else f"the {svc} service must answer"
        if cond:
            return f"needs {cond}"
    # Issuance-granted unit: the System's window is the moment (no milestone). Name the tools it grants
    # so the door is still learnable (ARCH #4), not a mystery.
    tools = ", ".join(getattr(u, "tools", ()) or ())
    return f"granted by the System when it issues this unit (unlocks: {tools})" if tools else \
        f"granted by the System (unit '{unit_id}')"


# ===================================================================================================
# Weather (§2) — metabolism reserve + solar charge + sleep pressure, one honest line. Facts, never
# feelings (W9). Fail-open: any unreadable source is simply omitted from the line.
# ===================================================================================================
def _fmt_epoch(ts) -> str:
    try:
        if ts is None:
            return ""
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return ""


def _derive_weather(config) -> str:
    parts: list[str] = []
    # Metabolism reserve.
    try:
        from nervous.metabolism import Metabolism
        m = Metabolism(config=config)
        energy = m.snapshot().get("energy")
        if energy is not None:
            parts.append(f"reserve {float(energy):.0%}")
    except Exception:  # noqa: BLE001
        pass
    # Solar charge (environmental, time-of-day; the plant's daylight curve).
    try:
        if getattr(config, "nervous_metabolism_solar_enabled", True):
            from nervous.metabolism import solar_charge_in
            now = datetime.now()
            charge = solar_charge_in(now.hour + now.minute / 60.0)
            if charge and float(charge) > 0:
                parts.append("charging in daylight")
            else:
                parts.append("no solar charge (dark)")
    except Exception:  # noqa: BLE001
        pass
    # Sleep pressure (adenosine).
    try:
        from nervous.neuromod import Adenosine
        a = Adenosine(max_wake_hours=float(getattr(config, "pillars_max_wake_hours", 18.0) or 18.0),
                      config=config)
        pressure = a.pressure()
        parts.append(f"sleep pressure {float(pressure):.0%}")
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        return "the weather is quiet"
    return "; ".join(parts)


# ===================================================================================================
# Service reachability — a fast TCP connect with a short timeout, NEVER a hang (W8, ARCH #1).
# ===================================================================================================
def _tcp_reachable(host: str, port: int, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 — refused/timeout/resolve-fail all mean "shut"
        return False


# ===================================================================================================
# Public API (§2 exact signatures).
# ===================================================================================================
def world_enabled(config) -> bool:
    """The one flag check (W7). Off (default) → byte-identical: no context block, no `go`, no writes.
    Read via getattr so a bare/partial Config never raises."""
    return bool(getattr(config, "world_enabled", False))


def build_world(config, *, persona: Optional[dict] = None, tick: int = 0) -> World:
    """Derive the whole world from real state (W1). A place is included only if its deriver returns a
    non-None object list (W4: absent referent → absent place). the_commons is always present (hub).
    Locked districts (unit not granted) appear as locked exits from the_commons (W6). Bounded (W8)."""
    seed, morph = _load_germline(config)
    namer = _object_namer(seed, morph)
    granted = _granted_units(config)

    places: dict[str, Place] = {}
    for pid in _PLACE_ORDER:
        kind, referent, deriver = _DERIVERS[pid]
        try:
            objs = deriver(config, namer)
        except Exception:  # noqa: BLE001 — a deriver that raises drops its place, never crashes the build
            objs = None
        if objs is None:
            continue  # W4: no referent, no room
        # A unit-gated district whose unit is NOT granted is not a walkable place — it is a LOCKED
        # exit from the hub (handled below), so we skip building it as a standalone place.
        unit_id = _DISTRICT_UNIT.get(pid)
        if unit_id is not None and unit_id not in granted:
            continue
        objs = objs[:MAX_OBJECTS_PER_PLACE]  # W8 belt-and-braces
        places[pid] = Place(
            id=pid,
            name=_place_name(pid, seed, morph),
            kind=kind,
            referent=referent,
            objects=objs,
            exits=[],  # filled below once we know which places exist
            notices=[],
        )

    # Exits: every real place is reachable from the_commons and back (a simple hub topology, v1). The
    # hub also carries LOCKED exits to unit-gated districts that aren't granted yet (W6) — you can walk
    # up to the door and read the real condition.
    commons = places.get("the_commons")
    if commons is not None:
        for pid in _PLACE_ORDER:
            if pid == "the_commons":
                continue
            if pid in places:
                commons.exits.append(Exit(to=pid, open=True))
                # Back-edge home from each district/plot.
                places[pid].exits.append(Exit(to="the_commons", open=True))
            else:
                unit_id = _DISTRICT_UNIT.get(pid)
                if unit_id is not None and unit_id not in granted:
                    # A locked door: the district's referent isn't granted. Name the real condition.
                    commons.exits.append(Exit(to=pid, open=False, locked_reason=_locked_reason(unit_id)))

    # W8: cap total places (the fixed topology is already ≤ 10, but enforce the invariant).
    if len(places) > MAX_PLACES:
        keep = {pid for pid in _PLACE_ORDER[:MAX_PLACES]}
        places = {k: v for k, v in places.items() if k in keep}

    here = current_place(config)
    if here not in places:
        here = "the_commons" if "the_commons" in places else (next(iter(places), DEFAULT_PLACE))

    return World(
        places=places,
        here=here,
        weather=_derive_weather(config),
        generated_tick=int(tick),
    )


# ---------------------------------------------------------------------------------------------------
# Position store — state/world_position.json, atomic write, fail-open to the_commons (§5).
# ---------------------------------------------------------------------------------------------------
def _position_path(config) -> Path:
    return Path(config.state_dir) / POSITION_STATE_NAME


def current_place(config) -> str:
    """The persisted current place id, fail-open to the_commons. Missing/corrupt file → the hub (§5)."""
    try:
        d = json.loads(_position_path(config).read_text(encoding="utf-8"))
        here = d.get("here")
        if isinstance(here, str) and here:
            return here
    except Exception:  # noqa: BLE001 — missing/corrupt → the hub, never a crash
        pass
    return DEFAULT_PLACE


def _persist_place(config, place_id: str) -> None:
    """Atomically persist position (atomicio/UnlockState pattern: unique temp + replace). Best-effort
    (a failed write is a lost move, never a crash)."""
    tmpname = None
    try:
        state_dir = Path(config.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmpname = tempfile.mkstemp(dir=str(state_dir), prefix=".world_position-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"here": str(place_id)}, f, ensure_ascii=False)
        os.replace(tmpname, _position_path(config))
    except Exception:  # noqa: BLE001 — best-effort persistence
        if tmpname is not None:
            try:
                os.unlink(tmpname)
            except Exception:  # noqa: BLE001
                pass


def move_to(config, place_id: str) -> tuple[bool, str]:
    """Mechanically adjudicate a move (§5, ARCH #4 — the wall is learnable):
      · unknown place → (False, a message listing the real place ids),
      · locked place  → (False, a message naming the real unlock condition),
      · success       → persist atomically, return (True, an arrival line).
    Position is the sole world-only mutable state (W1); the graph is otherwise pure derivation."""
    place_id = str(place_id or "").strip()
    world = build_world(config)
    if place_id in world.places:
        _persist_place(config, place_id)
        p = world.places[place_id]
        return True, f"You are at {p.name}."
    # Not a walkable place. Is it a known-but-LOCKED district? (a locked exit from the hub)
    commons = world.places.get("the_commons")
    if commons is not None:
        for ex in commons.exits:
            if ex.to == place_id and not ex.open:
                return False, (f"'{place_id}' is locked: {ex.locked_reason}. "
                               f"You can walk up to the door, but not through it yet.")
    # Truly unknown → list the real place ids (open exits + any locked doors), so the wall is learnable.
    open_ids = sorted(world.places.keys())
    locked_ids = sorted(ex.to for ex in (commons.exits if commons else []) if not ex.open)
    listing = ", ".join(open_ids)
    if locked_ids:
        listing += f" (locked: {', '.join(locked_ids)})"
    return False, f"There is no place called '{place_id}'. You can go to: {listing}."


# ===================================================================================================
# Rendering (§4) — the "## Where you are" block. A pure VIEW over the graph (W3). States facts, never
# feelings (W9); never says "you should" — it says "here is". Budget ≤ 900 chars (W8).
# ===================================================================================================
def render_here(world: World, *, budget_chars: int = RENDER_BUDGET_CHARS) -> str:
    """The proprioception block for the current place. Bounded to budget_chars (W8) by trimming from
    the least-load-bearing tail (notices, then objects), never mid-fact."""
    here = world.places.get(world.here)
    if here is None:
        # Fail-open: an empty/degenerate world still renders honestly.
        return "## Where you are\n(the world is quiet)"

    lines: list[str] = ["## Where you are", f"You are at {here.name}."]

    # Objects with real states.
    obj_lines: list[str] = []
    for o in here.objects[:MAX_OBJECTS_PER_PLACE]:
        aff = f"  [{', '.join(o.affordances)}]" if o.affordances else ""
        detail = f" — {o.detail}" if o.detail else ""
        obj_lines.append(f"- {o.name}: {o.state}{detail}{aff}")

    # Open exits + locked doors (with real reasons — W6).
    exit_lines: list[str] = []
    open_exits = [ex.to for ex in here.exits if ex.open]
    if open_exits:
        exit_lines.append("Exits: " + ", ".join(sorted(open_exits)))
    for ex in here.exits:
        if not ex.open:
            exit_lines.append(f"Locked: {ex.to} — {ex.locked_reason}")

    weather_line = f"Weather: {world.weather}" if world.weather else ""
    notice_lines = [f"Notice: {n}" for n in here.notices[:MAX_NOTICES_PER_PLACE]]

    # Assemble in priority order, then trim the tail to fit the budget. Header + place + weather +
    # exits are load-bearing; objects then notices are trimmed first if over budget.
    body = list(lines)
    if weather_line:
        body.append(weather_line)
    body.extend(exit_lines)
    body.extend(obj_lines)
    body.extend(notice_lines)

    out = "\n".join(body)
    if len(out) <= budget_chars:
        return out
    # Over budget: drop trailing lines (notices first, then objects) until it fits, keeping the
    # load-bearing head. Never cut mid-line.
    core = lines + ([weather_line] if weather_line else []) + exit_lines
    trimmable = obj_lines + notice_lines
    while trimmable:
        trimmable.pop()  # drop from the tail
        candidate = "\n".join(core + trimmable)
        if len(candidate) <= budget_chars:
            return candidate
    # Even the core is over budget (pathological): hard-truncate honestly at the boundary.
    core_text = "\n".join(core)
    return core_text[:budget_chars]


# ===================================================================================================
# to_json (§2) — for /api/world and future renderers. Pure serialization of the typed graph (W3).
# ===================================================================================================
def to_json(world: World) -> dict:
    """JSON-serializable snapshot of the whole graph. A pure view: no derivation, no state read."""
    return {
        "here": world.here,
        "weather": world.weather,
        "generated_tick": world.generated_tick,
        "places": {pid: asdict(place) for pid, place in world.places.items()},
    }
