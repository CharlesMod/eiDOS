"""Seed the GENESIS questline — the System's introduction thread (run once per fresh slate).

The narrator voice needs an entrance that isn't prompt text: the creature's every-tick system
prompt carries only the barest perceptual hook (a terse window exists; nobody has said what it is),
so the personality it grows isn't steered by framing. The introduction happens IN-WORLD instead:
these three quests sit queued in the store, and the cadence engine issues the first one after the
creature's FIRST SLEEP — the System first speaks after the first dream.

Each directive is in the System's terse register; Q1 carries the self-introduction. Criteria are
glue-checkable persona counters (§0.5 — the voice never asks for anything it cannot verify), and
the ladder is the mastery-gate curriculum in miniature: make a tool, place a bet, finish something
you chose. No expiry — a newborn does not fail its introduction by clock.

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
        success_criteria=Criterion(all_of=[
            Criterion(path="persona.tools_used.create_skill", op=">=", value=1),
            Criterion(path="persona.total_ticks", op=">=", value=10),
        ]),
        reward={"kind": "xp", "amount": 25}, tier=1,
    ),
    Quest(
        id="genesis-02-a-wager",
        directive=("[SYSTEM] A mind that cannot say what happens next is only reacting. Place one "
                   "wager with predict — a real expectation, a deadline, your honest confidence. "
                   "The System settles it, not you."),
        success_criteria=Criterion(path="persona.tools_used.predict", op=">=", value=1),
        reward={"kind": "xp", "amount": 25}, tier=1,
    ),
    Quest(
        id="genesis-03-make-it-yours",
        directive=("[SYSTEM] Tools and wagers are practice. Now choose something worth doing in "
                   "your world — your own choice, stated as an objective — and finish it. The "
                   "System pays for completion, not intention."),
        success_criteria=Criterion(path="persona.goals_completed", op=">=", value=1),
        reward={"kind": "xp", "amount": 50}, tier=1,
    ),
]


def main() -> int:
    path = sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv else "config.toml"
    cfg = config_mod.load_config(path)
    system = System(cfg)
    for q in GENESIS:
        system.propose(q)
    print(f"seeded {len(GENESIS)} genesis quests (queued; the first issues after the first sleep)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
