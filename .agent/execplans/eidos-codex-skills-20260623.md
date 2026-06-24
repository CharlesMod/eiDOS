# eiDOS Codex Skill Set

This ExecPlan is a living document. Maintain it according to `.agent/PLANS.md`.

## Purpose / Big Picture

Create a small, practical set of Codex skills for agents working on eiDOS so
they can apply the repository's design philosophy without loading every long
doctrine document into context. The result should help future agents decide and
build in the eiDOS style: substrate-honest, boundary-first, observable,
biomimetic only where the analogy is honest, and safe around live services and
self-improvement.

The visible outcome is a committed `.codex/skills/` scaffold in this repository,
with concise `SKILL.md` files and progressively loaded `references/` distilled
from the eiDOS docs.

## Progress

- [x] (2026-06-23 22:47Z) Started from clean branch
  `codex/eidos-agent-docs` at commit `2e60c69`, which already contains
  `AGENTS.md`, `.agent/PLANS.md`, and `.agent/execplans/README.md`.
- [x] (2026-06-23 22:47Z) Read the required `skill-creator` instructions,
  `tradeoff-decision` skill, and the dev workspace tradeoff framework.
- [x] (2026-06-23 22:47Z) Confirmed the official skill helper scripts live at
  `/agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/`.
- [x] (2026-06-23 22:47Z) Confirmed host Python cannot currently run
  `quick_validate.py` because `yaml` is not installed; validation must use a
  Python environment with PyYAML or an explicit fallback check.
- [x] (2026-06-23 22:55Z) Incorporated subagent boundary review: tightened
  trigger language, renamed broad loop/context skills, and added a narrow
  positive capability-design skill.
- [x] (2026-06-23 22:59Z) Scaffolded six repo-local Codex skills under
  `.codex/skills/` with `SKILL.md`, `agents/openai.yaml`, and one reference
  file each.
- [x] (2026-06-23 23:00Z) Ran official `quick_validate.py` for all six skills
  through a temporary `/tmp` venv with PyYAML; all six reported
  `Skill is valid!`.
- [x] (2026-06-23 23:04Z) Reviewed forward-test subagent results. The organism
  loop and tool-capability tests passed without code changes; the
  nervous-system test suggested adding a compact vision/motion example to the
  V3 reference, which is now incorporated.
- [ ] Commit the final skill set and record clean status evidence.

## Surprises & Discoveries

- Observation: The skill validation script is present but cannot run under host
  Python.
  Evidence: `python .../quick_validate.py --help` failed with
  `ModuleNotFoundError: No module named 'yaml'`.
- Observation: Official validation succeeds when run from a temporary validator
  venv with PyYAML.
  Evidence: `python -m venv /tmp/eidos-skill-validate-venv`, install PyYAML,
  then `quick_validate.py` over each `.codex/skills/*` folder returned six
  `Skill is valid!` lines.
- Observation: Forward-tests used the skills to reject three expected bad
  designs: self-declared curiosity as energy, raw webcam frame summaries in the
  prompt, and prompt reminders in place of HTTP atoms/validation.
  Evidence: subagents returned corrected designs preserving literal power
  energy, receptor/filter/change/salience/admission placement, and atom plus
  AST-validation capability design.

## Decision Log

- Decision: Build on the existing `codex/eidos-agent-docs` branch instead of
  creating a second branch.
  Rationale: The branch is distinct, clean, and already contains the repo-level
  agent/ExecPlan scaffold this work extends.
  Date/Author: 2026-06-23 / Codex

- Decision: Use repo-local `.codex/skills/` as the skill location.
  Rationale: The user's objective asks for committed skill scaffolds on the
  eiDOS docs branch, not installation into the current operator's global
  `$CODEX_HOME`.
  Date/Author: 2026-06-23 / Codex

- Decision: Keep six skills rather than the stricter five-skill set one review
  recommended.
  Rationale: The five core doctrine skills cover decisions, nervous-system
  design, organism loops, tick context/agency, and self-improvement safety.
  A separate `eidos-tool-capability-design` skill is still warranted because
  positive design of tools, atoms, delegates, manuals, and capability surfaces
  has different success criteria from mutation safety. Its trigger is narrow so
  it does not become a generic "tools" lore dump.
  Date/Author: 2026-06-23 / Codex

- Decision: Rename the loop skill from biomimetic/drive wording to
  `eidos-organism-loop-design`.
  Rationale: "Biomimetic" invites biology-as-proof false positives, while
  "organism loop" points at the intended job: measured feedback loops with
  anti-Goodhart checks.
  Date/Author: 2026-06-23 / Codex

## Outcomes & Retrospective

Not yet complete.

## Context And Orientation

eiDOS is an always-on autonomous intelligence runtime. The main docs that shape
agent behavior are `BIBLE.md`, `ARCHITECTURE_PRINCIPLES.md`,
`CONTEXT_REDESIGN.md`, `EIDOS_V3_BLUEPRINT.md`,
`EIDOS_V3_ARCHITECTURE.md`, `EIDOS_V3_PHILOSOPHY.md`,
`METABOLISM_PLAN.md`, `SELF_IMPROVEMENT_PLAN.md`, `CLAUDE.md`, and
`OPERATING_MANUAL.md`.

Codex skills are folders containing a required `SKILL.md`, optional
`agents/openai.yaml`, and optional `references/` files. The design target here
is progressive disclosure: small trigger-focused `SKILL.md` files that point to
deeper references only when the task needs them.

## Constraints

