# Demo script

Two recordings for the README and launch posts. Neither is recorded yet.

## A. Scripted terminal demo (zero quota)

[`examples/demo/demo.sh`](../examples/demo/demo.sh) feeds the real hook
commands the same JSON payloads Claude Code sends, in a throwaway
`LANDING_HOME`. No model runs and no account quota is spent. Output is
deterministic, so the recording can be redone at any time.

```bash
DEMO_PAUSE=0 bash examples/demo/demo.sh
```

```bash
asciinema rec --cols 100 --rows 30 -c "bash examples/demo/demo.sh" landing-demo.cast
```

```bash
agg --font-size 16 landing-demo.cast docs/landing-demo.gif
```

On Windows, run it from Git Bash with `LANDING_PY=.venv/Scripts/python.exe`.

Storyboard (about 25 seconds at the default 1.5 s pause):

| # | Caption | What the viewer sees |
| --- | --- | --- |
| 1 | Quota as the status line reports it | `five_hour 88.0% left · seven_day 73.0% left` |
| 2 | `/landing leave 72 fix the tests` | the hook opens the budget on every window |
| 3 | Agent tries `Edit src/app.py` | `permissionDecision: deny` with the boundary message |
| 4 | Agent runs `Read src/app.py` | no objection: inspection still works |
| 5 | Agent tries `landing cancel` | denied: only the user can lift the floor |
| 6 | Agent's final reply | `Stop` hook saves it to `~/.landing/handoffs/…`, then prints it |

The floor (72%) is set just under the weekly reading (73%). With the default 4%
reserve the landing boundary is 76%, so the budget is in LAND as soon as it
opens. That is the point of the demo: it shows the guard acting before the
floor, not at it.

Be honest in the caption: this demo shows Landing's decisions, using real hook commands and
synthetic payloads. It does not show a host acting on them. Take B does.

## B. Live take in Claude Code (optional, a few hundred tokens)

This shows a real host enforcing the denial. It spends only the one or two model
turns needed to attempt the write and write the reply. **It also serves as the
pending live verification of the Stop-hook handoff.** Record the host version
and keep the result for [VERIFICATION.md](VERIFICATION.md).

1. Use the terminal CLI, where the status line runs, in a session that has had
   at least one reply (quota appears only after it). Note the lower of the two
   readings in the status bar, e.g. `W 73%`.
2. Start recording (asciinema, or a screen recorder for the desktop app).
3. Send `/landing leave 72 add a one-line comment to README.md`, using a floor 1
   point under the reading from step 1.
4. Expect: the agent's `Edit` is denied with "Quota budget boundary reached";
   the agent replies with a handoff; Claude Code shows
   `Landing: quota floor reached; handoff saved to …`.
5. In a terminal: `ls ~/.landing/handoffs/` and show the newest file.
   README.md must be unchanged (`git diff --stat` is empty).
6. Stop recording. Send `/landing cancel` if anything is still armed.

Capture `claude --version` alongside the recording. Codex can follow the same
steps with `$landing leave 72 …`, after `landing doctor` reports all three hooks
trusted.

If step 4 shows a write going through, that is a finding, not a retake: record
whether the host ignored a `deny` (host bug) or Landing never denied it (policy
bug), as [TESTING.md](TESTING.md) asks.
