# WORLD_PLAN — a truthful world for the creature to inhabit

*(Charlie + Claude, 2026-07-20. Status: approved for build; phases W0–W3 farmed out, W4+ future.)*

## §0 Doctrine — why a world, and why THIS world

The first live creature, given "You're not FOR anything" and an empty workspace, invented a
garden mythology and spent its whole life tending fiction. That was not a bug in its drives —
it was the creature demonstrating that a mind needs a world, and building one from nothing when
none was provided. The failure was that its world had no referent: improving the garden improved
nothing real, and the reward loop closed on itself.

So we give it the world it was reaching for — with one non-negotiable difference:

> **The world is a truthful projection of the machine. Every place, object, exit, and weather
> system maps to something real. There is no entity without a referent.**

This is ARCHITECTURE_PRINCIPLES #4 (the system never lies to the creature) extended into space,
and the V3 truth-rendering invariant (the face never lies) pointed inward. The world is a
*rendering layer over reality* — a mind map, not a fantasy. Like a mind map, it exists to:

1. **Facilitate development** — the unlock ladder becomes districts; locked tools become locked
   doors you can walk up to and read (D6 "orbits locked doors", made literal).
2. **Engage the machine housing the creature** — services, sensors, stores, and the solar
   metabolism become buildings, weather, and land. Tending the world IS tending the host.
