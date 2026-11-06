"""Which browser writes still ask for the password: a short, exact list (ADR-0010).

The signed-in session is enough for everything else. Adding a route to the password list, or
taking one off, must change this test too.
"""

import re

from pi.api.browser_contract import (
    PASSWORD_WRITES,
    owner_allowed,
    password_write,
    runtime_allowed,
    session_only_write,
)

JOB = "job_" + "a" * 32
RUN = "scheduled_" + "b" * 32
PASSWORD = [
    f"/hooks/{JOB}",
    "/memory/forget",
    "/memory/forget-many",
    "/imports/chats/imp_" + "0" * 32 + "/delete",
    "/owner/preferences",
    f"/jobs/{JOB}/hook",
    f"/jobs/{JOB}/adopt",
    f"/jobs/runs/{RUN}/budget",
    f"/jobs/runs/{RUN}/provision-budget",
    "/setup/protection",
    "/approvals/apr_123/decided",
]
# Ordinary, undoable writes: chats, tasks, proposals, jobs' own controls.
SESSION = [
    "/sessions",
    "/sessions/ses_1/turns",
    "/sessions/ses_1/fork",
    "/models/auto-settings",
    "/turns/t_1/resume",
    "/tasks",
    "/tasks/t_1/archive",
    "/proposals/p_1/decision",
    f"/jobs/runs/{RUN}/cancel",
    f"/jobs/runs/{RUN}/resume",
    f"/jobs/runs/{RUN}/reconcile",
]


def allowed(path):
    return runtime_allowed("POST", path) or owner_allowed("POST", path)


def test_the_password_list_is_exact_and_every_entry_is_a_real_route():
    assert len(PASSWORD_WRITES) == 9
    for pattern in PASSWORD_WRITES:
        assert [path for path in PASSWORD if re.fullmatch(pattern, path)], pattern
    for path in PASSWORD:
        assert allowed(path), path
        assert password_write("POST", path) and not session_only_write("POST", path), path


def test_ordinary_writes_need_only_the_session():
    for path in SESSION:
        assert allowed(path), path
        assert session_only_write("POST", path) and not password_write("POST", path), path


def test_reads_are_never_writes():
    for path in PASSWORD + SESSION:
        assert not password_write("GET", path) and not session_only_write("GET", path)


def test_lookalikes_of_a_password_route_do_not_slip_through_as_password_free():
    # A longer or shorter path is not the route; it is refused by the relay's own allowlist.
    for path in (
        "/memory/forget/x",
        "/memory/forget-few",
        "/owner/preferences/",
        f"/jobs/{JOB}/hook/x",
        "/imports/chats/imp_x/delete",
        "/imports/chats/imp_" + "0" * 32 + "/delete/x",
    ):
        assert not allowed(path), path
