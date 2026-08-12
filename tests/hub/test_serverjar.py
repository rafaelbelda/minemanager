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
    """The refusal reaches the API, not just the resolver."""
    inst = client.post(f"/api/nodes/{node_id}/instances", json={
        "name": "wrapped", "type": "paper", "root_dir": "/srv/mc",
        "start_command": "./start.sh",
    }).json()["id"]
    r = client.post(f"/api/instances/{inst}/update", json={"version": "1.21.8"})
    assert r.status_code in (400, 409)      # 400 unresolvable, 409 if agent offline first
    if r.status_code == 400:
        assert "jar" in r.json()["detail"].lower()
