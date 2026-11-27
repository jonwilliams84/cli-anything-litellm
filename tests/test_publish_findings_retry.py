"""Regression tests for the transient-failure retry in publish_findings.py.

The 2026-11-25 Security Scan run failed its Findings Summary job because
`gh issue edit` died with a transient GitHub GraphQL error ("Something went
wrong while executing your query") that the publisher never retried. These
tests load the real script and assert the BEHAVIOUR the retry produces (a
transient failure heals itself, a permanent one still fails), never the source
that implements it.
"""
from __future__ import annotations

import importlib.util
import json as _json
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "publish_findings.py"
)
_spec = importlib.util.spec_from_file_location("publish_findings", _SCRIPT)
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)


class _Result:
    def __init__(self, code: int, stdout: str = "", stderr: str = ""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class FakeGH:
    """Scripted `gh` for publish(): labels + open issue exist; the mutating
    command (edit/create) gets a queue of scripted results, defaulting to
    success once the queue is empty."""

    def __init__(self, mutations: list[_Result] | None = None):
        self.mutations = list(mutations or [])
        self.mutation_count = 0

    def __call__(self, cmd, capture_output=True, text=True, **kwargs):
        head = list(cmd)[:3]
        if head == ["gh", "label", "list"]:
            labels = [publish.SECURITY_LABEL, publish.QUALITY_LABEL]
            return _Result(0, stdout=_json.dumps([{"name": n} for n in labels]))
        if head == ["gh", "issue", "list"]:
            return _Result(0, stdout='[{"number": 2}]')
        # the mutating call (issue edit / create / label create)
        self.mutation_count += 1
        if self.mutations:
            return self.mutations.pop(0)
        return _Result(0)

    @property
    def mutation_attempts(self) -> int:
        return self.mutation_count


# publish() reaches gh via the subprocess module import in the script
def _install(monkeypatch, fake, record):
    monkeypatch.setattr(publish.subprocess, "run", fake)
    monkeypatch.setattr(publish.time, "sleep", record)


def test_transient_edit_failure_is_retried_and_issue_updated(monkeypatch):
    """A transient GraphQL hiccup heals itself: the edit is retried until it
    succeeds and publish() reports success."""
    fake = FakeGH([
        _Result(1, stderr="GraphQL: Something went wrong while executing your query"),
        _Result(0),
    ])
    sleeps: list[float] = []
    _install(monkeypatch, fake, sleeps.append)

    assert publish.publish("t", publish.SECURITY_LABEL, "body") is True
    assert fake.mutation_attempts == 2
    # escalating backoff, not busy-looping
    assert sleeps == [publish.TRANSIENT_BASE_DELAY * attempt for attempt in (1,)]


def test_persistent_transient_failure_exhausts_attempts_then_fails(monkeypatch):
    """A GitHub outage that lasts beyond TRANSIENT_ATTEMPTS attempts still fails
    the publish (the check is not weakened), after trying every attempt."""
    boom = _Result(
        1, stderr="failed to update issue #2: GraphQL: Something went wrong"
    )
    fake = FakeGH([boom] * (publish.TRANSIENT_ATTEMPTS + 1))
    sleeps: list[float] = []
    _install(monkeypatch, fake, sleeps.append)

    assert publish.publish("t", publish.SECURITY_LABEL, "body") is False
    assert fake.mutation_attempts == publish.TRANSIENT_ATTEMPTS
    assert len(sleeps) == publish.TRANSIENT_ATTEMPTS - 1


def test_permanent_failure_is_not_retried(monkeypatch):
    """Non-transient errors (auth, 404, 422...) are permanent by definition and
    must fail on the first attempt: the retry loop may not stall permanent
    failures or mask them."""
    fake = FakeGH([
        _Result(1, stderr="failed to list issues: HTTP 404: Not Found")
    ] * 3)
    sleeps: list[float] = []
    _install(monkeypatch, fake, sleeps.append)

    assert publish.publish("t", publish.SECURITY_LABEL, "body") is False
    assert fake.mutation_attempts == 1
    assert sleeps == []


def test_is_transient_matches_github_transient_wording():
    """The marker list must cover the wording the actual outage produced and
    the ordinary 5xx/timeouts, without matching permanent errors."""
    assert publish.is_transient(
        "GraphQL: Something went wrong while executing your query"
    )
    assert publish.is_transient("There was a technical problem")
    assert publish.is_transient("error sending request: timeout")
    assert publish.is_transient("Bad Gateway received from server")
    assert not publish.is_transient("HTTP 404: Not Found")
    assert not publish.is_transient("HTTP 422: validation_failed")
    assert not publish.is_transient("")
