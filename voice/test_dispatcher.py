"""Tests for work-order detection.

This is the highest-consequence logic in the voice service. A missed dispatch
costs one repeated sentence; a WRONG dispatch points an agent with
bypassPermissions at the wrong repository and lets it edit files there. The
asymmetry is why classify() demands an explicit project mention and why these
cases lean heavily on the sentences that must NOT dispatch.

Run:  voice/venv/Scripts/python.exe -m pytest voice/test_dispatcher.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dispatcher import Dispatcher          # noqa: E402
from projects import resolve, PROJECTS     # noqa: E402


def classify(text: str):
    proj, go, reason = Dispatcher().classify(text)
    return (proj.key if proj else None), go, reason


# --- sentences that SHOULD start a job -------------------------------------

def test_action_verb_plus_project_dispatches():
    for text, key in [
        ("run the tests on the servesync api", "api"),
        ("fix the build on the nursetrack admin", "admin"),
        ("build me a week of posts on unshackled truth", "unshackled"),
        ("draft a blog post for wendell", "wendell"),
        ("add a note to the vault about the charting", "vault"),
        ("update the portfolio hero copy", "kellybuilt"),
    ]:
        got_key, go, _ = classify(text)
        assert go is True, f"should dispatch: {text}"
        assert got_key == key, f"{text} -> {got_key}, expected {key}"


def test_explicit_prefix_dispatches_without_an_action_verb():
    key, go, _ = classify("work on my portfolio")
    assert go is True
    assert key == "kellybuilt"


# --- sentences that must NOT start a job -----------------------------------

def test_questions_about_a_project_stay_conversation():
    for text in [
        "what is the nurse app built with",
        "how is the admin dashboard doing",
        "is the backend running?",
        "tell me about unshackled truth",
        "remind me what the api does",
        "did you check the portfolio",
    ]:
        _, go, _ = classify(text)
        assert go is False, f"must NOT dispatch: {text}"


def test_no_project_named_never_dispatches():
    for text in ["what did we do yesterday", "hey what time is it", "fix it"]:
        key, go, _ = classify(text)
        assert go is False, f"must NOT dispatch: {text}"
        assert key is None


def test_action_verb_alone_is_not_enough():
    """The dangerous case. 'Fix the login' with no project named must never
    pick a repo on its own."""
    key, go, _ = classify("fix the login screen")
    assert go is False
    assert key is None


# --- alias resolution ------------------------------------------------------

def test_longer_alias_wins_over_shorter():
    """'nursetrack admin' must resolve to the admin app, not the mobile app
    that shares the first word. This is the ordering guarantee in
    projects._ALIAS_INDEX and it is easy to break by adding an alias."""
    assert resolve("fix the nursetrack admin login").key == "admin"
    assert resolve("run the tests on nursetrack").key == "nursetrack"


def test_every_project_has_at_least_one_alias():
    for p in PROJECTS:
        assert p.aliases, f"{p.key} has no spoken aliases, so it can never be reached by voice"


def test_every_alias_resolves_to_its_own_project():
    """Guards against a new alias being shadowed by a longer one from a
    different project, which would silently route work to the wrong repo."""
    for p in PROJECTS:
        for alias in p.aliases:
            got = resolve(f"please build {alias} now")
            assert got is not None, f"alias {alias!r} resolves to nothing"
            assert got.key == p.key, (
                f"alias {alias!r} of {p.key} resolves to {got.key} instead"
            )


# --- explicit request with no project --------------------------------------

def test_explicit_request_without_a_project_asks_which_one():
    key, go, reason = classify("work on it")
    assert go is False
    assert reason is not None and "which one" in reason.lower()
