"""Which file the updater is allowed to overwrite.

A wrong answer here installs a new jar over a file nothing runs (the server
keeps booting its old binary while the hub records the new version) or, worse,
over something that is not the server at all. The module's whole job is to
refuse rather than guess, so the ``unknown`` cases matter as much as the hits.
"""

from __future__ import annotations

import pytest

from minemanager_hub import serverjar


class TestExplicitOverride:
    def test_operator_path_always_wins(self):
        got = serverjar.resolve("java -jar paper.jar", jar_path="custom/server.jar")
        assert (got.path, got.confidence) == ("custom/server.jar", "explicit")

    def test_explicit_may_be_created_when_missing(self):
        assert serverjar.resolve("./start.sh", jar_path="paper.jar").allow_create is True

    def test_blank_override_is_ignored(self):
        assert serverjar.resolve("java -jar paper.jar", jar_path="   ").confidence == "parsed"


class TestParsed:
    @pytest.mark.parametrize("command,want", [
        ("java -jar paper.jar", "paper.jar"),
        ("java -Xmx4G -jar paper.jar nogui", "paper.jar"),
        ("/usr/lib/jvm/java-21/bin/java -jar server.jar", "server.jar"),
        ("java -jar sub/dir/paper.jar", "sub/dir/paper.jar"),
        ('java -jar "my server.jar"', "my server.jar"),
        ("java -jar=paper.jar", "paper.jar"),
        ("java -cp velocity.jar com.velocitypowered.Main", "velocity.jar"),
        ("java -classpath server.jar net.minecraft.server.Main", "server.jar"),
        ("java -cp 'libraries/*:server.jar' net.minecraft.Main", "server.jar"),
    ])
    def test_resolves(self, command, want):
        got = serverjar.resolve(command)
        assert (got.path, got.confidence) == (want, "parsed")

    def test_parsed_may_not_be_created(self):
        """A mis-parse must fail loudly, not create a jar nothing runs."""
        assert serverjar.resolve("java -jar paper.jar").allow_create is False


class TestRefusesToGuess:
    @pytest.mark.parametrize("command", [
        "./start.sh",                                   # wrapper script
        "bash run.sh",
        "java @user_jvm_args.txt @libraries/args.txt",   # argfile hides the classpath
        "java -cp a.jar:b.jar net.minecraft.Main",       # ambiguous classpath
        "",
        "   ",
    ])
    def test_unknown(self, command):
        got = serverjar.resolve(command)
        assert (got.path, got.confidence) == (None, "unknown")
        assert got.allow_create is False

    def test_unbalanced_quotes_degrade_rather_than_raise(self):
        assert serverjar.resolve('java -jar "unterminated').confidence in ("parsed", "unknown")


def test_update_refuses_when_the_jar_cannot_be_identified(client, node_id):
    """The refusal reaches the API, and nothing is sent to the agent."""
    from minemanager_hub.agents.registry import AgentConnection, registry

    sent: list[dict] = []

    async def record(frame: dict) -> None:
        sent.append(frame)

    inst = client.post(f"/api/nodes/{node_id}/instances", json={
        "name": "wrapped", "type": "paper", "root_dir": "/srv/mc",
        "start_command": "./start.sh",
    }).json()["id"]
    registry.register(AgentConnection(node_id, send=record))
    try:
        r = client.post(f"/api/instances/{inst}/update", json={"version": "1.21.8"})
    finally:
        registry.unregister(node_id)
    assert r.status_code == 400
    assert "jar path" in r.json()["detail"].lower()
    assert sent == []


class TestTheLaunchConventionForNewServers:
    """One builder, used for both kinds of create, so the jar name and the
    nogui rule are not restated per caller."""

    def test_each_supported_software_has_its_jar(self):
        assert serverjar.jar_for_type("paper") == "paper.jar"
        assert serverjar.jar_for_type("vanilla") == "server.jar"
        assert serverjar.jar_for_type("velocity") == "velocity.jar"

    def test_a_server_takes_nogui(self):
        assert serverjar.build_start_command("paper", "4G") == (
            "java -Xmx4G -jar paper.jar nogui"
        )

    def test_a_proxy_does_not(self):
        assert serverjar.build_start_command("velocity", "2G") == (
            "java -Xmx2G -jar velocity.jar"
        )

    def test_no_memory_means_no_xmx(self):
        assert serverjar.build_start_command("paper", None) == "java -jar paper.jar nogui"
        assert serverjar.build_start_command("paper", "  ") == "java -jar paper.jar nogui"

    def test_unknown_software_is_refused_rather_than_guessed(self):
        """Inventing a jar name is what the rest of this module exists to avoid."""
        with pytest.raises(serverjar.UnsupportedSoftware):
            serverjar.jar_for_type("purpur")
        with pytest.raises(serverjar.UnsupportedSoftware):
            serverjar.build_start_command("purpur", "4G")


class TestDeclaringAnExistingServer:
    def test_the_start_command_is_composed_when_it_is_omitted(self, client, node_id):
        inst = client.post(f"/api/nodes/{node_id}/instances", json={
            "name": "survival", "type": "paper", "root_dir": "/srv/mc/survival",
            "memory": "6G",
        }).json()
        assert inst["start_command"] == "java -Xmx6G -jar paper.jar nogui"

    def test_an_explicit_start_command_still_wins(self, client, node_id):
        inst = client.post(f"/api/nodes/{node_id}/instances", json={
            "name": "wrapped", "type": "paper", "root_dir": "/srv/mc/wrapped",
            "start_command": "./start.sh", "memory": "6G",
        }).json()
        assert inst["start_command"] == "./start.sh"

    def test_a_memory_value_the_jvm_would_refuse_is_rejected(self, client, node_id):
        r = client.post(f"/api/nodes/{node_id}/instances", json={
            "name": "survival", "type": "paper", "root_dir": "/srv/mc/survival",
            "memory": "2048",
        })
        assert r.status_code == 422
