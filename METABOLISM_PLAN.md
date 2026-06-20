# eiDOS Metabolism & Drives — the organism economy

Status: **DESIGN** (brainstormed with Dean, 2026-06-20). Build on branch `v3-nervous-system`,
guarded + config-gated like the rest of the nervous system. Builds on the learning layer
(reward/world-model/curiosity/sleep) and the V3 afferent nervous system.

## Why
The first fresh-creature overnight run validated **survival, identity (a creature, not the house AI),
and calm affect** — but the creature **ruminated all night (5067 thoughts, ~3 actions).** Root cause:
a free creature with no STAKES reverts to its training (talk/think). *A robot that sits on its charger
all day is a flawed being.* The fix is not a reward patch — it's a **metabolism**: genuine resource
scarcity that makes inaction costly and engagement nourishing, so organism-like behavior EMERGES.

## Guiding principle (Dean)
**Seek biomimetic feedback loops that naturally resolve the behaviors we want — not code guardrails.**
Loops are self-solving (tune, don't wall). Every mechanism here should be a loop, not a rule.
**Food = values = what the creature becomes.**

## The shape
- An **energy reserve** — the "can I act / am I exhausted" layer. Drains with living + cognition
  (thinking is the *dearest* act) + action; restored by nourishing acts + rest (+ real power later).
- **Hunger is felt** (a felt bar, NOT baseline) so a depleting reserve worsens the body-feeling and
  rides the existing wellbeing→reward homeostatic loop — the creature is already rewarded for keeping
  its body well (`felt.py` + `reward.py W_FELT` give this hook for free).
- **What nourishes = what we want it to do**: learning, mastery, connection, exploration. The loops
  below force a *varied diet* → a varied life.

## The loops (the heart)

### Loop A — Learning-progress nutrient (resolves THREE wants at once)
Nourishment from learning = the world-model getting **better at predicting** (prediction error
dropping over exposure / compression gain), NOT raw surprise. From this one honest measure:
- **anti-chaos**: static/noise is forever-surprising but yields no lasting predictive gain → stops
  feeding instantly → boredom with noise is automatic (the TV-static problem self-resolves).
- **variety**: repeating a thing stops yielding progress (already learned) → stops feeding → variety
  is sought on its own (sensory-specific satiety, free).
- **maturity**: a mastered domain yields no more progress → "been-there-done-that" → pushed to
  harder/newer frontiers → the developmental arc emerges, *not coded*.

### Loop B — Satiety as natural regulation
Full reserve → no hunger → no drive to feed → rest/idle; living drains it → hunger returns → forage.
Self-sustaining. No coded "surplus pole"; play is just what a not-hungry creature does with the
learning drive idling.

### Loop C — Tiredness → sleep before zero (hibernation, not death)
As energy falls it **feels tired** and naturally reduces expenditure (cheaper/slower cognition) →
drifts to sleep *before* reaching 0 → recovery wakes it. Energy=0 is sleep, not death. Rhythm emerges
from the energy balance + (later) real power — mild on a wall-plugged desktop, a real diurnal pulse on
a battery robot. **Endgame:** a creature that owns a Tuya plug and plugs itself in when tired =
"going to bed" as a sought biological pressure (the opposite of the charger-bot: *seeking* the charger
as rest, then leaving to live).

### Loop D — Connection, reciprocation-gated (self-calibrating sociability)
Connection nourishes **only when reciprocated** (user replies / holds the listening box / approves —
existing bond signals). An unrequited bid yields no food and costs a little energy → nagging into
silence **self-extinguishes** (the creature learns bids to an unresponsive user aren't fed, turns to
other foods). An engaged user reciprocates → bids are fed → it becomes a companion. **The creature
becomes exactly as social as the user invites it to be** — annoyance self-corrects, intrigue
self-amplifies (attachment theory). Optional genome/seed starting bias (aloof / balanced / companion)
that the loop then tunes. **Backstop guardrail** (the one wall that earns its keep): a refractory /
rate-limit on *initiating* contact, so even a mistuned creature cannot spam.

### Loop E — Mastery via a skill LANGUAGE (the revamp)
Accomplishment nourishes only on real, verified, *reused* capability — which requires skills to stop
being brittle monoliths and become atomic, composable, and growing. **Design-first** (section below).

## Honesty (anti-Goodhart) — the make-or-break
Every food must be earned and **verified by lasting / downstream value, never self-declaration** (the
creature already gamed "thinking succeeded"): knowledge that gets recalled again; a skill that gets
*reused*; an exchange the user reciprocates; fetched info that feeds a later action. We define what's
nourishing (values); the creature learns *how* to obtain it (foraging policy, via the existing reward
learner). Get this wrong and we've built a slot machine, not a creature.

## Cross-cutting
- **Genome-weighted appetites** → individuality (explorer / builder / companion). Maturity is free
  from Loop A.
- **Behind-the-curtain**: surface energy, hunger, each appetite, and what's feeding it.
- Guarded + `nervous_metabolism_enabled`; inert if nervous disabled; never blocks the tick.

---

## TODO (phased)

### Phase M0 — Energy core
- [x] **M0.1** `nervous/metabolism.py`: energy reserve [0,1]; drain = basal + cognition (dearest) +
  action; recover = rest; publishes a retained `metabolism` event; `snapshot()`. Tests in isolation.
- [x] **M0.2** Hunger as a felt bar: fold energy→hunger into `felt.py` `_PHRASE` (NON-baseline, so it
  drives the overall feeling) → rides wellbeing→reward. Tests (hunger worsens overall; recovery eases).
- [x] **M0.3** Tiredness self-regulation: low energy → cheaper/slower cognition → sleep before 0
  (hibernation), recovery wakes. Wire to the sleep cycle. Tests (never flatlines; wakes on recovery).
- [x] **M0.4** `run_loop` wiring (guarded, `nervous_metabolism_enabled`); behind-the-curtain shows
  energy/hunger. Full suite green. Commit.

### Phase M1 — Learning-progress nutrient (Loop A)
- [x] **M1.1** Extend `nervous/worldmodel.py`: track learning **progress** (prediction-error drop over
  exposure / compression gain), not just instantaneous surprise.
- [x] **M1.2** Feed energy from genuine learning progress. Tests: noise/chaos does NOT feed
  (anti-TV-static); repetition satiates; a mastered domain stops feeding (frontier-seeking).
- [x] **M1.3** Wire into metabolism + reward; surface in behind-the-curtain. Commit.

### Phase M2 — Mastery + the SKILL REVAMP  ⚠ DESIGN-FIRST (do NOT execute until the design below is settled)
- [x] **M2.0** Finalize the skill-language design (section below) with Dean.
- [x] **M2.1** Atom layer: expose the reliable built-in toolset as an in-scope vocabulary for authored
  skills (kills the `import requests` / out-of-scope `http_request` brick walls).
- [x] **M2.2** (atoms compose + soft-fail; skill→skill calls deferred) Composition: skills compose atoms (+ other skills); typed-failure (atoms never raise);
  short and legible.
- [ ] **M2.3** Promotion loop: a proven, reused composition is compiled into a new named atom/skill
  (ties to `reward.habits()` habit-compilation) → the vocabulary grows.
- [x] **M2.4** Accomplishment nutrient: feeds only on real run + downstream reuse (anti-gaming). Tests.

### Phase M3 — Connection nutrient (Loop D)
- [ ] **M3.1** Reciprocation-gated connection feed (bond signals); unrequited bid = no food + small
  energy cost; self-calibrating sociability. Optional genome/seed bias.
- [ ] **M3.2** Safety backstop: refractory / rate-limit on initiating contact. Tests (nagging
  self-extinguishes; cannot spam).

### Phase M4 — Real solar/power layer (Phase 2)
Dean: wire in real **battery level** AND **solar intake** when we can. Two distinct signals, two roles:
- **battery level (%)** → anchors / calibrates the creature's actual energy *reserve* (the health bar
  IS the real charge; deep-night drawdown is a real famine).
