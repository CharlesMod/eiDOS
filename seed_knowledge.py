"""Seed eiDOS's long-term knowledge store with bootstrapping self-knowledge.

The curated nuggets themselves live in **preserved_nuggets.toml** — the small,
hand-edited database of "what eiDOS should ALWAYS know" (its identity, the
infrastructure, hard-won lessons, the operating-manual pointer). This module is
just the loader: it reads that TOML and writes each nugget into the knowledge
store, tagged as a bootstrap seed. Edit the TOML to change what a fresh eiDOS
starts knowing; you don't touch this file.

Run after a workspace reset:  python seed_knowledge.py
"""

import os
import sys

KDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KDIR)

from config import load_config  # noqa: E402
import knowledge  # noqa: E402

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

PRESERVED_PATH = os.path.join(KDIR, "preserved_nuggets.toml")
# Machine-local nuggets that must NEVER enter git (device credentials, keys). Same format,
# gitignored; absent on a fresh clone, which is fine.
LOCAL_PATH = os.path.join(KDIR, "preserved_nuggets.local.toml")


def load_nuggets(path: str = PRESERVED_PATH, optional: bool = False):
    """Load a preserved nuggets database -> list of (category, tags, content).

    Degrades gracefully: returns [] (with a printed warning, unless `optional`) if the
    file is missing or unparseable, so a reset can't crash on a bad edit — the operator
    just sees "seeded 0/0" and investigates.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if not optional:
            print(f"  ! preserved nuggets database not found: {path} (seeding nothing)")
        return []
    except Exception as e:  # noqa: BLE001 - corrupt TOML, etc.
        print(f"  ! failed to parse {path}: {e} (seeding nothing)")
        return []
    out = []
    for n in data.get("nugget", []):
        content = (n.get("content") or "").strip()
        if not content:
            continue
        out.append((n.get("category", "facts"), list(n.get("tags", [])), content))
    return out


# Curated bootstrap knowledge, loaded from preserved_nuggets.toml at import time.
NUGGETS = load_nuggets()


def main():
    cfg = load_config(os.path.join(KDIR, "config.toml"))
    # re-read fresh so edits take effect immediately; local (gitignored, secrets) seeds too
    nuggets = load_nuggets() + load_nuggets(LOCAL_PATH, optional=True)
    n = 0
    for cat, tags, content in nuggets:
        try:
            # Mark as a bootstrap seed so the context layer can tell seeds (rarely need surfacing)
            # apart from LEARNED facts (the agent's own discoveries, always surfaced in the world model).
            knowledge.store_entry(cfg, content, tags, cat, source_goal="seed")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! failed ({cat} {tags}): {e}")
    print(f"seeded {n}/{len(nuggets)} knowledge nuggets into {cfg.knowledge_dir}")


if __name__ == "__main__":
    main()
