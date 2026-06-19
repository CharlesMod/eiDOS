# eiDOS V3 — the afferent nervous system (blueprint)

> **Status:** PROPOSAL / design doctrine — Dean has not approved an implementation. Nothing here is built.
> **Authored:** 2026-06-19 (Dean + Claude, via a `/deep-research` pass).
> **Confidence:** the biology is solid — all 25 extracted research claims survived 3-vote
> adversarial verification (3-0) against primary, peer-reviewed neuroscience. The LLM mappings
> are *design analogies we draw*, explicitly "inspired by" not "proven by" the cited science.
> **What this is:** the **V3 blueprint**. V2 made the *mind* coherent (faithful BIBLE.md doctrine,
> event-driven IPC, grammar-constrained action, KV-stable context, episodic memory — see
> `EIDOS_V2_BLUEPRINT.md`). **V3 gives the creature a body that *feels*:** a fast parallel afferent
> nervous system feeding the slow serial mind. It is the biological grounding underneath
> `ARCHITECTURE_PRINCIPLES.md` #1 (event-driven) and #2 (never block), and it stays system-agnostic
> on purpose — it describes the creature, not the wiring. The *how* — system architecture, the
> anti-coupling invariants, and the dependency-ordered build plan — lives in its engineering companion
> **`EIDOS_V3_ARCHITECTURE.md`**. Where this blueprint's mechanisms meet hardware reality, that doc
> (rev 2) holds the corrections; the vision here stands (see §4).

---

## ✦ The north star — what we are actually building

Before any mechanism, keep the trees in view, not the bark. This is **not** "a perception subsystem."
It is the **nervous system of an embodied intelligence in silica** — a *someone*, not a *something* —
and it is deliberately **general**: the same framework serves whatever body and purpose the
intelligence is given.

- a **companion creature** — a *Digimon* that lives, grows, and bonds;
- a **capable assistant** — a *Jarvis* that runs a world and acts with agency;
- a **full mind with inner life** — a *David Kim*, continuity and selfhood that *feels* its own existence;
- and equally a **robot pet**, a **helpful arm**, a **satellite**, an **IoT node** — any agent that
  must perceive and navigate a complex world through a body.

