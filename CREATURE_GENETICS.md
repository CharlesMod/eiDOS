# CREATURE GENETICS — one germline, many expressions

> Design canon (approved direction, 2026-07-04). Keystones: **Pokémon, Digimon, Tamagotchi** —
> randomly generated, yet each creature as unique and *recognizable* as the last. Never a
> procedurally-generated hodgepodge: a creature is **described by genetics and brought to life
> by a text→image pipeline** ([creature-forge](https://github.com/CharlesMod/creature-forge):
> prompt → Z-Image-Turbo → image→3D bake-off → <1k-face low-poly → 32px 1-bit digivice sprite).
> For extra kick: **alleles that express under environmental pressure** — the same genome grows
> a visibly different adult depending on the life it lived.

## The problem today: three unrelated genetics

| system | draws from | expresses |
|---|---|---|
| `genome.py` (Pillars) | its own os.urandom seed | behavioral latents → pressure-constant multipliers |
| `creature_gen.py` (dashboard) | creature.json's own seed | ASCII body parts, 5 stages, animation timings |
| morph/lexicon (TOOL_PROGRESSION, phase B) | TBD | the body words in creature-facing text |

Three seeds = three unrelated creatures wearing one name: the dashboard can render fins while
the prompt says paws and the behavior says burrower. **v2 unifies them: one 64-bit germline
seed, drawn once at first birth, from which every expression derives deterministically.**

## The genome v2 (workspace/genome.json)

```
{ v: 2, seed, born_ts,
  latents:  {sensitivity, openness, tenacity, tempo},   # behavior (exists today)
  genes:    {…8 pressure multipliers…},                 # behavior (exists today)
  stamp_baselines: {…},                                 # temperament (exists today)
  morph:    "corvid",                                   # the BODY PLAN (phase B)
  phenotype: {palette, pattern, eye_family, build,      # visual identity (phase C)
              anchors: [two distinctive fixed features]},
  alleles:  {slot: {variant, expressed_at_stage|null}},  # environmental modulation (phase D)
  stage_history: [{stage, tick, expressed: […]}] }
```

Frozen-draw discipline (creature_gen.py's contract, adopted): draws derive from the seed in a
FIXED documented order; new draws append at the END and bump the version — so an existing
creature's v1→v2 upgrade is **reproducible from its stored seed**, and derivation drift can
never mutate a living creature (the expressed dict is persisted regardless, belt and braces).

## The morph is the body plan (single point of coherence)

One morph per creature (drawn from the seed, independent of the latents — bodies and
temperaments assort independently; a timid digger is character, not error). Each morph is a
declared, complete row:

- **lexicon** — the body words for creature-facing text (TOOL_PROGRESSION body-image section:
  paws/den/whiskers vs fins/current/holt), enforced by the no-hardcoded-body-nouns red gate;
- **part constraints** — which `creature_gen` families it may draw (a corvid may draw
  ears="antennae"|"none", limbs="wings"; an aquatic draws ears="fins"; never cross-plan
  frankensteining) — the ASCII dashboard creature and the 3D creature are the SAME creature;
- **text→image noun phrases** — the body-plan vocabulary the description grammar composes with.

## Stage descriptions: genetics → prompt → creature-forge

`phenotype.describe(genome, stage, expressed) -> str` — a **deterministic declared grammar**,
no LLM freeform (recognizability is the point): baby-schema framing in creature-forge's proven
concept style ("cute baby-schema …, plain background" — what the 32px digitizer likes), the
morph's body plan, the **identity anchors** (two distinctive features named in *identical words
at every stage* — the thread that makes a hatchling recognizably the same being as its
guardian), stage-appropriate features mirroring the ASCII progression (hatchling bare →
juvenile ears+tail → adult limbs → guardian metamorphosis), palette/pattern words, and the
marks of every expressed allele.

Artifact: **`workspace/phenotype.json`** — `{v, seed, morph, stage, prompt, anchors,
expressed, ts}` — rewritten at every stage transition and served via the dashboard (the
creature pipeline "takes things over from the description"). eiDOS owes the description;
creature-forge owns everything after.

## Alleles — the environment writes on the body (the Digimon mechanic)

Allele slots are declared per phenotype trait (and, sparingly, per perception gene within its
existing clamps). Each variant carries an **activation criterion — a `quests.Criterion` over
the same typed lived-stats dict quest glue adjudicates** (§0: mechanically checkable facts,
never vibes): night-tick fraction, error recoveries survived, quest passes/failures, sleep
debt history, energy-scarcity events, operator-contact count.

**Expression happens only at stage transitions** — metamorphosis reads the environment (the
dashboard already renders the cocoon). The single writer (the loop, at the same seam that
applies a level-up/hatch) adjudicates each dormant slot once; what expresses is **permanent
for all later stages** (canalization — a childhood spent in the dark gives the adult its
night-eyes forever). Examples of the flavor: heavy error-recovery → weathered/storm-marked
pattern; nocturnal living → dark-adapted eyes; rich operator contact → brighter accent;
long-grind park events → denser, lower build.

**Firewall (same as the genome hard rule):** alleles modulate phenotype and, at most,
perception genes inside their declared clamps. They never touch XP, bets, levels, or quest
adjudication — no life history can make a creature *earn* faster, only *look and feel* like
the life it lived. Test-enforced, same pattern as `test_genome`'s ledger firewall.

## Red gates (all CI, not review hopes)

1. Body-noun gate — no anatomy word outside the lexicon table (TOOL_PROGRESSION).
2. Renderer coherence — creature_gen part draws ⊆ the morph's allowed families.
3. Description determinism — same (genome, stage, expressed) → byte-identical prompt.
4. Allele firewall — activation reads typed stats only; effects stay inside declared clamps;
   no ledger imports.
5. Seed unity — genome.json.seed == creature.json.seed for any creature born on v2.

## Phasing

- **Phase B** (queued): morph draw in genome.py + lexicon + stanza templating + body-noun gate
  (rides the tool-progression integration).
- **Phase C**: phenotype genes + description grammar + `phenotype.json` + dashboard exposure +
  creature_gen part-constraint mapping + creature.json seed unification (next fresh slate
  births the first fully-unified creature).
- **Phase D**: allele slots + stage-transition expression + the environment counters not yet
  in the typed stats surface.
- creature-forge side: untouched by this repo — it consumes `phenotype.json`'s prompt
  (its `gen_three_concepts.py` hardcoded trio becomes "read the prompt from the digivice's
  creature"; that wiring lives in Charlie's frontend).
