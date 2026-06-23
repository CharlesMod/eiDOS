# Capability Surface Reference

Use this reference for `eidos-tool-capability-design`.

## Capability Surfaces

- **Built-in tools** in `tools.py`: directly callable action primitives.
- **Atoms** in `skill_atoms.py`: reliable vocabulary for authored skills.
- **Runtime-authored skills** in `skills.py`: composed capabilities that eiDOS
  can create, version, validate, and reuse.
- **Delegates** in `delegate.py`: background coding/research jobs.
- **Manuals and self-knowledge** in `OPERATING_MANUAL.md` and
  `eidos_capabilities.md`: what eiDOS should know exists.

## Design Priorities

1. Expose reliable atoms for frequent needs instead of letting authored skills
   reinvent brittle imports or shell commands.
2. Validate before execution. Prefer typed failure and repair guidance over raw
   exception blobs.
3. Keep tool and skill documentation aligned with runtime reality.
4. Reward runtime success and downstream reuse, not authoring or compile-only
   success.
5. Keep long or slow work asynchronous by default.

## Skill-Language Pattern

The reliable built-in tools are the vocabulary. Authored skills should compose
known-good atoms and other proven skills rather than importing arbitrary
libraries for common operations.

Healthy growth:

```text
tested atoms -> short compositions -> repeated successful use -> promoted capability
```

Unhealthy growth:

```text
compile passes -> runtime import fails -> dead skill remains callable
```

## Common Walls To Prevent

- Missing `requests` import when an HTTP atom should be used.
- Windows path or shell quoting mistakes repeated in skill code.
- Async commands that hide failure status.
- Skill names that shadow built-ins or collide with reserved names.
- Skills with zero successful runtime uses staying in the active library.

## HTTP Atom Pattern

When authored skills need HTTP, expose reliable atoms rather than relying on
third-party imports inside skill code:

```text
http_get(url, headers?, timeout?, save?)
http_post(url, json?, data?, headers?, timeout?, save?)
http_request(method, url, headers?, json?, data?, timeout?, save?)
```

Atoms should return typed, non-throwing results:

```text
{ok, status, headers, text, json, saved_path, error_kind, error}
```

Timeouts should be defaulted or mandatory at the atom layer. If a skill needs
HTTP behavior the atom does not cover, extend the atom/tool contract rather than
letting the skill import a new dependency.

## Validation And Repair

AST validation should reject unavailable or disallowed imports such as
`import requests` and `from requests import ...`, including imports inside
functions or branches. Return a typed validation failure such as
`SKILL_IMPORT_DISALLOWED` or `MISSING_RUNTIME_DEPENDENCY` with repair guidance:
use the HTTP atoms instead.

Dry-run/import checks remain useful, but they are a second line of defense.
They must run with the same atom namespace the skill will get at runtime.
Repeated runtime dependency failures should quarantine or demote the skill.

## Verification Patterns

- Unit test input validation and typed failure classes.
- Run a skill in the real runtime environment before promotion.
- Prove a repair path gives a working alternative, not just a rejection.
- Confirm `OPERATING_MANUAL.md` and `eidos_capabilities.md` mention the
  capability only after it exists.
- Track invocation success and demote repeated failures.