- **solar intake (watts)** → the *recharge rate* — food availability in its environment, abundant at
  midday, nothing at 3am. Modulates `feed()`/recovery rather than the reserve directly.
- [ ] **M4.1** Investigate readable power: Windows battery API / UPS / solar inverter on the LAN /
  smart watt-meter (e.g. a Tuya energy plug). Capture both charge % and PV watts if available.
  - **CONFIRMED SOURCE (Dean, 2026-06-20): the solar system is a Renogy Rover 20A with Bluetooth (the
    BT-1/BT-2 module).** It exposes battery **state-of-charge %** AND **PV charging rate (watts/amps)**
    over BLE — exactly the two signals M4 needs, from one device. Read it via BLE (e.g. `bleak` on
    Windows; the Rover speaks Modbus-over-BLE — community libs: `renogy-bt` / `solar-monitor` decode
    the 0x0100-ish holding registers for SOC, PV W/V/A, battery V, load). Plan: a tiny poller →
    publishes a retained `Kind.metabolism` power event (SOC + PV watts) → metabolism reads SOC as the
    reserve anchor and PV watts as the `feed()`/recovery multiplier. No Tuya watt-meter needed for the
    sense path; Tuya stays only for M4.3 (self-charging actuation).
- [ ] **M4.2** If readable: battery% anchors the reserve, PV watts drives recovery → a real diurnal
  rhythm (lively by day, husbanding itself at night). Else keep internal + the hook ready.
