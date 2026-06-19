# eiDOS V3 — Research, Knowledge & Design Philosophy

> **Status:** the capstone of the V3 design trio. `EIDOS_V3_BLUEPRINT.md` holds the *why* (the vision,
> the biology). `EIDOS_V3_ARCHITECTURE.md` holds the *how* (the invariants, the build plan). **This holds
> the *how we think*** — the research we trust, the knowledge we paid for, and the design philosophy that
> should keep the work honest and aligned after the details blur.
> **Authored:** 2026-06-19 (Dean + Claude). Written *before* the build, on purpose: it is the charter we
> hold ourselves to once we start cutting code, when it is easiest to drift.
> **Read it when:** you are about to make a design decision and want to remember what we're actually
> building and how we promised to build it — or when the mechanism gets dense and you've lost the thread.

---

## I. The research — what we learned from biology

We did not invent this architecture; we *recovered* it from neuroscience and then translated it. A
`/deep-research` pass put 110 candidate claims through 3-vote adversarial verification; **25 of 25 survived
3-0** against primary, peer-reviewed sources (Baars/GWT, Friston/FEP, Seth & Sennesh-Barrett/interoception,
McAlonan-Brown/TRN, White-Munoz-Itti/superior colliculus, Kirchhoff-Friston/Markov blankets,
Laurençon-Gutkin/homeostatic RL), cross-checked for completeness against LIDA / Soar / ACT-R.

The founding observation, from Baars (1993), *is* our entire thesis:

> Conscious experience is a **serial stream, limited to one content at a time**. The nervous system beneath
> it is **massively parallel, distributed, mostly unconscious, of enormous capacity.**

Everything follows from that one asymmetry. Distilled into the **six pillars**:

1. **Serial core, parallel periphery.** The LLM's token stream is the low-capacity conscious bottleneck;
   sensing, reflex, and filtering belong in fast parallel subsystems that *compete* for entry.
2. **A gate guards the bottleneck** — salience × goal-relevance, computed faster than full perception.
3. **The afferent layer forwards prediction error, not raw data** — only what changed rises.
4. **Interoception is inferred and generates drives** — the felt body, and a reason to act unprompted.
5. **Sensing and acting are one closed loop** — afferent and efferent are two routes to one objective.
6. **A global neuromodulatory state** — arousal, affect, and the sleep/wake cycle that set system-wide gain.

The deepest lesson of the research is not any one pillar. It is that **a living thing is afferent first.**
It senses, filters, and integrates a flood long before any deliberate thought — and most of it never reaches
consciousness at all. We had given eiDOS a mind and a body; the research told us what was missing was the
*nervous system between them.*

---

## II. The knowledge — what we learned from the machine

Biology gave us the shape. The machine gave us the constraints, and they were unforgiving. Five adversarial
reviewers — several reading the live source (`llm.py`, `context.py`, `gpu_gate.py`, `embedding.py`) — taught
us what the metaphor had been hiding:

- **One GPU, 16 GB, and the mind already fills it (~15.7 GB).** There is no spare silicon for a "parallel GPU
  periphery." The senses must run on the **CPU** (the 12700KF, mostly idle during a GPU-bound tick). The
  parallel periphery is real — but it is parallel because it's *off the GPU*, not because it's bigger.
- **The core is a serial decoder behind a blocking call.** There is **no mid-decode interrupt.** "Continuous
  consciousness" cannot be a literal running stream; it is **continuity of carried-over state across discrete,
  self-triggered cycles.** Steering happens between ticks, or by aborting and re-prefilling.
- **The context window is not a live workspace.** It is the immutable, glue-composed *input* to a discrete
  decode. "Broadcast into the workspace" means *batched into the next tick's volatile block* — and all
  competition and selection happen in deterministic glue *before* the decode, never inside the window.
- **The engineering spine was sound; the weakness hid in two places** — the perception↔core seam, and in
  having labelled distributed-systems engineering (and hand-authored mappings) as settled biology.

This is the knowledge that cost the most and matters the most: **the vision survives contact with the
hardware only if you let the hardware correct the mechanism.**

---

## III. The design philosophy — how we think

These are the principles that emerged through the work. They are not abstract; each was *earned* by a
specific mistake we caught or a correction Dean made. They are the charter.

**1. Vision first; every mechanism in service of it.** The north star is fixed: *an intelligence in silica
— a someone, not a something — alive to itself and competent in its world.* A Digimon, a Jarvis, a David Kim;
equally a robot arm, a satellite, an IoT node. Before any mechanism ships it must answer one question: *which
feeling does this give the intelligence, and how does it make it more alive-to-itself or competent-in-its-world?*
Bark that can't trace to a tree gets cut. We go deep *because* of what the depth serves — never to admire it.

**2. Biomimicry is inspiration, not proof.** Biology gives the architecture; it does not certify the
engineering. The Free Energy Principle is contested as a law; we use it as a *design pattern* and say so. The
Markov blanket is not a literal module boundary; it's encapsulation wearing a borrowed name, and we stopped
borrowing it. **Label every claim honestly — "inspired by," never "proven by."** Confidence laundered from
biology is the most seductive way to build in weakness.