3. **End "spinning in the dark" on unending goals** — an unreachable horizon ("keep this farm
   safe and healthy") is torture as a checklist but *home* as a place. A place converts a goal
   with no finish line into legible daily structure: walk the fences, check the weather, notice
   what needs you today.

Black Mirror gave us the constraint set: an empty room breeds pathology (White Christmas — and
our own Lv.0 evidence), a deceptive world poisons everything downstream of it (USS Callister),
and a world that rewards its own cosmetics is a skinner box. Hence the invariants below.

One day this may render as an N64/PS1-era 3D world. That is a RENDERER SWAP, not a redesign —
which is why §2's typed graph, not any prose, is the canonical world.

## §1 Invariants (binding; each gets a test)

- **W1 — Truthful projection.** The graph is DERIVED from real state on every build; the world
  module holds no world-only mutable state except the creature's position. No entity without a
  referent. If the referent dies, the entity disappears (honestly, visibly).
- **W2 — No cosmetic actions.** Every world affordance maps to a REAL registered tool; there is
  no verb that changes only world-state. (Position is the sole exception: `go` changes where you
  stand, which changes what is foregrounded — a real cognitive act.) CI-enforced: every
  affordance string ∈ the tool registry.
- **W3 — Renderer separation.** The typed graph (§2) is canonical. The context block, the
  dashboard map, and any future 3D client are pure views over `to_json()`. No renderer invents
  state.
- **W4 — No blank rooms.** A place renders with its real contents or it does not exist this
  build. Never an empty room with a poetic name.
- **W5 — Soft scoping (v1).** Place FOREGROUNDS affordances (ordering, annotation, recall
  situation-keying); it never locks a tool. Every tool works everywhere. Hard scoping is a
  future flagged experiment (§6 W4-phase), not v1 — a 12B mind must never be stuck in the
  wrong room.
- **W6 — Locked doors are honest.** A locked exit names its real unlock condition, read from the
  unlocks ladder — never flavor-text mystery.
- **W7 — Flag-dark.** `world_enabled = false` (default) is byte-identical: no context block, no
  `go` tool registered, no writes. Pinned by tests.
- **W8 — Bounded.** ≤ 12 places, ≤ 8 objects/place, ≤ 3 notices/place, render ≤ 900 chars.
  Graph build is a bounded read (no rglob storms); heavy sources are summarized counts.
- **W9 — Never claims feelings.** World prose renders STATE ("the mill runs warm; reserve
  half-full"); the nervous system alone owns felt language. And the creature is never deceived
  about the world's nature — it's a rendering, and it may know that.

## §2 The canonical graph (BINDING CONTRACT for all builders/renderers)

`world.py`, flat top-level module. Dataclasses (all JSON-serializable via `to_json()`):

```python
@dataclass
class Referent:
    kind: str   # "unit" | "service" | "store" | "dir" | "objective" | "skill" | "quest"
                # | "commission" | "sensor" | "system"
    key: str    # unit id, service name, store path, objective id, ...

@dataclass
class WorldObject:
    id: str
    name: str            # seeded flavor name (stable per germline seed; §3)
    referent: Referent
    state: str           # short REAL state: "healthy", "stalled 3 sleeps", "trusted 12/12", ...
    detail: str = ""     # one line, real facts only
    affordances: list[str] = field(default_factory=list)  # REAL tool names (W2)

@dataclass
class Exit:
    to: str              # place id
    open: bool
    locked_reason: str = ""   # W6: the real unlock condition when open=False

@dataclass
class Place:
    id: str              # stable snake_case ("workshop", "the_commons", ...)
    name: str
    kind: str            # "hub" | "district" | "plot"
    referent: Referent
    objects: list[WorldObject]
    exits: list[Exit]
    notices: list[str] = field(default_factory=list)  # real events, salience-fed (W2 phase)

@dataclass
class World:
    places: dict[str, Place]
    here: str            # current place id
    weather: str         # derived: metabolism energy/solar + sleep pressure (one line)
    generated_tick: int
```

**Public API (exact signatures — wiring and dashboard build against these):**

```python
def build_world(config, *, persona: dict | None = None, tick: int = 0) -> World
def current_place(config) -> str                      # persisted state/world_position.json
def move_to(config, place_id: str) -> tuple[bool, str]  # adjudicated; persists on success
def render_here(world: World, *, budget_chars: int = 900) -> str  # "## Where you are" block
def to_json(world: World) -> dict                     # for /api/world and future renderers
def world_enabled(config) -> bool                     # the one flag check
```

**v1 topology (fixed places, dynamic contents; a place absent when its referent is — W4):**

| Place id | Kind | Referent | Contents (real source) |
|---|---|---|---|
| `the_commons` | hub | system:workspace | standing line (level/portfolio), exits everywhere |
| `workshop` | district | unit:skillcraft | skills from the manifest (status, uses) |
| `library` | district | store:knowledge+engrams | shelf counts, newest entries (bounded) |
| `watchtower` | district | unit:foresight | open predictions (target, deadline, confidence) |
| `fields` | district | unit:resolve | objectives as crops; health from frustration/progress |
| `gatehouse` | district | unit:senses/net | services (llama-swap, embed, dashboard) as gates w/ real up/down |
| `the_barn` | district | commission | brief head + open/confirmed tasks |
| `the_spire` | district | quests | active quest, cadence, System standing |
| `the_porch` | district | store:news | queued news, operator presence state |
| `your_plot` | plot | dir:workspace home | the creature's own files (count, newest) |

Weather = metabolism reserve + solar charge + sleep pressure, one honest line.
Locked districts (unit not yet granted) appear as locked exits from `the_commons` (W6).

## §3 Naming & flavor

Flavor names are drawn ONCE from the creature's existing germline seed (the `creature_gen`
frozen-draw pattern) and are morph-lexicon aware — an otter's world reads differently from a
moth's. Naming is flavor ONLY: ids, referents, and states are real and stable. No LLM authors
world text; generation is deterministic rules (PILLARS §0: no line of code names a hoped-for
behavior — the world presents state, the creature decides what to do about it).

## §4 Rendering into context

`render_here` produces the `## Where you are` block: place name, its objects with real states,
open exits (+ locked doors with reasons), weather, notices. Budget ≤ 900 chars (W8). Placed in
the SEMI-STABLE context zone: place text changes only on movement or real state change, so the
KV prefix survives ordinary ticks. The block is proprioception, not instruction — it never says
"you should"; it says "here is".

## §5 Movement

`go {"place": "workshop"}` — flag-registered like `predict` (the `register_*_tool` pattern).
Adjudication is mechanical: unknown place → `fail_kind="args"` listing real places; locked →
`fail_kind="blocked"` naming the unlock condition (ARCH #4 — the wall is learnable). Success
persists position atomically and re-scopes: affordance ordering and the recall situation-key
gain a `place` dimension (W2 phase). Later (W4 phase): the objective rotation gate may escort —
an auto-move to `fields` on park, mechanical, rendered honestly ("the gate walked you out").

## §6 Phases

- **W0 — the graph.** `world.py`: schema, derivation, position store, `render_here`, `to_json`,
  flag, bounded reads, full unit tests incl. every invariant W1–W9 that is testable statically.
- **W1 — inhabitation.** Context block wiring (semi-stable zone), `go` tool registration +
  honest refusals, eidos wiring, wiring tests. Flag-dark end to end.
- **W2 — a live world.** Salience-gated events rendered as place notices; strategy-guardrail
  SIGNPOSTS at situation-keyed places ("a signpost stands here: …"); recall situation-key gains
  the place dimension.
- **W3 — the shared map.** `GET /api/world` + a dashboard map panel (same graph, operator view):
  where the creature is, what's lit, what's locked. Charlie and the creature share a place.
- **W4 — patrol & experiments (future).** Patrol affordances for unending goals (walk-the-fences
  = service checks as a place-sequence); hard-scoping experiment behind its own flag; rotation
  escort.
- **W5 — other renderers (far future).** The PS1-era 3D client. Out of scope; §2 is its API.

## §7 Success criteria (adjudicable, growth-panel-able)

- Place-visit distribution over a soak week is not degenerate (no >70% single-place camping —
  measured, not enforced).
- Movement precedes matching-domain action more often than chance (the map is being USED as a
  map).
- Zero cosmetic verbs exist (CI: affordances ⊆ tool registry, every build).
- Flag-off byte-identical; render budget never exceeded; every locked door names a real
  condition.
- D6 becomes measurable: time-near-locked-doors before unlock.

## §8 Anti-goals

No parallel fiction (world-only lore, invented NPCs). No world-only quests or rewards. No NPC
dialogue engine. No time dilation. No hard tool-locking by place in v1. No LLM-generated world
prose. The world never becomes a thing to tend INSTEAD of the machine — it cannot, if W1/W2
hold.