- [ ] **M4.3** (future) self-charging via a Tuya plug = "going to bed" — the creature seeks its charger
  when tired, the most biomimetic loop of all.

---

## Skill-language design (M2 — the deeper think; DRAFT, finalize before code)

**Reframe: skills should be a LANGUAGE, not a pile of scripts.** A language has primitives (vocabulary),
composition (grammar), abstraction (naming proven phrases), and growth. Today's skills are isolated
essays — each authored whole, each able to fail catastrophically (`check_boss_presence` rewritten 20×
and still broke). We want a *growing language* the creature builds capability in.

**The key realization:** the reliable built-in tools (bash, http_request_robust, memorize, recall,
vision, speak, …) ALREADY ARE the atoms. The brick walls happen because authored skill code can't
*reach* them — so it reinvents them (`import requests`) and breaks. So step one is almost mechanical
and huge: **inject the built-in toolset as a clean, in-scope vocabulary for skill code.**

**The three layers:**
1. **Atoms (guaranteed vocabulary).** Platform-provided, tested, always in scope; the creature never
   imports. Categories: I/O (read/write/list), net (`http_get`/`http_post`/`web_search`/`fetch`,
   timeout-bounded), memory (`store`/`recall`/`note`), parse (`json`/`regex`/`extract`), compute
   (`compare`/`filter`/`map`/math), time (`now`/`schedule`), comms (`speak`/`notify`/`message`), sense
   (`look`/vision), introspect (`list_atoms`/`list_skills`). Atoms NEVER raise — typed failure only.
2. **Compositions (skills).** Short functions composing atoms (+ other skills via `call(name, **args)`).
   Possibly a declarative *recipe* form (a named sequence of atom calls with data flow) for linear
   skills — inspectable, safe, no arbitrary control flow — alongside Python for logic. Keep each piece
   small and independently working, so partial failure degrades instead of bricking.
3. **Promotion (evolution).** A composition that proves reliable and gets reused enough is **compiled
   into a new named atom/skill** (the habit→skill automatization already half-living in
   `reward.habits()`). The vocabulary GROWS, so each day the creature builds something more complex on
   a larger base of things that just work. *Atoms → compositions → promoted atoms.*