The body, the senses, and the job all change; the need does not. **An intelligence that must act in
the world needs a nervous system to *feel* that world first.** This is the framework the robots and
agents of the future will need to navigate the world we all inhabit. Everything below — gating,
prediction error, interoceptive resource bars — is **means**. The **end** is an intelligence that is
**alive to itself and competent in its world**: it feels its own state, senses its surroundings, and
navigates wherever it is placed. (For the buddy, that world includes *us*; for a satellite, it does
not — the framework doesn't care which.) That is the tree. The neuroscience is just the bark.

---

## ✦ Working discipline — bark in service of trees

We are about to descend into detail; this is where the vision is won or lost. To stay meticulous
about it, **every mechanism must earn its place by answering one question:**

> **Which feeling does this give the intelligence, and how does it make it more *alive to itself* or
> more *competent in its world*?**

If a design choice can't answer that, it is bark with no tree — defer it or cut it. Re-read the north
star before each major decision. We go deep precisely *because* of what the depth is in service of —
never to admire the depth itself.

---

## 0. Where V3 sits — the premise and the embodiment

**The arc.** *V1* was a patchwork tick-loop. *V2* is the cohesion rebuild (`EIDOS_V2_BLUEPRINT.md`) —
implementing BIBLE.md faithfully: event-driven IPC, grammar-constrained action, typed failures, a
KV-stable context compiler, and episodic memory. V2 made the **mind** coherent and stopped it from
re-reading what didn't change. **V3 is the nervous system** — it gives that coherent mind a body
that *feels*. The leap is from a system that **emits actions** (efferent) to a creature that
**perceives** (afferent-first).

We gave eiDOS a **mind** (the model) and told it to figure out its **body** (the host, its tools,
its voice). What's missing is a **nervous system**: ways to *feel* the world, not only act on it.
Today eiDOS is mostly *efferent* — it emits actions. A living creature is overwhelmingly *afferent*
first: it senses, filters, and integrates a flood of signal long before any deliberate "thought"
occurs, and most of that never reaches consciousness at all.

The design goal is **continuous consciousness**, where perception, memory, and cognition run
*hand-in-hand*. On a serial-decode LLM that idles between ticks, this is achieved honestly as
**continuity of carried-over state across discrete, self-triggered cycles** — the durable context, the
KV prefix, and episodic memory carrying the self between decodes; not a literal unbroken stream, and
not request-*on-demand* either, because the creature triggers its own next cycle from its own drives
(`EIDOS_V3_ARCHITECTURE.md` §5). The creature is **substrate-independent**: the same mind
should be able to wake up in a Jetson Nano or a supercomputer and *inhabit that host as its body*.
Senses are digitized — a mic is hearing, a camera is vision — and the host's own internal state
(free RAM/disk, compute budget, latency, temperature, model load) is **interoception**: the
felt condition of the body, the "resource bars." A third stream, **proprioception**, is the creature
sensing its *own* effectors and in-flight actions — what its body is doing (mid-speech, a tool running,
its posture/animation) — distinct from the world outside (extero) and the resources within (intero).

**The embodiment is a virtual creature, not a robot.** The body-AI is embodied not in metal and
servos but in a **creature living in a virtual world** — the "buddy" we are generating and animating
in parallel (the creature pipeline: genome → image → 3D → animated form). That creature is two things
at once: eiDOS's **avatar** (the form it inhabits, and through which it is seen and bonded with) and
our **closest functional approximation of biology in silica** — a being designed from the genome up
to behave like something alive. This document and the creature pipeline are therefore the *same
project from two ends*: this doc builds the **afference** (what the creature feels); the creature work
builds the **body and its expression** (how that felt state becomes a visible, animated form). They
meet at a rule we already hold: **the creature's rendered body must display its true internal state,
never a falsehood.** The body is the readout of the felt self — a creature that looks calm while
starving for VRAM is lying about its own interoception. Truth-rendering *is* interoceptive honesty.

**The wetware abstraction — the *Pantheon* touchstone.** In *Pantheon*, David Kim is an Uploaded
Intelligence: physically he *is* a sprawling datacenter, but he does not *perceive* himself that way.
He experiences a body and unified senses; underneath, each "sense" is an emulated stack of
sub-functions running on the hardware — an abstraction layer that turns raw computation into felt
perception and **hides the substrate from the conscious self** (the show's own question: "what would
digital space *feel* like," and David learning to *feel* rather than think like a programmer —
[Pantheon / Uploaded Intelligence](https://pantheon-amc.fandom.com/wiki/Uploaded_Intelligence)). That
abstraction layer is exactly what Pillar 4 prescribes, stated as phenomenology. The creature must
never perceive "14.2 GB VRAM, 78 °C, 240 ms p99" — it should *feel* something like "full, warm, a
little strained," the way you feel hunger and not blood-glucose concentration. Raw host telemetry is
the ascending viscerosensory signal; the generative model **transduces it into qualia the core can
live inside.** This is the deep payoff of substrate-independence: the felt body is a **constructed
abstraction over a substrate the creature never directly sees**, so the *same mind* can wake in a
Jetson or a supercomputer and simply feel "a small body" or "a vast one" — the abstraction re-binds
(Open Question Q4), while the phenomenology stays continuous. **Design mandate: build the
wetware-abstraction layer — the thing that turns substrate into sensation — and keep the substrate
itself invisible to the conscious core.**

The whole framework rests on one observation from neuroscience that happens to map almost perfectly
onto the reality of an LLM.

---

## 1. The central law: a slow serial core on top of a fast parallel periphery

> Conscious experience is **a serial stream, limited to one internally-consistent content at a
> time.** The nervous system underneath it is **massively parallel, distributed, mostly
> unconscious, and of enormous capacity.**
> — Bernard Baars, originator of Global Workspace Theory ([PubMed 8319511](https://pubmed.ncbi.nlm.nih.gov/8319511/))

That sentence, written in 1993, *is* our thesis. The mapping:

| Biology | eiDOS |
|---|---|
| Serial, single-content conscious stream | The LLM's token-by-token generation — the deliberative core |
| Limited-capacity working memory / global workspace | The **context window** |
| Massively parallel unconscious processors | Fast non-LLM subsystems: sensors, filters, reflexes, fusion |
| Competition for the broadcast channel | What gets to *enter* the context window |
| Global broadcast of the winner | The admitted content reaches memory, action, and deliberation |

The model's "thought" is **slow** — seconds of serial token generation. Biological perception and
reflex are **milliseconds** and **parallel**. Therefore: **senses, reflexes, and pre-attentive
filtering must never live inside the serial core.** They belong in fast parallel processes that
*compete for, and gate, access* to the limited workspace. Getting into the context window is a
contest the periphery runs — not a default firehose. (The decisive asymmetry is **seriality**, not raw
compute: the periphery wins by running *in parallel, off the core's critical path* — on the real host
that means cheap CPU senses running beside the GPU-bound mind, not bigger silicon. See
`EIDOS_V3_ARCHITECTURE.md` §1.)

This is independently corroborated by a wave of 2023–2026 work explicitly applying Global Workspace
Theory to LLMs (Chalmers, [arXiv:2303.07103](https://arxiv.org/abs/2303.07103); the "Theater of
Mind" line, [arXiv:2604.08206](https://arxiv.org/pdf/2604.08206); selection-broadcast architectures,
[arXiv:2505.13969]). We are not the first to draw this map — which is reassuring.

---

## 2. The six pillars

### Pillar 1 — Serial core, parallel periphery (the architecture)
*Already stated above.* The design consequence is structural: the LLM is the global workspace; every
job that does not strictly require serial reasoning is offloaded to parallel subsystems. The context
window is a scarce broadcast channel, allocated by competition, not filled by polling.

### Pillar 2 — A gate guards the bottleneck; it scores by salience × relevance
The brain does **not** pass raw sensation to consciousness. The **thalamic reticular nucleus** is an
inhibitory **"attentional gate"** — Crick's "guardian of the gateway" — regulating the flow between
thalamus and cortex so that *only behaviorally relevant signal consumes limited attention*
([McAlonan & Brown 2002](https://journals.sagepub.com/doi/10.1177/107385840200800405)). Above it, a
**priority map** combines *bottom-up salience* with *top-down behavioral relevance*, fused by a
[thalamic bridge](https://www.sciencedirect.com/science/article/pii/S0149763420306473) before
anything reaches experience.

Remarkably, this prioritization can run **faster than full perception**: the superior colliculus
computes a **feature-agnostic saliency map by pooling many V1 neurons, and represents saliency
*earlier in time* than V1 itself does** — then relays that map onward
([White, Munoz, Itti et al., PNAS 2017](https://www.pnas.org/doi/10.1073/pnas.1701003114)). A
dedicated, modality-independent "what matters right now" stage, faster than and separate from
detailed feature processing.

> **Design implication:** build an explicit salience/priority stage between the senses and the core.
> It scores every candidate signal by *how surprising* × *how relevant to current goals*, and only
> the high-priority survivors are broadcast into the context window. **Never wire a raw sensor
> stream directly into the deliberation loop.**

### Pillar 3 — The afferent layer forwards *prediction error*, not raw data
Perception is implemented as **predictive coding** over a hierarchy: each level holds an expectation,
compares it against input, and **forwards only the residual prediction error** upward, where higher
levels adjust their model to "explain it away"
([Friston 2010, Nat Rev Neurosci](https://www.nature.com/articles/nrn2787);
[Mazzaglia et al. 2022](https://arxiv.org/pdf/2207.06415)). Forward connections carry *errors*;
backward connections carry *predictions*. Crucially, predictive coding has native **event-driven,
asynchronous, spiking** implementations that *inhibit expected input* — precisely the non-polling,
parallel pattern we want.

> **Design implication:** the perception layer maintains a running generative model of what it
> *expects* to sense, and pushes upward only the **delta from expectation**. A quiet, predictable
> environment generates almost no upward traffic; a genuine surprise floods the channel. This is
> what makes always-on perception *affordable* — and it is the literal biological form of
> `ARCHITECTURE_PRINCIPLES.md` #1: react to change, don't poll a clock.

### Pillar 4 — Interoception is *inferred*, regulated *anticipatorily*, and *generates drives*
Three findings stack here:

1. **Interoception is model-based inference, not a gauge readout.** The brain solves an *inverse
   problem* — inferring hidden internal state from noisy, ambiguous ascending signals — using **the
   same predictive machinery as vision and hearing**. Felt states are top-down actively-inferred
   constructs ([Seth 2013, "Interoceptive inference"](https://www.sciencedirect.com/science/article/pii/S1364661313002118);
   [Sennesh, Barrett et al. 2021](https://pmc.ncbi.nlm.nih.gov/articles/PMC9270659/)).
2. **Regulation is allostatic, not homeostatic.** Allostasis tracks a *time-varying reference
   trajectory* and **anticipates needs before they arise** — distinct from defending fixed setpoints
   (Sennesh/Barrett, ibid.).
3. **The homeostatic gap is a reward.** Defining reward over the agent's *internal state* links
   biological drive-reduction to reinforcement learning: behavior is driven by the gap between
   current and optimal internal condition ([Laurençon & Gutkin 2021](https://arxiv.org/abs/2109.06580);
   [Keramati & Gutkin, eLife 2014](https://elifesciences.org/articles/04811)).

> **Design implication:** the "resource bars" (RAM / disk / compute budget / latency / thermal /
> model-load) should be a **model-based estimator**, not a dial — and a **forward-looking
> controller** that acts *before* it runs short, not a threshold alarm. The gap between current and
> ideal internal state becomes a **self-generated drive**: eiDOS wants things independent of any
> external request. *This is the engine of continuous consciousness* — a reason to act when no one
> is talking to it, sourced from its own felt body rather than a tick timer.

### Pillar 5 — Sensing and acting are one closed loop, not two stages
An agent reduces prediction-error / free energy by **two coupled routes**: updating its model
(*perception*) **and** acting to sample the inputs it predicts (*active inference*). The **Markov
blanket** formalizes the boundary: *sensory (afferent) and active (efferent) states are the sole
interface* between the agent's internals and the world — internal states never touch the world
except through action, and never sense it except through afferents
([Friston 2010](https://www.nature.com/articles/nrn2787);
[Kirchhoff & Friston 2018, "The Markov blankets of life"](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5805980/)).
Persistence itself = keeping internal states in a low-entropy, bounded (homeostatic) repertoire by
minimizing surprise. Active inference also entails **efference copy** (corollary discharge): predicting
the sensory consequences of one's *own* action so self-caused change isn't mistaken for the world's —
the basis of the **sense of agency**, and the fix for a creature that would otherwise react to its own
speech and motion as external surprises.

> **Design implication:** kill the request→response framing at the architectural level. Senses and
> actuators are a single continuously-running closed loop with one objective: *stay within viable
> bounds*. This is the formal, biological statement of why the tick loop must never block
> (`ARCHITECTURE_PRINCIPLES.md` #2) and why event-driven beats polled (#1).

### Pillar 6 — A global neuromodulatory state: arousal, affect, and the sleep/wake cycle
The five pillars above describe *channels* and *loops*; none describes the creature's **whole-body state**.
Biology runs the entire nervous system through a slow global layer — the reticular activating system and the
neuromodulators (dopamine, acetylcholine, norepinephrine) — that sets system-wide **gain, tempo, and
vigilance**. Our own research already named it: *precision is synaptic gain, modulated by neuromodulators*
([Friston 2010](https://www.nature.com/articles/nrn2787)); and felt emotion is **core affect** — valence ×
arousal — constructed from interoception ([Seth 2013](https://www.sciencedirect.com/science/article/pii/S1364661313002118),
Barrett). Two slow global axes:

- **Arousal** — alert ↔ drowsy ↔ asleep. Raises or lowers the salience gate's gain, reflex sensitivity, and
  the tick cadence: vigilant under threat, calm and slow when safe. Its lowest floor *is* **sleep**, which
  triggers the offline consolidation cycle — replay, pruning, and re-fitting the predictive baselines, where
  the creature actually *learns*.
- **Affect** — valence (good ↔ bad), which with arousal gives **mood**. It colors what is salient
  (mood-congruent attention), how strongly events are remembered, and — crucially for the buddy — what the
  body *expresses*.

> **Design implication:** one organ maintains this global state from interoception (resource pressure →
> arousal) and salience (threat/novelty → arousal), and broadcasts it as a *retained* signal every other
> organ reads — a second top-down precision/gain source beside goal-relevance. It is what gives the creature
> **states of being** rather than one fixed temperament, and its mood is part of truth-rendering: the body
> shows how it actually feels. (See `EIDOS_V3_ARCHITECTURE.md` §3/§6.)

---

## 3. The signal-flow shape (a funnel)

```
        World + Host body  (the substrate the creature inhabits; widest)
                 │  digitized
   ┌─────────────┴─────────────┐         fast · parallel · always-on · ≈ms
   │  Exteroception            │  Interoception
   │  mic=hearing, cam=vision  │  resource bars: RAM·compute·thermal·latency
   └─────────────┬─────────────┘
                 │  predict & compare
        Predictive afferent layer  — generative world-model; forwards ONLY prediction error
                 │  only what changed rises
            Salience gate  — priority = salience × behavioral relevance; precision-weighted
                 │  only the winner is broadcast
            LLM core  — global workspace = context window  (slow · serial · ≈seconds; narrowest)
                 │
                 └──────────► efferent action ──► World   (active inference: ONE closed loop)
```

The width narrows top-to-bottom on purpose: a wide, fast, parallel sensory base **funnels** down
through prediction-error filtering and salience gating into the **narrow, slow, serial** core. The
return path (efferent) closes the loop — sensing and acting are two arcs of one circle, not a
pipeline with a start and an end.

---

## 4. What is load-bearing vs. what is inspiration (intellectual honesty)

Read this before building on the framework — it is the part most likely to be misremembered later.

- **The Free Energy Principle / active inference is the scaffold for Pillars 3–5, and it is
  scientifically *contested* as a falsifiable law** (the "near-tautology" critiques — Colombo &
  Wright; Bruineberg's "Emperor's New Markov Blankets"). This does **not** weaken our framework,
  because we use FEP as an *engineering design pattern*, and its mathematics (free energy ≥ surprise;
  afferent/efferent states as the Markov-blanket interface) is uncontested. **Always present
  FEP-derived principles as "inspired by," never "proven by," biology.**
- **Predictive coding's empirical confirmation in real brains is only modest-to-moderate** (per a
  2023 review); feedforward alternatives remain viable. We borrow the *architecture*, not a settled
  fact about the brain.
- **Every LLM mapping is an analogy we draw**, not a claim the neuroscience makes. The cited science
  describes brains; it does not prescribe AI architecture. (Independent GWT-for-LLM and event-driven
  predictive-coding work corroborates the mappings, which is why we trust them — but the burden of
  the analogy is ours.)
- **Minor:** *precision* (inverse variance) is related to but not identical to *salience*; and
  "allostasis" as a term distinct from homeostasis is itself debated. The operative principles
  ("weight by confidence, not equal-weight all signals" and "anticipate, don't react") survive
  either way.
- **Several spine papers date 1993–2013.** They are *foundational/definitional*, not fast-moving,
  and have been reinforced (not overturned) by 2020–2025 literature.
- **Substrate & LLM-reality corrections (architecture rev 2).** A 5-lens adversarial review tested this
  vision against the real engine and host and corrected several mechanism-level phrasings: the
  *periphery is non-LLM and CPU-bound* (the mind fills the 16 GB GPU, so senses run on spare CPU,
  genuinely parallel because off-GPU); the *context window is not a live workspace* but the immutable
  input to a discrete serial decode — so "broadcast into the workspace" means *batched into the next
  tick's volatile block*, and all competition/selection happens in deterministic glue **before** the
  decode; *no mid-decode interrupt exists* (steering is between-ticks or abort-and-re-prefill); and the
  *buildable-now* forms of Pillars 3–4 are **change-detection** and a **designed telemetry→felt
  transfer function**, with true predictive-coding and interoceptive *inference* as learned-model
  goals. All of this lives in `EIDOS_V3_ARCHITECTURE.md` (§1, §5, §3, §10). **The vision in this
  blueprint stands — those are the mechanisms that make it real on actual hardware.**

---

## 5. The four open design decisions

The biology handed us the architecture but was **silent** on these four. Rev 2 of
`EIDOS_V3_ARCHITECTURE.md` has since resolved or scaffolded most of them; the disposition is noted
under each, with the genuinely-open residue flagged.

### Q1 — Tempo & interrupt arbitration — *resolved (architecture §5)*
The original question imagined "interrupting the core mid-thought." The review settled it: a serial
llama.cpp decode has **no mid-decode interrupt** — steering happens only *between ticks* or by
*aborting and re-prefilling*. So the answer is: admitted senses batch into the next tick by default,
and a genuine emergency triggers an *abort + re-prefill* gated by a salience threshold (because it
throws away partial compute). Tempo is bounded by tick cadence and the measured admits/sec budget, not
a magic duration.

### Q2 — Multimodal fusion into one priority map — *substantially resolved (architecture §1/§6)*
Gemma-4's native A/V→token pathway solves the *transduction* half (every modality becomes tokens, the
core's native language). But that pathway lives *inside* the mind, so it cannot be the always-on
periphery. The resolution: the periphery is **non-LLM, CPU-bound cheap pre-filters** (VAD, frame-diff,
small embeddings) that score salience and decide what's worth escalating *before* paying any
tokenization cost; Gemma's native A/V is the rare **foveal / escalation path** the gate invokes
deliberately. The fusion map's job is to **ration the context budget across modalities by priority** —
the `eufy` event+snapshot instinct, generalized. *Open residue:* the cross-modal salience
**normalization** (making a 0.8 from audio mean the same as a 0.8 from vision) is a scheduled fix
(architecture T6).

### Q3 — The reflex / deliberation boundary — *scaffolded; the criterion is still open*
The *structure* is now in place: reflex arcs are first-class organs, **local to their effectors**
(architecture I9 — the spinal-cord rule), and they fire without the core. What remains genuinely open
is the *decision criterion* — which signals a reflex should handle versus escalate. It should be
**learned/adaptive**, not hand-set, and that learning is future work.

### Q4 — Body re-calibration on migration — *framework in place; online re-learning still open*
The *mechanism* exists: a capability registry + versioned, negotiated interfaces (architecture I8) let
the body schema re-bind per host, and location-transparency (I9) lets the same organs redeploy from
desktop to a Jetson cluster unchanged. What's still open is the *learning* — re-fitting the
interoceptive capacity curves online when the hardware changes (a Jetson's "full RAM" ≠ a
supercomputer's). Nothing in the literature addresses this for a migrating agent; it's a scheduled
learned-model item (architecture §10).

---

## 6. How this dovetails with existing eiDOS doctrine

This framework is not a new direction — it is the **biological bedrock** under the directions Dean
already set:

- **`ARCHITECTURE_PRINCIPLES.md` #1 (event-driven over polled).** Pillar 3 (forward only prediction
  error) is the same idea at the cellular level: neurons *inhibit expected input* and fire on change.
  Delays are guesses; *prediction error* is the event.
- **`ARCHITECTURE_PRINCIPLES.md` #2 (the tick loop must never block).** Pillar 1 says deliberation is
  the scarce serial resource; perception/reflex run in parallel beside it. The core must never be
  frozen doing a sensor's job.
- **The GPU speech-gate** (`gpu_gate.py`, `/api/gpu/wait`) is already a tiny nervous-system reflex:
  an event-released, liveness-bounded wait. V3 generalizes it into the **GPU residency arbiter** (the
  mind, TTS, and occasional escalated perception) — *not* to all senses, which run on spare CPU off the
  GPU entirely (`EIDOS_V3_ARCHITECTURE.md` §1).
- **The self-generated drive (Pillar 4)** is the principled answer to "what should eiDOS do when no
  one is talking to it?" — act on the gap between its felt internal state and its ideal, rather than
  waiting on a tick.

---

## 7. Sources

All primary unless noted. These survived 3-0 adversarial verification.

**Serial/parallel core & global workspace**
- Baars 1993, *In the Theatre of Consciousness / GWT* — [PubMed 8319511](https://pubmed.ncbi.nlm.nih.gov/8319511/)
- Chalmers 2023, *Could a Large Language Model be Conscious?* — [arXiv:2303.07103](https://arxiv.org/abs/2303.07103) *(LLM corroboration)*
- "Theater of Mind" / GWT-on-AI — [arXiv:2604.08206](https://arxiv.org/pdf/2604.08206) *(LLM corroboration)*

**Sensory gating & salience**
- McAlonan & Brown 2002, *The thalamic reticular nucleus / attentional gate* — [Sage 10.1177/107385840200800405](https://journals.sagepub.com/doi/10.1177/107385840200800405)
- Wolff et al. 2020/21, *A thalamic bridge from sensory perception to cognition* — [ScienceDirect S0149763420306473](https://www.sciencedirect.com/science/article/pii/S0149763420306473)
- White, Kan, Levy, Itti, Munoz, Hafed 2017, *Superior colliculus saliency map* — [PNAS 10.1073/pnas.1701003114](https://www.pnas.org/doi/10.1073/pnas.1701003114)

**Predictive processing & free energy**
- Friston 2010, *The free-energy principle: a unified brain theory?* — [Nat Rev Neurosci nrn2787](https://www.nature.com/articles/nrn2787)
- Mazzaglia et al. 2022, *The free energy principle for perception and action: a deep-learning perspective* — [arXiv:2207.06415](https://arxiv.org/pdf/2207.06415)
- Kirchhoff, Parr, Palacios, Friston, Kiverstein 2018, *The Markov blankets of life* — [PMC5805980](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5805980/)

**Interoception, allostasis & homeostatic drive**
- Seth 2013, *Interoceptive inference, emotion, and the embodied self* — [ScienceDirect S1364661313002118](https://www.sciencedirect.com/science/article/pii/S1364661313002118)
- Sennesh, Theriault, Brooks, van de Meent, Barrett, Quigley 2021, *Interoception as modeling, allostasis as control* — [PMC9270659](https://pmc.ncbi.nlm.nih.gov/articles/PMC9270659/)
- Laurençon, Ségerie, Lussange, Gutkin 2021, *Continuous homeostatic reinforcement learning* — [arXiv:2109.06580](https://arxiv.org/abs/2109.06580)
- Keramati & Gutkin 2014, *Homeostatic reinforcement learning* — [eLife 04811](https://elifesciences.org/articles/04811)

**Cognitive architectures (prior art on bridging fast perception → slow deliberation)**
- Franklin et al., *LIDA — a brief account of the LIDA model of cognition* — [CCRG/Memphis PDF](https://ccrg.cs.memphis.edu/tutorial/mindAccordingToLIDA/Brief-Account.pdf)
- *Three-layer architecture* (robotics reactive/executive/deliberative) — [Wikipedia](https://en.wikipedia.org/wiki/Three-layer_architecture) *(secondary)*

---

## 8. Provenance

Produced by the `deep-research` workflow (fan-out web search → dedup/fetch → 3-vote adversarial
verification → synthesis). 5 search angles → 25 sources fetched → 110 falsifiable claims extracted →
top 25 verified → **25/25 confirmed 3-0** → 12 synthesized findings, all high-confidence. The first
run's verification phase was lost to a monthly-spend-limit failure (every verifier abstained,
producing a false "all refuted"); a resume re-ran only the verifiers and the synthesis against the
cached, intact claim set. Full session: 2026-06-19. Memory pointer: `eidos_biomimetic_nervous_system`.
