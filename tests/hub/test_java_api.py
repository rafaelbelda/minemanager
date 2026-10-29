"""The hub's Java routes, against a faked agent and a faked Adoptium."""

from __future__ import annotations

import pytest

from minemanager_hub.agents.registry import AgentConnection, registry
from minemanager_hub.api import java as java_api
from minemanager_hub.providers.base import Download
from minemanager_shared.protocol import Action, Response

R8 = {"home": "/usr/lib/jvm/java-8", "major": 8, "version": "1.8.0_412", "source": "system"}
R17 = {"home": "/usr/lib/jvm/java-17", "major": 17, "version": "17.0.12", "source": "system",
       "default": True}
R21 = {"home": "/var/lib/minemanager-agent/java/temurin-21-jre", "major": 21,
       "version": "21.0.5+11", "source": "managed"}
LINUX = {"os": "linux", "arch": "x64"}


class FakeAgent(AgentConnection):
    def __init__(self, node_id: str, runtimes=None, platform=LINUX):
        super().__init__(node_id, self._answer)
        self.runtimes = runtimes if runtimes is not None else [R8, R17, R21]
        self.platform = platform
        self.commands: list[dict] = []
        self.probe_result = {"home": "/opt/jdk-25", "major": 25, "source": "manual"}

    async def _answer(self, frame: dict) -> None:
        self.commands.append(frame)
        action, data = frame["action"], {}
        if action == Action.java_list.value:
            extra = [{"home": h, "major": 25, "source": "manual"} for h in frame["data"]["extra"]]
            data = {"runtimes": [dict(r) for r in extra + self.runtimes],
                    "platform": self.platform, "managed_dir": "/var/lib/minemanager-agent/java"}
        elif action == Action.java_probe.value:
            data = {"runtime": self.probe_result}
        elif action == Action.java_install.value:
            data = {"runtime": {**R21, "major": frame["data"]["feature"]}}
        elif action == Action.java_remove.value:
            data = {"removed": frame["data"]["java_home"]}
        self.resolve(Response(id=frame["id"], ok=True, data=data))

    def sent(self, action: str) -> list[dict]:
        return [c for c in self.commands if c["action"] == action]


@pytest.fixture
def agent(node_id):
    conn = FakeAgent(node_id)
    registry.register(conn)
    yield conn
    registry.unregister(node_id)


def _declare(client, node_id, name, java_home=None):
    body = {"name": name, "type": "paper", "root_dir": f"/srv/mc/{name}",
            "start_command": "java -jar paper.jar"}
    if java_home:
        body["java_home"] = java_home
    r = client.post(f"/api/nodes/{node_id}/instances", json=body)
    assert r.status_code == 201, r.text
    return r.json()


class TestList:
    def test_lists_with_usage(self, client, node_id, agent):
        _declare(client, node_id, "lobby")                       # node default
        _declare(client, node_id, "survival", R21["home"])
        out = client.get(f"/api/nodes/{node_id}/java").json()
        by_home = {r["home"]: r for r in out["runtimes"]}
        assert by_home[R17["home"]]["used_by"] == ["lobby"]
        assert by_home[R21["home"]]["used_by"] == ["survival"]
        assert out["online"] and out["installable"] and out["managed"] == [8, 17, 21, 25]

    def test_requirement_and_recommendation(self, client, node_id, agent):
        out = client.get(f"/api/nodes/{node_id}/java?software=paper&version=1.21.4").json()
        assert out["requirement"]["minimum"] == 21
        assert out["recommended_home"] == R21["home"]
        compat = {r["home"]: r.get("compatible") for r in out["runtimes"]}
        assert compat == {R8["home"]: False, R17["home"]: False, R21["home"]: True}

    def test_recommendation_prefers_the_default_among_equals(self, client, node_id, agent):
        agent.runtimes = [{**R17, "default": False, "source": "managed",
                           "home": "/var/lib/minemanager-agent/java/temurin-17-jre"}, R17]
        out = client.get(f"/api/nodes/{node_id}/java?software=paper&version=1.20.4").json()
        assert out["recommended_home"] == R17["home"]

    def test_offline_lists_registered_paths_unprobed(self, client, node_id, agent):
        assert client.post(f"/api/nodes/{node_id}/java", json={"java_home": "/opt/jdk-25"}).status_code == 201
        registry.unregister(node_id)
        out = client.get(f"/api/nodes/{node_id}/java").json()
        assert not out["online"]
        assert out["runtimes"] == [{"home": "/opt/jdk-25", "source": "manual", "probed": False,
                                    "used_by": []}]

    def test_registered_paths_are_sent_for_probing(self, client, node_id, agent):
        client.post(f"/api/nodes/{node_id}/java", json={"java_home": "/opt/jdk-25"})
        client.get(f"/api/nodes/{node_id}/java")
        assert agent.sent(Action.java_list.value)[-1]["data"]["extra"] == ["/opt/jdk-25"]

    def test_unknown_node(self, client):
        assert client.get("/api/nodes/nope/java").status_code == 404


