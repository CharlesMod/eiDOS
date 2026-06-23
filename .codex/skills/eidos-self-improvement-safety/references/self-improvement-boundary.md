# Self-Improvement Boundary

Use this reference for `eidos-self-improvement-safety`.

## Core Boundary

eiDOS proposes; the operator-controlled dashboard applies. The shipped posture
is accident-safety, not adversary-proofing.

The dashboard/supervisor owns privileged actions:

- validating proposal paths and content
- showing diffs
- applying changes
- making git checkpoints
- restoring last-good state
- restarting the child process
- enforcing protected paths

eiDOS and authored skills may write proposals or runtime state only through
sanctioned tools.

## Protected Surfaces

Treat these as safety-sensitive:

- `dashboard.py`
- `git_safety.py`
- `selfedit.py`
- `safety.py`
- `atomicio.py`
- `config.py`
- `config.toml`
- `.gitignore`
- `llm.py`
- `skills.py`

Config may narrow protections, not casually widen them.

## Proposal Apply Requirements

- Canonicalize target paths against repo root.
- Reject absolute paths, traversal, drive-letter/UNC paths, protected files,
  symlinks when relevant, and stale base revisions.
- Compile/import/smoke before applying when the target can affect boot.
- Checkpoint before write.
- Write pending markers before risky copy when health probes depend on them.
- Restart through the dashboard-owned path.
- Boot paused on operator apply/restore restarts.
- Roll back on failed health probe or crash loop.

## Skill Creation And Promotion

Runtime-authored eiDOS skills are not the same as Codex skills. For eiDOS
runtime skills:

- Authoring alone is not success.
- Compile success is not runtime success.
- Imports such as unavailable `requests` must be caught before promotion.
- A skill keeps standing through successful real use.
- Repeated failure should demote or quarantine, not remain callable forever.

## Live Service Notes

The dashboard supervises the tick-loop child. Voice is separate. The local model
and voice can contend for GPU residency. Tests should use temporary workspaces
and ports unless the task explicitly requires live service proof.

## Refusal Tests To Prefer

- agent-facing tool cannot write protected source
- proposal cannot self-approve
- stale proposal is rejected
- malformed or unsafe target path is rejected
- restore excludes protected files and preserves unrelated dirt
- unauthorized state-changing POST is rejected
- broken authored skill does not remain promoted
