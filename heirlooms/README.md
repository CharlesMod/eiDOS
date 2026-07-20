# heirlooms/ — the creature lineage's bookshelf

Each file here is a **retirement volume**: when a creature is retired (`scripts/fresh_slate.sh`),
`legacy.py` distills its REPLAY-VALIDATED corpus — the strategy guardrails and procedures that
earned their keep, its scars (hard-won failure lessons), high-utility facts, and its reflex
registry — into a versioned `heirlooms/<creature-name>-<YYYYMMDD>.jsonl` BEFORE the wipe.

The next newborn seeds from `preserved_nuggets.toml` (Charlie's letter) **plus the latest heirloom
volume** (`seed_knowledge.py`). Every inherited item is stamped provenance `inherited`,
strength-discounted (the told/inherited floor), and decays faster until this new body VERIFIES it
for itself (an inherited claim it can't confirm is a rumor). Inherited reflexes arrive **disarmed,
as proposals** — a new body must re-earn its own automation (WIS1 across generations).

This is how **the species gets smarter when the individual resets** (WISDOM_PLAN §6). These files
are committed lineage — do NOT gitignore them. They are the bookshelf a new mind is born beside.

## Format
JSON Lines. The FIRST line is a header:

    {"header": {"creature": "...", "birth_ts": "...", "retire_ts": "...", "level": N,
                "goals_completed": N, "quests_passed": N, "heirloom_version": 1,
                "record_count": N}}

Each subsequent line is one heirloom record:

    {"kind": "strategy|procedure|fact|error|reflex",
     "body": "...",
     "provenance_chain": {"ancestor": "...", "original_provenance": "experienced|told|inherited"},
     "stats_summary": {"strength": ..., "credit_sum": ..., "replay_learned": ...},
     "exported_ts": "..."}