class TestRegister:
    def test_register_and_forget(self, client, node_id, agent):
        r = client.post(f"/api/nodes/{node_id}/java", json={"java_home": "/opt/jdk-25/"})
        assert r.status_code == 201 and r.json()["runtime"]["major"] == 25
        assert agent.sent(Action.java_probe.value)[0]["data"]["java_home"] == "/opt/jdk-25"
        assert client.post(f"/api/nodes/{node_id}/java",
                           json={"java_home": "/opt/jdk-25"}).status_code == 409
        assert client.delete(f"/api/nodes/{node_id}/java?java_home=/opt/jdk-25").status_code == 204
        assert not agent.sent(Action.java_remove.value)      # forgetting deletes nothing

    def test_unusable_path_is_refused(self, client, node_id, agent):
        agent.probe_result = {"home": "/opt/x", "source": "manual", "error": "no bin/java in /opt/x"}
        r = client.post(f"/api/nodes/{node_id}/java", json={"java_home": "/opt/x"})
        assert r.status_code == 400 and "no bin/java" in r.json()["detail"]

    @pytest.mark.parametrize("bad", ["relative/jdk", "/opt/jdk;rm -rf /", "/opt/jdk/bin/java", ""])
    def test_bad_paths_are_422(self, client, node_id, agent, bad):
        assert client.post(f"/api/nodes/{node_id}/java", json={"java_home": bad}).status_code == 422

    def test_offline_is_409(self, client, node_id):
        assert client.post(f"/api/nodes/{node_id}/java",
                           json={"java_home": "/opt/jdk"}).status_code == 409


class TestRemoveManaged:
    def test_refused_while_in_use(self, client, node_id, agent):
        _declare(client, node_id, "survival", R21["home"])
        r = client.delete(f"/api/nodes/{node_id}/java", params={"java_home": R21["home"]})
        assert r.status_code == 409 and "survival" in r.json()["detail"]
        assert not agent.sent(Action.java_remove.value)

    def test_removes_when_unused(self, client, node_id, agent):
        r = client.delete(f"/api/nodes/{node_id}/java", params={"java_home": R21["home"]})
        assert r.status_code == 204
        assert agent.sent(Action.java_remove.value)[0]["data"]["java_home"] == R21["home"]


class TestInstall:
    @pytest.fixture
    def adoptium(self, monkeypatch):
        calls = []

        async def fake(feature, os_name, arch):
            calls.append((feature, os_name, arch))
            return Download(url="https://github.com/adoptium/x.tar.gz", filename="x.tar.gz",
                            version=f"jdk-{feature}", size=1, checksum="a" * 64,
                            checksum_algo="sha256")
        monkeypatch.setattr(java_api, "resolve_jre", fake)
        return calls

    def test_resolves_for_the_agent_platform(self, client, node_id, agent, adoptium):
        agent.platform = {"os": "alpine-linux", "arch": "aarch64"}
        r = client.post(f"/api/nodes/{node_id}/java/install", json={"feature": 17})
        assert r.status_code == 200, r.text
        assert adoptium == [(17, "alpine-linux", "aarch64")]
        sent = agent.sent(Action.java_install.value)[0]["data"]
        assert sent["feature"] == 17 and sent["download"]["checksum_algo"] == "sha256"

    @pytest.mark.parametrize("feature", [11, 16, 99])
    def test_only_managed_releases(self, client, node_id, agent, adoptium, feature):
        assert client.post(f"/api/nodes/{node_id}/java/install",
                           json={"feature": feature}).status_code == 422

    def test_unsupported_platform(self, client, node_id, agent, adoptium):
        agent.platform = {"os": "windows", "arch": "x64"}
        assert client.post(f"/api/nodes/{node_id}/java/install",
                           json={"feature": 21}).status_code == 400
        assert not adoptium


def test_compat_table(client):
    out = client.get("/api/java/compat").json()
    assert out["managed"] == [8, 17, 21, 25]
    assert out["rules"]["paper"][-1] == {"since": "26.1", "minimum": 25, "recommended": 25}


class TestAdoptium:
    """The resolver's own refusals, with the network faked."""

    @pytest.fixture
    def upstream(self, monkeypatch):
        from minemanager_hub.providers import adoptium
        box = {}

        async def fake_get_json(url, ttl=0):
            box["url"] = url
            return box["reply"]
        monkeypatch.setattr(adoptium, "get_json", fake_get_json)
        return box

    @staticmethod
    def _asset(**pkg):
        base = {"name": "OpenJDK21U-jre_x64_linux_hotspot_21.0.5_11.tar.gz",
                "link": "https://github.com/adoptium/temurin21-binaries/releases/download/x.tar.gz",
                "checksum": "b" * 64, "size": 50}
        return [{"binary": {"package": {**base, **pkg}}, "release_name": "jdk-21.0.5+11"}]

    async def test_resolves(self, upstream):
        from minemanager_hub.providers.adoptium import resolve_jre
        upstream["reply"] = self._asset()
        d = await resolve_jre(21, "linux", "x64")
        assert (d.checksum_algo, d.version) == ("sha256", "jdk-21.0.5+11")
        assert "image_type=jre" in upstream["url"] and "/21/hotspot" in upstream["url"]

    @pytest.mark.parametrize("pkg", [
        {"link": "https://evil.example/x.tar.gz"},
        {"link": "http://github.com/x.tar.gz"},
        {"checksum": ""},
        {"checksum": "not-hex"},
    ])
    async def test_refuses(self, upstream, pkg):
        from minemanager_hub.providers import ProviderError
        from minemanager_hub.providers.adoptium import resolve_jre
        upstream["reply"] = self._asset(**pkg)
        with pytest.raises(ProviderError):
            await resolve_jre(21, "linux", "x64")
