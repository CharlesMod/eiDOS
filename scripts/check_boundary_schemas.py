#!/usr/bin/env python3
"""Generate or check committed Pydantic boundary schemas."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "boundary-schemas.json"

sys.path.insert(0, str(ROOT))

from typed_boundary import boundary_schema_bundle  # noqa: E402


def _render() -> str:
    return json.dumps(boundary_schema_bundle(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    rendered = _render()
    if "--write" in argv:
        SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {SCHEMA_PATH.relative_to(ROOT)}")
        return 0
    try:
        current = SCHEMA_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"{SCHEMA_PATH.relative_to(ROOT)} is missing; run {Path(__file__).name} --write", file=sys.stderr)
        return 1
    if current != rendered:
        print(f"{SCHEMA_PATH.relative_to(ROOT)} is stale; run {Path(__file__).name} --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
