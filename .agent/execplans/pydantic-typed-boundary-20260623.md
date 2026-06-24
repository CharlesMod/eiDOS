# Pydantic Typed Boundary Migration

## Objective

Expand eiDOS's Pydantic typed-boundary coverage across the five requested
boundary classes while preserving the flat Python app shape and Nix-first
runtime:

1. config/TOML/env validation
2. remaining built-in tool arguments
3. dashboard/API POST payloads
4. durable JSON state records
5. generated or checked config/schema documentation

Malformed or malicious external inputs should fail closed with clear errors.
Internal hot paths stay as dataclasses, dicts, and existing modules unless they
are directly reading or writing a boundary.

## Constraints

- Keep the flat module layout; do not move code into `src/` or package eiDOS.
- Keep Nix as dependency authority; no venv, pip, uv sync, npm, or curl hooks.
- Keep Pydantic at ingress/egress boundaries; do not remodel the nervous
  tick-loop or `nervous/event.py`.
- Preserve existing valid behavior and durable file formats, including legacy
  observation tick sentinels such as `"compaction"`.
- Keep sabotage as controlled test-time evidence only; final tree must contain
  the intact guards.

## Implementation Notes

- Added `typed_boundary.py` as the shared Pydantic boundary module for config
  documents, env overrides, resolved runtime sanity checks, dashboard POST
  payloads, observation/chat/job records, and schema generation.
- `config.load_config()` now validates merged TOML, uses `pydantic-settings`
  for `EIDOS_*` env overrides, and validates the resolved config after path
  normalization.
- `tools.execute_tool()` now validates every built-in tool name through a
  Pydantic argument model before dispatch. Existing pilot guards remain inside
  the high-risk direct tool functions.
- `dashboard.py` POST endpoints now use typed payload models instead of ad hoc
  `json.loads(...).get(...)` parsing for state-changing JSON bodies.
- `memory.py` validates observation and chat reply records on write/readback.
  `tools._read_jobs()` and `_write_jobs()` validate the jobs ledger as a typed
  durable record list.
- Added `scripts/check_boundary_schemas.py` and
  `docs/boundary-schemas.json`; `nix flake check` includes a
  `boundary-schema-docs` derivation that fails if the artifact drifts from the
  Pydantic source.

## Verification Log

- Passed focused boundary suite:
  `nix develop --command python -m pytest tests/test_config.py tests/test_dashboard_post_boundaries.py tests/test_pydantic_tool_args.py tests/test_memory.py tests/test_boundary_schema_docs.py -q`
  (`87 passed, 3 subtests passed`).
- Passed full offline suite:
  `nix develop --command python -m pytest -q -m "not slow and not live"`
  (`953 passed, 19 skipped, 29 deselected, 3 subtests passed`).
- Passed final flake check:
  `nix flake check --print-build-logs`
  (4 checks: Claude smoke, import smoke, boundary schema docs, offline tests).
- Passed explicit import smoke:
  `nix develop --command python - <<'PY' ... PY`
  importing `pydantic`, `pydantic_settings`, `config`, `dashboard`, `memory`,
  `tools`, and `typed_boundary` (`imports-ok 2.12.5`).
- Passed dashboard runtime smoke:
  `nix run .#dashboard -- --config <tmp>/config.toml --port <tmp-port>`
  with a temporary workspace; `/api/status` responded successfully.

## Sabotage Log

- Config/TOML guard: temporarily widened `[dashboard].port` upper bound in
  `DashboardConfigSection`; `test_rejects_invalid_dashboard_port` failed.
  Restored the bound and the test passed.
- Dashboard POST guard: temporarily changed `_StrictBoundaryModel` from
  `extra="forbid"` to `extra="ignore"`; `test_chat_payload_rejects_extra_field`
  failed. Restored strict extras and the test passed.
- Built-in tool arg guard: temporarily changed `_ToolArgs` from
  `extra="forbid"` to `extra="ignore"`; the `update_plan` extra-field
  side-effect test failed because the call succeeded. Restored strict extras
  and the test passed.
- Durable JSON state guard: temporarily widened `JobRecord.status` from a
  literal status set to `str`; `test_jobs_ledger_rejects_bad_status_on_write`
  failed. Restored the literal status set and the test passed.
- Schema-doc guard: temporarily changed the generated schema bundle string
  without regenerating `docs/boundary-schemas.json`;
  `test_boundary_schema_docs_are_current` failed with the stale-schema error.
  Restored the bundle string and the test passed.
