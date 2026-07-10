# The Commission — a standing order the creature works between check-ins

**Approved:** Charlie, 2026-07-10 ("build a game according to xyz spec, then improve it as best you
can ad infinitum; the user checks in now and then to playtest and give feedback, and the LLM keeps
crunching"). Naming: *commission* — `missions.py` already belongs to Phase 7 generals.

## What it is

A **commission** is a long-horizon work order that lives ABOVE the objective gate. Objectives
rotate, park, and archive on the scale of hours; a commission persists across all of that — across
sleeps, level-ups, and restarts — and the creature's own task list is how it keeps itself on track
between the operator's visits.

Three surfaces, three ownership rules:

| Surface | Path | Writer | Reader |
|---|---|---|---|
| **Brief** (the spec) | `workspace/commission/brief.md` | **operator only** (outside the creature's home — read-only *by placement*, not by honor) | injected into context every tick, char-capped |
| **Notes** (free scratch) | `workspace/home/commission_notes.md` | **creature** (its existing read/write file tools — no new plumbing) | itself; the operator can peek any time |
| **Todo** (typed tasks) | `workspace/commission/commission.json` | **the engine** (eidos process, single writer), via two creature tools | rendered as a standing TODO block in context; summarized on `/api/status` |

## The honesty rule (the same one everything else obeys)

The creature marking its own task done is a **claim, not a settlement**. A task pays out only when
one of two ground truths confirms it:

1. **A checkable claim** attached at `commission_add` time, in the expectation ledger's existing
   claim vocabulary (`exists:<relpath>`, `not_exists:<relpath>`, `<stat.path> <op> <number>`) —
   settled by glue, no operator needed. A claim that doesn't parse is rejected at the tool
   boundary (the Administrator-criteria lesson: an ungradeable promise must be unrepresentable).
2. **An operator verdict**, given in the normal chat box: `/commission done 3 nice work` (or
   `reject 3 <why>` / `drop 3`). Prose without the slash prefix flows to the creature as ordinary
   coworker chat, exactly as today. A rejection reopens the task and carries the note back to the
   creature through the System's window — that IS the playtest-feedback loop.

Verdicts cross the process boundary as one-file-per-verdict in `workspace/commission/verdicts/`
(dashboard writes, engine consumes-and-deletes) — the interventions pattern, no shared-file races.

## The incentive (what the creature innately wants)

A confirmed task pays through the currencies the creature already lives on:

- **XP** (`COMMISSION_XP_CONFIRMED`) — above skill-reuse pay, below a genesis quest: steady
  commissioned work outearns tinkering but never outearns growth milestones.
- **Energy** (`COMMISSION_FEED` into the metabolism reserve) — the same reserve skill-authoring
  *spends* from, closing an economic loop: building tools for the commission costs energy;
  confirmed commission progress feeds it back. Work earns food.
- **The felt moment** — a `[SYSTEM] COMMISSION TASK CONFIRMED — PAID …` window in the observation
  stream (the reward learner reads outcomes from lived turns, so the dopamine loop needs no extra
  wiring) plus a news ingest so a returning operator sees what was accomplished.

Nothing pays on `done_claimed` alone. Rejected tasks pay nothing and return to `open`.

## Deliberately deferred

- Administrator integration: the dossier reading commission state and gap-mining the next quest
  *from the brief* — the decomposition engine. (The context block gets the creature surprisingly
  far; do this when the todo proves too shallow.)
- `commission.confirmed_total` joining `quests.ADJUDICATABLE_PATHS` so quests/bets can reference
  commission progress.
- Goal-tension coupling (open commission tasks as incompletion pressure).
- A dashboard panel (the chat command + status payload cover v1).

## The horizon this points at

Charlie's stated end-state: *an LLM-in-a-box, loaded with an eiDOS consciousness raised to a
minimum competency level, frozen, deployed, and unfrozen awaiting standing orders.* The pieces
this plan assumes and the ladder already builds toward: maturity = the mastery-gate tiers (a
deployable creature is one past defined gates), the consciousness snapshot = workspace +
`preserved_nuggets.toml` (the letter to the next self), deployment = `fresh_slate.sh`'s inverse
(load, don't wipe), and the standing order = this commission system. When a Level-N creature can
carry a commission unattended for days, the box is mostly packaging.
