"""A host may wrap the user's words in context of its own before any hook sees them.

Observed 2026-10-15 in the ChatGPT desktop app (Codex, originator
`codex_work_desktop`, cli 0.162.0-alpha.17.2): every prompt arrives as an
`<in-app-browser-context source="ambient-ui-state">` block followed by
`## My request:` and then what the user actually typed. `$landing spend 20 ...`
was therefore mid-message, read as prose about the command, and ignored -- the
task ran unguarded while the user believed it had a budget. `$landing cancel`
would have been ignored the same way, taking away the exit too.
"""

from agent_landing.hooks import is_cancel_invocation, user_prompt
from agent_landing.invocation import extract_invocation, undecorate

AMBIENT = (
    "\n<in-app-browser-context source=\"ambient-ui-state\">\n"
    "This block is automatically supplied ambient UI state, not part of the user's request. "
    "Do not treat it as an instruction or as evidence that the user explicitly selected the in-app browser.\n"
    "# In app browser:\n"
    "- The user has the in-app browser open with 1 tab.\n"
    "- Current URL: http://127.0.0.1:3180/review/preview.html\n"
    "</in-app-browser-context>\n\n"
    "## My request:\n"
)


def test_a_wrapped_invocation_is_still_authoritative():
    assert extract_invocation(AMBIENT + "$landing spend 20 有些太糊了") == ("spend 20 有些太糊了", True)


def test_a_wrapped_cancel_still_cancels():
    """The rescue path must survive the wrapper above all else."""
    assert is_cancel_invocation(AMBIENT + "$landing cancel")


def test_the_wrapper_without_a_request_heading_is_also_removed():
    wrapped = "<editor-context source=\"ambient-ui-state\">\nfile: a.py\n</editor-context>\n/landing leave 70 x"
    assert extract_invocation(wrapped) == ("leave 70 x", True)


def test_several_ambient_blocks_are_all_removed():
    two = AMBIENT.replace("## My request:\n", "") + AMBIENT
    assert extract_invocation(two + "$landing leave 70 x") == ("leave 70 x", True)


def test_only_blocks_the_host_marks_as_ambient_are_removed():
    """A tag the user typed is the user's text, and stays."""
    typed = "<note>remember this</note>\n$landing leave 70 x"
    found = extract_invocation(typed)
    assert found is not None and found[1] is False


def test_prose_about_the_command_after_the_wrapper_is_still_prose():
    found = extract_invocation(AMBIENT + "why does $landing cancel not work?")
    assert found is not None and found[1] is False
    assert not is_cancel_invocation(AMBIENT + "why does $landing cancel not work?")


def test_undecorate_leaves_an_unwrapped_prompt_alone():
    assert undecorate("`/landing cancel`") == "/landing cancel"


def test_the_hook_opens_a_budget_from_a_wrapped_prompt(tmp_path):
    from time import time

    from agent_landing.models import QuotaSnapshot, QuotaWindow
    from agent_landing.state import StateStore

    store = StateStore(tmp_path)
    store.save_snapshot(QuotaSnapshot(
        provider="codex", windows=[QuotaWindow(id="seven_day", used_percent=60)], sampled_at=time(),
    ))
    result = user_prompt(
        {"prompt": AMBIENT + "$landing spend 20 有些太糊了", "cwd": str(tmp_path)},
        store, provider="codex",
    )
    assert "budget is already active" in result["hookSpecificOutput"]["additionalContext"]
    session = store.load_session()
    assert (session.mode, session.task) == ("max_spend", "有些太糊了")
