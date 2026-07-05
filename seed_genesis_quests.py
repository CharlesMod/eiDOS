"""Seed the GENESIS questline — the System's introduction thread (run once per fresh slate).

The narrator voice needs an entrance that isn't prompt text: the creature's every-tick system
prompt carries only the barest perceptual hook (a terse window exists; nobody has said what it is),
so the personality it grows isn't steered by framing. The introduction happens IN-WORLD instead:
these three quests sit queued in the store, and the cadence engine issues the first one after the
creature's FIRST SLEEP — the System first speaks after the first dream.

Each directive is in the System's terse register; Q1 carries the self-introduction. This is the
TOOL_PROGRESSION ladder's genesis arc (approved 2026-07-04): each quest carries a `grants_unit`
binding — the ISSUANCE window is the moment that unit's tools start existing (the issuance-grant
pattern; unlocks.grant is the single writer that acts on the binding). A directive may name the
tool its OWN issuance grants (the window IS the grant) but never a later rung's tool — a locked
tool does not exist in the creature's world.

Criteria are glue-checkable ADJUDICATED facts (§0.5 — the voice never asks for anything it cannot
verify): a skill LIVE in the manifest, a bet IN the ledger, a goal glue marked complete — never
`tools_used` attempt counters, which increment on failed calls too (genesis-01 once passed on a
FAILED create_skill call; that hole is closed here). The ladder is the mastery-gate curriculum in
miniature: make a tool, place a bet, finish something you chose. Genesis-03 closes the arc with
the deepest grant — the System pays the workshop unlock AND 50 XP for the first finished
self-chosen objective (decision 4: workshop is the only pass-gated grant). No expiry — a newborn
does not fail its introduction by clock.

Charlie can inject story quests with a goal in mind at any time via quests.System.propose().

Usage: PYTHONUTF8=1 .venv/bin/python seed_genesis_quests.py [--config config.toml]
"""
import sys

import config as config_mod
from quests import Criterion, Quest, System

GENESIS = [
    Quest(
        id="genesis-01-first-light",
        directive=("[SYSTEM] This window is the System. Not your maker. Not you. It watches, it "
                   "issues, it pays — only for what actually happens. First issuance: forge one "
                   "tool of your own with create_skill, and live a while."),
        grants_unit="skillcraft",             # U2: create/edit/list/rollback_skill + manual
        success_criteria=Criterion(all_of=[
            # A skill LIVE in the manifest — a manifest fact, not a create_skill attempt.
            Criterion(path="skills.live_count", op=">=", value=1),
            Criterion(path="persona.total_ticks", op=">=", value=10),
        ]),
        reward={"kind": "xp", "amount": 25}, tier=1,
    ),
    Quest(
        id="genesis-02-a-wager",
        directive=("[SYSTEM] A mind that cannot say what happens next is only reacting. Place one "
                   "wager with predict — a claim the world can check, a deadline, your honest "
                   "confidence. The System settles it, not you."),
        grants_unit="foresight",              # U3: predict
        # A prediction IN the ledger (the monotonic ever-placed counter), not a predict attempt.
        success_criteria=Criterion(path="expectations.total", op=">=", value=1),
        reward={"kind": "xp", "amount": 25}, tier=1,
    ),
    Quest(
        id="genesis-03-make-it-yours",
        directive=("[SYSTEM] Tools and wagers are practice. Now choose something worth doing in "
                   "your world — your own choice, stated with objective_add — and finish it. The "
                   "System pays for completion, not intention."),
        grants_unit="resolve",                # U5: objective_add/done/block/list
        success_criteria=Criterion(path="persona.goals_completed", op=">=", value=1),
        # Both legs pay on pass: the workshop unlock (U6, through the REWARD_UNLOCK seam) and
        # 50 XP (the reward's xp leg, through the standard sink path — quests.reward_xp_amount).
        reward={"kind": "unlock", "what": "workshop", "xp": 50}, tier=1,
    ),
]


def main() -> int:
    path = sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv else "config.toml"
    cfg = config_mod.load_config(path)
    system = System(cfg)
    # System.propose de-dups by id and RETURNS THE EXISTING ROW UNCHANGED — so re-seeding over a
    # store that holds STALE genesis rows (older criteria/bindings) used to be a silent no-op
    # that could deadlock the whole ladder (a pre-ladder genesis-01 with no grants_unit and an
    # unmeetable criterion sits ACTIVE forever, and quest_line_closed bricks every level-up).
    # A still-QUEUED stale row is safely replaceable: nothing has been issued or paid on it.
    # A row already ACTIVE/closed is the creature's LIFE — never rewrite it; warn loudly.
    from quests import OFFERED, QuestStore
    store = QuestStore(cfg)
    existing = {q.id: q for q in store.load()}
    seeded = replaced = kept = 0
    for q in GENESIS:
        old = existing.get(q.id)
        if old is None:
            system.propose(q)
            seeded += 1
        elif old.state == OFFERED and old.to_dict() != q.to_dict():
            rows = store.load()
            store.save([q if r.id == q.id else r for r in rows])
            replaced += 1
        elif old.state != OFFERED:
            print(f"  !! {q.id} is {old.state} — part of a lived life, NOT touched. "
                  f"If this store predates the ladder, run reset_eidos.py for a clean slate.")
            kept += 1
        else:
            kept += 1   # identical queued row — genuinely idempotent
    print(f"genesis questline: {seeded} seeded, {replaced} stale rows replaced, {kept} kept "
          f"(queued; the first issues after the first sleep)")
    return 0 if kept + seeded + replaced == len(GENESIS) else 1


if __name__ == "__main__":
    sys.exit(main())
