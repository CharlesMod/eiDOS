# Pydantic Typed Boundary Slice

## Objective

Migrate eiDOS's first typed-boundary slice to Pydantic while preserving the flat
Python app shape and Nix-first runtime. The selected slice is tool argument
ingress for high-risk built-in tools, where untrusted LLM-produced dictionaries
cross into filesystem, shell, background-job, and HTTP behavior.

## Constraints

- Keep the flat module layout; do not move code into `src/` or package eiDOS.
- Keep Nix as dependency authority; no venv, pip, uv sync, npm, or curl hooks.
- Do not rewrite `nervous/event.py`; its stdlib wire contract stays intact.
- Keep sabotage as controlled test-time evidence only; final tree must contain
  the intact guards.

## Implementation Notes

- Use Pydantic models to validate and normalize arguments for:
  - `bash`
  - `write_file`
  - `read_file`
  - `bg_run`
  - `bg_check`
  - `http_request`
- Preserve existing valid-call behavior and failure taxonomy.
- Prefer clear `fail_kind="args"` failures for malformed arguments.

## Verification Log

- Passed: focused Pydantic guard tests via
  `nix develop --command python -m pytest tests/test_pydantic_tool_args.py tests/test_failkind.py::TestFailKind::test_bash_missing_cmd_is_args tests/test_tools.py::TestTools::test_bash_simple tests/test_tools.py::TestTools::test_write_file tests/test_tools.py::TestTools::test_bg_run_success -q`
  (`10 passed in 0.23s`).
- Passed: sabotage failure proof. Temporarily changed `_ToolArgs.model_config`
  from `extra="forbid"` to `extra="ignore"` and ran
  `tests/test_pydantic_tool_args.py::TestPydanticToolArgs::test_bash_rejects_extra_field_without_running`;
  the test failed because the command executed. Restored `extra="forbid"` and
  reran the same test; it passed.
- Passed: `nix flake check --print-build-logs` (`929 passed, 19 skipped,
  29 deselected in 14.17s`).
- Passed: `nix develop` import smoke for core deps, `pydantic`,
  `pydantic_settings`, and touched modules (`tools`, `dashboard`, `eidos`,
  `embedding`, `nervous`).
- Passed: `nix run .#dashboard` throwaway-port smoke. Dashboard responded from
  `/api/status` on a temporary port with a temporary workspace.
