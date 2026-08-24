---
name: landing
description: Start a quota-budgeted coding task with a hard remaining-quota floor and graceful handoff.
argument-hint: '<floor-percent> -- <task>'
disable-model-invocation: true
allowed-tools: Bash(landing *)
---

Use Agent Landing for this task. The raw invocation arguments are:

`$ARGUMENTS`

Interpret the invocation as:

- the first numeric argument is the minimum remaining quota floor, in percent;
- `--` separates the quota options from the task text;
- if no quota window is named, use `seven_day`;
- `5h` or `five_hour` selects the five-hour window;
- `weekly` or `seven_day` selects the seven-day window.

Examples:

- `/landing 70 -- refactor auth and add tests`
- `/landing weekly 75 -- fix the failing checkout tests`
- `/landing 5h 30 -- investigate the flaky integration test`

Before implementation, summarize the task text after `--` into one concise shell-safe task description and run:

`landing start --provider claude --window <window> --preserve <floor> --task <task>`

Then execute the requested task normally. Treat the floor as a hard lower bound, never as a spending target. If the task completes early, immediately run `landing complete` and stop. When hooks inject CONSERVE, LAND, EMERGENCY, or STOPPED state, follow them exactly. Do not invent extra work to consume unused quota.

If the invocation does not contain a task after `--`, do not start implementation or create a budgeted session. Briefly show the expected syntax instead.
