# Context And Agency Doctrine

Use this reference for `eidos-tick-context-agency`.

## One Conviction

Agency is architecture, not a vibe. A chat-tuned LLM in a control loop defaults
to dialogue unless deterministic structure makes action the optimal path.

## Division Of Labor

- The LLM is the deliberative planner: goals, decomposition, constraints,
  selective narration.
- Deterministic glue owns behavior shaping: routing, salience, resource
  arbitration, conflict/stall detection, strain, recall, goal tension.
- Execution owns fast policy or software tools. The LLM selects and
  parameterizes; it should not improvise primitive mechanics repeatedly.

## Minimal Context Pack

Stable prefix:

- identity
- tools/skills
- hard constraints
- output contract

Fresh tick data:

- exactly one current focus
- compact world model / learned facts
- active concerns, capped
- new since last tick
- recent action/outcome history
- condition or mode label

## Lessons To Preserve

- Guardrails must not lie or stonewall. Prefer auto-correct and repair paths
  over a dead "blocked" response.
- The most prominent objective wins, so make it the right one.
- Memory that can be written but not seen causes rediscovery loops.
- Salience is not optional; fresh operator input and async results belong at
  the decision point.
- Mechanisms must be wired where decisions actually happen.
- Interaction bugs require combination tests, not isolated spot checks.
- Stable prefix and delta prompting save cognition; do not re-prefill static
  doctrine every tick.

## Glue Signals

Use prose labels only as the surface of a mechanism. Real agency comes from:

- action gate thresholds
- salience ranking
- retry and park budgets
- strain accumulation
- goal-tension pressure
- state-triggered episodic recall
- typed failure classes and recovery graphs

## Verification Patterns

- Ghost replay: feed a recorded context scenario and compare next action.
- Context snapshot: show the model sees current focus, learned facts, and new
  event at the expected location.
- Parser/validator test: malformed output enters repair path.
- Loop-breaker test: repeated failure forces a different tactic or escalation.
- Recall test: current situation retrieves a prior useful episode before
  failure repeats.
