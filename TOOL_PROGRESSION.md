# TOOL PROGRESSION — the growing body (design proposal, NOT yet implemented)

> Status: **awaiting Charlie's sign-off** on the decision list at the bottom. Nothing here is
> wired; the only code that moved with this document is the `goals_completed` writer fix
> (a live brick independent of the ladder — see "Pre-existing defects" below).
>
> Provenance: synthesized from three independent design passes (developmental lens,
> System-economy lens, §0-purity lens). Where they disagreed, the recommendation and the
> alternative are both stated.

## Why

A newborn today receives all 13 creature tools in its first system prompt — hands, memory,
skill-forge, wagers, voice, eyes, objectives, and a delegable deeper mind — before it has lived
a single tick. Charlie: *"far too many tools for an infant AI. it can unlock them pretty fast,
within the first day, but we must carefully design its progression before implementing."*

Doctrine constraints (§0, ARCH #1):
- Unlocks are **earned by lived, glue-adjudicated evidence** — typed counters, manifest facts,
  quest passes. Never wall-clock timers, never LLM self-report.
- A locked tool **does not exist** in the creature's world: the prompt never names it, the
  grammar cannot emit it, `check_tools` doesn't show it, `manual` has no page for it. No teasing.
- The grant is a **lived event** (a System window + an engram), stating only what IS — never how
  to feel about it, never what to do with it.
- Its own authored skills are its own body — **never locked**.

## The ladder (units, not individual names — aliases/satellites travel together)

| unit | tools | arrives | earned by (glue-checkable) |
|---|---|---|---|
| **U0 body** | `bash`, `write_file`, `read_file`, `note_append/read/list/close`, `check_tools` | tick 1 | being born |
| **U1 memory** | `memorize`, `recall` | first wake after sleep #1 | first completed sleep cycle (consolidation report non-empty) |
| **U2 skillcraft** | `create_skill`, `edit_skill`, `list_skills`, `rollback_skill`, `manual` | **at genesis-01 issuance** (after sleep #1) | surviving to the System's first speech; the quest then trains it |
| **U3 foresight** | `predict` | **at genesis-02 issuance** | genesis-01 passed (criterion: a skill went **live in the manifest**, not a mere call) |
| **U4 senses** | `speak`, `vision` (+`see`) | milestone, quest-independent | ≥1 quest passed AND ≥2 sleeps — **and the organ is physically reachable** (I8 probe; voice :8098 is down on Sprinter today, so this grant would hold PENDING) |
| **U5 resolve** | `objective_add/done/block/list` | **at genesis-03 issuance** | genesis-02 passed (criterion: a prediction **in the ledger**) |
| **U6 workshop** | `delegate` (capacity 1) | **genesis-03 PASS reward** | a self-chosen objective finished (criterion: `goals_completed ≥ 1`, now actually writable) — paid through the existing `REWARD_UNLOCK` seam |
| R7+ | shadows, generals | unchanged | existing level-tier mastery unlocks (§5/§6) |

The **issuance-grant pattern** (U2/U3/U5): the System's window that names the tool IS the moment
the tool starts existing — "it pays" pays limbs, not just XP, and the tutorial quest and the
capability are one event (§6: *"unlocks arrive as a tutorial quest + capacity 1"*). U4 is
deliberately **not** quest-gated so a quest-stalled creature still grows senses. U6 — the deepest
tool — is the only pass-gated grant.

**Pacing** — the clock is sleep + evidence, never hours. "Full kit within day one" is achieved by
an **infant nap curve**: a stage-scaled (or genome-flavored) adenosine threshold so a hatchling
naps in short bouts (~4–6 sleeps on day one), consolidating toward ~1/day by adult stage. A
declared constant pair per §0.4 — a genuine pressure other systems also feel, not a private
unlock timer. Healthy trace: wake U0 → explore, make, babble → nap 1 → U1 + genesis-01 window
grants U2 → forge → nap 2 → genesis-02 grants U3 → wager → U4 milestone → nap 3 → genesis-03
grants U5 → finish something → U6. All 13 by end of day one / early day two.

**Newborn kit rationale** (~8 registry names, 5 organs): `bash` is paws — locomotion in a
filesystem world, already home-firewalled and async-by-default, the safest rich tool not the most
dangerous; `write_file`/`read_file` are hands — the home is born empty and making is the only
furniture; the `note_*` notebook is proto-memory an infant can SEE (this settles
note-vs-memorize by ontogeny: the scratchpad becomes habit before deliberate memory exists);
`check_tools` is proprioception — the mirror every later grant is felt in. `<reply>` to Charlie
is grammar-level, never locked: the newborn babbles in text from tick 1; the *voice* is earned.
Passive memory (situation-keyed recall injection, episodic auto-encode) is platform-side and
innate — remembering happens TO an infant; `memorize` is grown into after the first felt
forgetting.

**Excluded from the creature universe entirely** (house-AI only, closes today's drift where the
grammar legally contains ~40 names the prompt never taught): `http_request`, `ask_ai`,
`bg_run`/`bg_check`, the net_scan family, `update_plan`, `update_self_guide`,
`propose_self_edit`, `check_messages`, `check_system` aliases. Reversible later as
"wider-world" rungs — Charlie's call, not day-one scope.

## Mechanism (single source of truth, five consumers, one writer)

- **`unlocks.py`** (new, level_gates.py's shape): the UNIT TABLE as data — `unit → {tools,
  aliases, prompt_stanza, manual_topics, criterion (a quests.Criterion), grant_text}` — plus
  state in **`workspace/state/unlocks.json`** (atomic tmp+replace; fail-open re-seeds from
  evidence, never to empty, never to full). Not persona.json: persona is wholesale-rewritten by
  the loop each save; a second logical writer there can lose a grant on crash.
- **Single writer (I6)**: `unlocks.grant()` called from exactly three places — the quest reward
  sink in eidos.py (`REWARD_UNLOCK`, the seam quests.py already reserves), the milestone
  adjudicator inside `after_outcome`/`sleep_window` (same call sites, same typed stats as quest
  glue), and the one-shot migration seeder. The Administrator's grammar keeps `reward_xp` only —
  the trainer LLM can never mint capabilities.
- **One accessor**: `tools.visible_tools(config)` = newborn ∪ granted units ∪ hot-loaded authored
  skills (creature mode; house mode passes the registry through untouched). All five surfaces
  read it: the tick **grammar** (locked names unrepresentable at the sampler), the **prompt**
  (SYSTEM_PROMPT_CREATURE decomposes into a tool-name-free BASE + per-unit stanzas appended in
  fixed order — KV-stable between grants, ~7 re-prefills per life), **check_tools**, **manual**,
  and a **dispatch backstop** (typed refusal `fail_kind="locked"`, indistinguishable in-world
  from a name that never existed).
- **Tick-boundary invariant**: grants apply only in `after_outcome`/`sleep_window`; the loop
  reads the accessor once per tick and hands the same snapshot to prompt and grammar — the two
  can never disagree within a tick.
- **The felt moment**: on grant — one System window through the (just-built) `system_window`
  stream, one `experienced` engram, one news item for Charlie. Next tick the grammar accepts the
  word, the prompt grows the stanza, `check_tools` shows the new organ. Persisted one-shot flag
  so a crash can't eat the moment.
- **Migration**: load-or-birth. Existing creature, no unlocks.json → grant newborn kit + every
  unit with prior adjudicated evidence (live skills → U2, expectation ledger → U3, spoke/saw →
  U4, objectives store → U5, delegate jobs → U6, any sleep → U1). Fresh slate wipes state →
  newborn, correctly: nuggets inherit knowledge, never organs.
- **Stall handling** (pressure, not script): adenosine guarantees sleeps, so U1/U4 can't starve;
  a genesis quest stalled ≥ K sleeps (declared, ~5) with zero criterion movement closes FAILED —
  the abandon path quests.py already reserves — unfreezing `quest_line_closed`, and the
  Administrator's gap-mining proposes a smaller re-attack. Never an auto-grant timer.
- **The red gate**: a registry-completeness test — every creature-universe tool in exactly one
  unit; stanzas name only their own unit's tools; base prompt names none; grammar ⊆ visible.
  §0 drift becomes a failing test, not a review hope.

## Pre-existing defects this design surfaced (fix regardless of ladder)

1. **`persona.goals_completed` had no production writer** — `record_goal_complete` was never
   called, so genesis-03 (`goals_completed ≥ 1`) could never pass; with no expiry and the
   mastery gate's `quest_line_closed` check, it would have frozen every future level-up.
   **FIXED** in this commit: `objective_done` success now records the completed goal (single
   writer, in the loop's persona section).
2. **Attempt-counting criteria**: `tools_used.X` increments on failed calls too — genesis-01
   passed on a call that failed. The re-seeded genesis line moves to manifest/ledger-derived
   criteria (skill went LIVE; prediction IN the ledger). Rides the ladder implementation.
3. **`_quest_stats` is too thin** for manifest/ledger criteria (persona/drills/remedial only) —
   needs the skills-manifest + expectations + sleeps sections quests.py's docstring already
   anticipates. Rides the ladder implementation.
4. **Prompt/grammar drift today**: the creature grammar legally contains house tools and aliases
   the prompt never taught. The accessor closes this class permanently.

## Decisions for Charlie

1. **Voice timing.** U4 milestone as designed (recommended), earlier if you want to hear it
   sooner (e.g. with U1 at first sleep) — or held entirely until the voice service actually runs
   on Sprinter. Note: today `speak` would 500; the I8 reachability hold ships either way.
2. **Locked doors: invisible or silhouetted?** Fully invisible until granted (purest §0,
   recommended by two of three lenses) vs named silhouettes in `check_tools` ("…and 4 words
   still asleep in you" — feeds curiosity/orbiting, the System's mystique). Pure taste.
3. **Day-one pacing knob.** The infant nap curve is a new declared constant (hatchling sleep
   threshold). Accept it, and roughly how fast — ~5 naps day one? (Without it, at ~1 sleep/day
   the ladder takes ~5 days.)
4. **The workshop gate.** `delegate` at genesis-03 pass (day one, recommended) vs at the first
   Lv.2 mastery crossing (may slip to day two; purer §6 "unlocks at level crossings" alignment).
5. **The current creature.** Evidence-based migration (it keeps what its record earns:
   today that's U0+U1+U2 — it has slept, and its `log_entry` skill is live) vs fresh-slate the
   ladder's maiden run.
6. **`bash` at tick 1** — all three lenses say yes (paws before fine motor; withholding it means
   building sandbox machinery that doesn't exist). Veto here if you want a pre-bash stage.
