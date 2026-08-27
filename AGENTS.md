# Codex Review / Test Instructions

You are reviewing **Agent Landing**, an alpha quota-budget guard for Codex and Codex.

Read these first:

1. `README.md`
2. `docs/ARCHITECTURE.md`
3. `docs/TESTING.md`
4. `src/agent_landing/assets/claude/landing/SKILL.md` (the packaged skill)

## Product invariant

**A quota floor is a limit, never a spending target.**

If a task is complete, stop immediately even when current quota is far above the requested floor. Never invent work, tests, refactors, or polish to consume unused budget.

## Safety architecture

Keep these separate:

- **Enforcement** uses actual quota snapshots + landing reserve + state machine.
- **Runway** is advisory telemetry derived from observed quota movement.

Do not make enforcement depend on predicted task cost or runway.

## Review goals

When asked to test or review this repository:

1. Run the baseline in `docs/TESTING.md`.
2. Prioritize state-machine correctness, early completion, LAND tool gating, emergency fallback, quota parsing, persistence, and runway isolation.
3. Add regression tests before or with fixes.
4. Prefer simulated/manual quota. Do not waste real account quota solely to hit a threshold.
5. For real Codex/Codex integration, record host version and distinguish a host hook bug from an Agent Landing policy bug.
6. Do not weaken the user's floor to make a test pass.
7. Do not replace unknown quota with 100%.

## Current alpha caveats

- Codex statusLine may not expose every model-scoped quota limit.
- Codex hook denial behavior may vary by host build; verify actual enforcement instead of assuming it.
- Codex's Agent Landing row is an autocomplete candidate, not a persistent argument hint. On `0.149.0-alpha.4.3`, Space dismisses an unselected row; select the skill before typing arguments.
- Runway is wall-clock burn telemetry, not a promise about task completion time or task cost.

## Useful commands

```bash
pytest -q
python -m compileall -q src
landing --help
landing simulate --start 100 --leave 70 --burn 0.5,0.7,0.6 --step 2
landing status
```

If you find a bug, report the minimal reproduction, expected/actual behavior, and the exact invariant affected.
