"""How the launch line is assembled, without needing tmux.

``new_session`` is the one place the operator's start command is modified before
it runs, so what it appends — and what it leaves alone — is worth pinning down.
The tmux CLI is stubbed; this is about the command string.
"""

from __future__ import annotations

import pytest

from minemanager_agent import tmux


@pytest.fixture
def calls(monkeypatch):
    """Records every tmux invocation and reports success for all of them."""
    recorded: list[tuple[str, ...]] = []

    async def fake_run(*args: str):
        recorded.append(args)
        # has-session must fail, or new_session refuses as "already exists".
        if args[0] == "has-session":
            return 1, "", ""
        return 0, "", ""

    monkeypatch.setattr(tmux, "_run", fake_run)
    return recorded


def _respawn(calls) -> str:
    """The command tmux was told to run in the pane."""
    for args in calls:
        if args[0] == "respawn-pane":
            return args[-1]
    raise AssertionError("no respawn-pane call was made")


class TestExitStatusCapture:
    async def test_the_status_is_recorded_after_the_command(self, calls):
        await tmux.new_session(
            "mm-i1", "/srv/mc", "java -jar paper.jar nogui", status_path="/var/x/i1.exit"
        )

        command = _respawn(calls)
        assert command.startswith("java -jar paper.jar nogui;")
        assert command.endswith("printf %s \"$?\" > /var/x/i1.exit")

    async def test_the_operators_command_is_not_rewritten(self, calls):
        # A start line can depend on the shell (an unexpanded $PATH, an @argfile,
        # a wrapper script). Appending keeps all of that working; wrapping it in
        # another quoting layer would not.
        start = "JAVA_HOME=/opt/jdk PATH=/opt/jdk/bin:$PATH ./start.sh @args"
        await tmux.new_session("mm-i1", "/srv/mc", start, status_path="/var/x/i1.exit")

        assert _respawn(calls).startswith(start + ";")

    async def test_a_path_needing_quotes_is_quoted(self, calls):
        await tmux.new_session(
            "mm-i1", "/srv/mc", "java -jar s.jar", status_path="/var/my data/i1.exit"
        )

        assert "'/var/my data/i1.exit'" in _respawn(calls)

    async def test_without_a_status_path_the_command_is_untouched(self, calls):
        # The agent runs without a data dir in some setups; it must still launch.
        await tmux.new_session("mm-i1", "/srv/mc", "java -jar paper.jar nogui")

        assert _respawn(calls) == "java -jar paper.jar nogui"

    async def test_the_mirror_is_attached_before_the_command_runs(self, calls):
        # Ordering the pane mirror depends on: a pipe can only be attached to a
        # pane that already exists, and it must be listening before the real
        # command starts or the launch output it exists to capture is lost.
        await tmux.new_session(
            "mm-i1", "/srv/mc", "java -jar s.jar",
            mirror_path="/var/x/i1.log", status_path="/var/x/i1.exit",
        )

        order = [args[0] for args in calls]
        assert order.index("new-session") < order.index("pipe-pane") < order.index("respawn-pane")