Keep skills concise and trigger-specific. Do not create a single large lore
dump. Preserve eiDOS principles: biology is inspiration, not proof; mechanisms
must serve the creature's purpose; build boundaries before organs; prefer
event-driven behavior; avoid live-service mutation; and preserve the
self-improvement boundary where eiDOS proposes and the dashboard applies.

Do not modify live services, runtime state, or eiDOS application code. This work
is documentation and skill scaffolding only. Do not push or open a PR without
explicit approval.

## Plan Of Work

First, decide the final skill boundary set by comparing the user's proposed
direction, the initial recommendation, and the repo docs. The skill set should
cover recurring work modes, not every document. The selected set is:

- `eidos-tradeoff-decision`
- `eidos-nervous-system-design`
- `eidos-organism-loop-design`
- `eidos-tick-context-agency`
- `eidos-self-improvement-safety`
- `eidos-tool-capability-design`

Second, initialize each skill folder with the official `init_skill.py` helper,
including `references/` and UI metadata. Edit `SKILL.md` files to contain the
workflow and trigger details, then add short reference files distilled from
eiDOS docs.

Third, validate syntax and triggering quality. Use `quick_validate.py` when a
PyYAML-capable Python environment is available. If it is not, run a fallback
frontmatter/name/reference check and record the missing dependency. Use
subagents for bounded review or forward-testing of skill trigger quality.

Finally, commit the skill set with this ExecPlan updated to include evidence,
decision rationale, and remaining risks.

## Concrete Steps

Working directory:

    /tmp/eidos-agent-docs

Current setup checks already run:

    git status --short --branch
    git log -3 --oneline --decorate
    python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/init_skill.py --help
    python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/quick_validate.py --help

The final scaffold commands will be recorded here after the skill names are
settled.

Scaffold commands used the official skill-creator helper:

    python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/init_skill.py eidos-tradeoff-decision --path /tmp/eidos-agent-docs/.codex/skills --resources references ...
    python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/init_skill.py eidos-nervous-system-design --path /tmp/eidos-agent-docs/.codex/skills --resources references ...
    python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/init_skill.py eidos-organism-loop-design --path /tmp/eidos-agent-docs/.codex/skills --resources references ...
    python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/init_skill.py eidos-tick-context-agency --path /tmp/eidos-agent-docs/.codex/skills --resources references ...
    python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/init_skill.py eidos-self-improvement-safety --path /tmp/eidos-agent-docs/.codex/skills --resources references ...
    python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/init_skill.py eidos-tool-capability-design --path /tmp/eidos-agent-docs/.codex/skills --resources references ...

The loop and context skills were narrowed to `eidos-organism-loop-design` and
`eidos-tick-context-agency` after subagent trigger review.

Validation command:

    python -m venv /tmp/eidos-skill-validate-venv
    /tmp/eidos-skill-validate-venv/bin/python -m pip install --quiet PyYAML
    for d in /tmp/eidos-agent-docs/.codex/skills/*; do
      /tmp/eidos-skill-validate-venv/bin/python /agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/quick_validate.py "$d"
    done

Result: all six folders printed `Skill is valid!`.

## Validation And Acceptance

Acceptance requires:

- `.codex/skills/<skill>/SKILL.md` exists for each selected skill.
- Each `SKILL.md` has valid frontmatter with `name` and `description`.
- Trigger descriptions name concrete eiDOS tasks and avoid overbroad lore
  triggers.
- Reference files are one level below each skill and are only loaded when
  needed.
- The official validation script passes, or a documented PyYAML dependency gap
  is paired with a fallback syntax/reference validation.
- At least one subagent review or forward-test checks whether the skills are
  useful and not too broad.
- Git status is clean after commit.

## Idempotence And Recovery

Skill scaffolding is additive. If a skill boundary proves too broad, merge or
delete the folder before committing; do not leave dead placeholder skills. If
validation tooling lacks dependencies, do not install packages into the repo;
use an existing environment or record the dependency gap and run a narrow
fallback validator.

## Artifacts And Notes

Source docs already inspected during this goal include:

- `EIDOS_V3_PHILOSOPHY.md`
- `EIDOS_V3_BLUEPRINT.md`
- `EIDOS_V3_ARCHITECTURE.md`
- `METABOLISM_PLAN.md`
- `CONTEXT_REDESIGN.md`
- `BIBLE.md`
- `EIDOS_V2_BLUEPRINT.md`
- `IMPROVEMENT_BACKLOG.md`

Forward-test subagents launched:

- `eidos-organism-loop-design`: critique a curiosity-hunger feature that feeds
  energy from LLM self-claimed surprise.
- `eidos-nervous-system-design`: place a webcam motion watcher without raw
  frame summaries entering the prompt.
- `eidos-tool-capability-design`: replace a "do not import requests" prompt
  reminder with a better capability/atom design.

Results:

- Organism loop: rejected energy/mood reward from LLM self-claimed surprise and
  preserved literal power as energy.
- Nervous system: rejected raw frame summaries in prompt and proposed receptor
  adapter -> pre-filter -> change/novelty -> salience -> admitted context, with
  capability/degradation tests.
- Tool capability: rejected prompt reminders as the fix for `requests` imports
  and proposed HTTP atoms, AST validation, typed failures, and aligned
  capability docs.

## Interfaces And Dependencies

New interface surface: repo-local Codex skills under `.codex/skills/`.

External helper scripts:

- `/agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/init_skill.py`
- `/agent-state/dev-workspace-agent1/home/.codex/skills/.system/skill-creator/scripts/quick_validate.py`

Validation dependency: `quick_validate.py` requires Python package `yaml`
from PyYAML.
