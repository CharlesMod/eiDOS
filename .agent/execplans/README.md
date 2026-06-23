# ExecPlan Directory

Store active eiDOS ExecPlans in this directory.

Use `.agent/PLANS.md` as the required format and operating standard. A plan
should be self-contained, living, and specific enough for a fresh agent to
resume the work without chat history.

Suggested name format:

    <short-topic>-<yyyymmdd>.md

Examples:

    dashboard-auth-20260623.md
    nervous-retained-events-20260623.md
    runtime-nix-shell-20260623.md

Keep completed plans when they carry useful evidence or historical context. If a
plan is superseded, add an `Outcomes & Retrospective` note pointing at the new
plan rather than deleting it casually.