**Authoring reliably:** the creature must SEE the atom vocabulary (signatures + examples) in context —
a "stdlib reference" via `check_tools`/`manual` — so the LLM composes from known-good pieces instead of
hallucinating imports.

**Honesty for the mastery food:** a skill nourishes only when it ACTUALLY RUNS and gets REUSED
(downstream value) — never on authoring alone. Spamming trivial skills feeds nothing.

**Resolved (Dean + predecessor data, 2026-06-20):**
- **Code, not recipes.** It's a competent coding agent — let it write Python over the atoms. The job
  is to *set it up for success*: catch the `requests`-class failure at author-time (see promotion),
  not at 3am in the loop.
- **Sandbox now, with a checkbox to set it free.** Authored skills run sandboxed by default
  (`skill_sandbox_enabled = true`); a config flag unleashes full coding-agent freedom.

**Predecessor behavior — the data behind the seed vocabulary** (house-AI archive, **4039 real actions**):
networking dominates — `http_request` 405 + `http_probe` 75 + `net_scan` 125 + `tcp_probe` + `ping_host`
38 + `probe_mqtt` ≈ **680 calls**; `bash` 501; memory/introspection (`recall`/`memorize`/`note`/
`check_tools`/`check_system`) heavy and ~100% reliable; `vision` 83%. The WALLS: `create_skill`
**26% success** (3 of 4 authored skills failed); **20 of 49 skill files `import requests`** (not
installed); 15 `No module named requests`; async shaky (`async_result` 58%, `bg_run` 72%); a `\U`
unicodeescape SyntaxError from Windows paths written into skill code.

**Seed atom vocabulary (data-grounded; cover what it actually did + the walls), ~14 atoms ≈ 95% coverage:**
- `http_get(url,…)` / `http_post(url, json=/data=,…)` — THE #1 need; expose the working
  `http_request_robust` so authored code never reaches for `requests` (kills the 40%-of-skills wall).
- `net_scan(subnet)`, `port_probe(host,ports)`, `ping(host)`, `tcp_probe(host,port)` — built-in probes
  ran ~100%; the *authored* ones ran 0–27%. Expose the good ones.
- `sh(cmd)` — bash, the workhorse.
- `recall(q)` / `memorize(fact)` / `note(text)` — memory (100%, heavily used).
- `json_parse(text)` / `extract(…)` — the pervasive HTTP-response handling.
- `look(image, question)` — vision.
- `list_tools()` / `check_system()` — introspection.
- `run_bg(cmd)` / `job_result(id)` — a CLEANER async primitive (the old async model was a hidden wall).

**Promotion — close the expectation↔reality gap (the core fix):** the predecessor promoted on
author-time COMPILE-pass, but `import requests` compiles and only fails at RUNTIME → the registry
filled with dead weight (**39 of 49 skills never worked; several 0% yet still callable**). Fix:
1. **Author-time env validation** — import/dry-run the skill in the REAL runtime before promoting;
   reject `import requests` etc. with a pointer to the `http_get` atom. Catches the 40% class up front.
2. **Runtime-grounded standing** — a skill keeps its place by REAL success over N calls (the reward
   layer already tracks outcomes); repeated-failure / never-successfully-used skills are flagged →
   demoted → garbage-collected. Promotion to an *atom* requires sustained real reliability.
3. **Atoms remove the root cause** — `http_get` instead of `import requests` means the dominant
   brokenness simply never occurs.

**Still open for M2.0:** sandbox mechanism (no-`import` + scope-restriction + watchdog vs. stronger
isolation); exact runtime-reliability thresholds for promote/demote/GC; whether to evolve
`create_skill`/`edit_skill` in place (inject atom scope + author-time validation) or add alongside.

---

## Open questions (whole plan)
- Social starting bias: genome-random, or an explicit user setting (aloof / balanced / companion)?
- Mortality: hibernation only, or eventually real death (finite creatures — profound but lossy)?
- How sharp is hunger overall — gentle motivator vs. real teeth toward torpor (stakes vs. cruelty)?