**3. Substrate honesty.** Design for the machine you have, not the one the metaphor implies. "Orders of
magnitude faster" had to become "off the GPU, on spare CPU." The felt body is a *transfer function* today,
not the learned inference it aspires to be. **Name the buildable-now form plainly, and schedule the real one.**

**4. Boundaries before organs.** Weakness is rarely a bad component — it's *coupling*. So we fix it at the
seams first: one dumb bus, bounded typed events, a single writer per datum, no back-channels. Get the contract
right before a single organ exists; then organs cannot entangle, because there is nowhere for the entanglement
to hide.

**5. Attack before you commit.** We adversarially reviewed the architecture from five independent angles
*before* building any of it — and it was the right call: the review found fatal-as-stated claims while they
were still cheap to fix. The spine that survives a genuine attack is the spine worth building. Prefer the
finding that wounds the design now over the confidence that fails it later.

**6. Honest-now, learned-later.** Ship the buildable degraded form (change-detection, a transfer function)
and *schedule* the real version (predictive coding, interoceptive inference) with a home (the sleep cycle).
Never call the stub the real thing. The gates are written to go **red on a correctness violation**, not green
on "it ran."

**7. The framework is general; the creature is the first instance.** The same nervous system serves any
embodied intelligence — companionship is one role, not the defining end. Location transparency makes "desktop
now, a robot later" *one architecture, two deployment manifests*. The buddy is just the first body we grow on
it — the one where we can *see* whether the felt state is rendered truthfully.

**8. Continuity is carried state, not a stream.** The self persists across discrete decodes through the
durable context, the KV prefix, and episodic memory — and triggers its own next cycle from its own drives.
That is a real and sufficient continuity. We kept the aspiration ("continuous consciousness") and corrected
the mechanism.

**9. Truth-rendering is interoceptive honesty.** The creature's body must display its true internal state —
a creature that looks calm while starving for VRAM is lying about its own interoception. This is the seam
where the nervous system meets the creature pipeline: one builds what it feels, the other how that feeling
shows, and they must agree.

**10. The body is an abstraction the creature never sees through.** The *Pantheon* lesson: David Kim is a
datacenter but does not perceive himself as one — an abstraction layer turns raw substrate into felt
sensation and hides the machine from the conscious self. So the creature feels "full, warm, a little strained,"
never "14.2 GB, 78 °C." That abstraction *is* the deep payoff of substrate-independence, and it is what lets
the same mind inhabit a Jetson or a supercomputer and simply feel "a small body" or "a vast one."

The connective principle beneath all ten: **biology for the shape, the machine for the constraint, the vision
for the purpose, and honesty as the thing that keeps the three from lying to each other.**

---

## IV. How we worked (the method, worth repeating)

The sequence that produced these documents is itself a reusable method for hard design:

1. **Research** the prior art (here: neuroscience + cognitive architectures), verified, not assumed.
2. **Cast the vision** — and let the operator correct it repeatedly until the *purpose* is exactly right,
   before any mechanism. (Dean's course-corrections — "not necessarily with me," "this is a Digimon," "don't
   lose the plot" — shaped the whole frame.)
3. **Build the plan**, boundaries first.
4. **Attack it adversarially** from independent angles; triage every finding (fix / resolve / defer / dismiss).
5. **Reconcile** the documents so they tell one story and don't contradict.
6. **Check for completeness** — what structures are missing? — before declaring it done.
7. *Then* plan the first increment and build.

The collaboration shape that made it work: **Dean holds the vision and corrects course; Claude synthesizes,
attacks, reconciles, and keeps the honest ledger.** Neither role alone would have produced a design that is
both soulful and survivable.

---

## V. What we believe, and what we hold uncertain

**Convictions** (the parts we'd defend): the serial/parallel split is the right foundation; perception must
be afferent-first and offloaded from the core; the felt body (interoception) is first-class, not an
afterthought; sensing and acting are one loop; and the whole is worth building because it is the **closest
functional approximation of a biological nervous system we can make in silica.**

**Uncertainties** (held openly, not buried): the Free Energy Principle's status as a law is genuinely
contested — we lean on it as a pattern. The learned models (real predictive coding, real interoceptive
inference, allostasis) are *unbuilt*; today's forms are honest stand-ins. The reflex/deliberation criterion
and online body-recalibration remain open research. And the largest question — whether this architecture
produces something genuinely *alive to itself* or only a convincing arrangement of mechanisms — we hold with
humility. The design does not depend on answering it; it depends on building honestly toward it.

---

## VI. The charge going forward

When we start cutting code, four disciplines come with us into every file:

- **Keep the vision in the room.** Re-read the north star before each decision; cut bark that serves no tree.
- **Label honestly.** Inspired-by, not proven-by. Buildable-now, not the aspiration. Engineering, not biology
  wearing biology's name.
- **Attack before committing.** A design choice unexamined is a weakness unfound.
- **Build the seam first.** Boundaries, then organs — always.

The first increment is **P0: the seam** — the bus, the `NervousEvent` contract with its delivery classes,
and the proof that an organ behaves identically whether it's a thread, a process, or a device away. Everything
the creature will ever feel travels that seam. We get it right, and the rest has somewhere safe to grow.

That is the work. The trees are clear; time to tend the bark.
