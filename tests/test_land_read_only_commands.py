"""LAND must still let the agent look at what it is handing off.

Observed in ChatGPT Work mode (Codex runtime), 2026-10-15: the agent's first
move in LAND was `Get-Location; rg --files -g fib.py .`, and it was denied --
the allowlist held only git status/diff/log and pytest. The handoff that
followed could only say "nothing read, nothing done". Read-only inspection
is now allowed, chained with `;` or `&&`, while every flag that can write a
file or run a program stays refused, and pipes and redirects stay out.
"""

import pytest

from agent_landing.hooks import is_safe_landing_command


@pytest.mark.parametrize("command", [
    "Get-Location; rg --files -g fib.py -g AGENTS.md .",
    "rg -n \"def fib\" src",
    "rg --files",
    "rg -i -C 2 docstring src/fib.py",
    "Get-Content src/fib.py -TotalCount 80",
    "gc src/fib.py",
    "Get-ChildItem -Recurse -Filter *.py",
    "gci src -File",
    "Get-Location",
    "pwd",
    "cat src/fib.py",
    "head -n 40 src/fib.py",
    "ls -la src",
    "Select-String -Pattern fib -Path src/*.py",
    "git status --short && rg --files",
    "find . -name fib.py -type f",
])
def test_read_only_inspection_is_allowed(command):
    assert is_safe_landing_command(command), command


@pytest.mark.parametrize("command", [
    "rg --pre sh x",                      # runs a program per file
    "rg --pre-glob '*' --pre cat x",
    "rg -z x",                            # spawns decompressors
    "rg --search-zip x",
    "Get-Content a.py | Set-Content b.py",
    "cat a.py > b.py",
    "Get-ChildItem; Remove-Item x",
    "rg --files; Set-Content a.py x",
    "find . -delete",
    "find . -exec rm {} +",
    "Set-Content a.py x",
    "git status; rm x",
    "rg --files && del x",
    "Get-Content $env:USERPROFILE/.ssh/id_rsa",
    "cat `whoami`",
    "Get-ChildItem -Recurse | Remove-Item",
    "Out-File x",
    "Get-Content a.py -Stream x",
    "",
    ";",
])
def test_anything_that_can_write_or_run_stays_refused(command):
    assert not is_safe_landing_command(command), command
