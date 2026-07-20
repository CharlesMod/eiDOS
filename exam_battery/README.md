# exam_battery — the frozen house-domain battery (WISDOM_PLAN §4)

The experience-curve instrument's fixed question set. Each `vN.jsonl` file is a
**versioned, immutable** battery: once results referencing a version are written to
`state/wisdom_curve.jsonl`, that file's bytes MUST NOT change (score comparability across
the curve). Additions or edits create a NEW version file (`v2.jsonl`), never a mutation of a
published one. `wisdom_curve.py` records the SHA-256 of every battery it uses and refuses to
run a version whose bytes have drifted from what previous results were scored against.

## File format — JSONL (one task per line)

Each line is a JSON object:

```json
{
  "id": "ops.model_port",                 // unique within the battery
  "domain": "ops",                        // ops | subsystems | recall | procedure
  "prompt": "…the question posed to the model…",
  "scorer": {
    "type": "exact | regex | claim | action_signature",
    ...type-specific fields...
  }
}
```

### Scorer types (all MECHANICAL — no model judges another model)

- **`exact`** — normalized exact match. Fields: `answer` (str) or `answers` (list of
  acceptable strings). Normalization: casefold, collapse whitespace, strip surrounding
  punctuation/quotes. Scores 1.0 on any match, else 0.0.
- **`regex`** — the model's answer must contain a match for `pattern` (Python `re`, case-
  insensitive by default; pass `flags` chars like `"s"`/`"m"` to add). Scores 1.0 / 0.0.
- **`claim`** — a checkable claim in the `quests.ADJUDICATABLE_PATHS` spirit: the answer must
  assert specific facts. Fields: `must_include` (list of substrings, ALL required, case-
  insensitive) and optional `must_exclude` (list, NONE may appear). Score = fraction of
  `must_include` present (partial credit), zeroed if any `must_exclude` appears.
- **`action_signature`** — the answer must contain a tool call whose tool name matches
  `tool`; parsed with `parser.parse_tool_call`. Optional `args_pattern` (regex over the raw
  call text) tightens it. Scores 1.0 / 0.0.

The four domains (§4): `ops` = this machine's operations (RUNTIME_SPRINTER facts);
`subsystems` = the creature's own capabilities (eidos_capabilities.md); `recall` = situational
recall-dependent calls; `procedure` = procedure recollection (the right sequence / the right
tool for a named job).

Sources of truth the answers are drawn from: `RUNTIME_SPRINTER.md`, `eidos_capabilities.md`,
`CLAUDE.md`, `config.toml`. If those change, the OLD battery answers do not — a battery is a
snapshot of what was true when it was frozen; that is the point of versioning.
