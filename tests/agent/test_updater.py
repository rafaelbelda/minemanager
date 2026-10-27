"""The transactional jar swap.

The installed file is the process the node executes next, so every path that
is not "verified download, backup, atomic replace" must leave the old jar in
place and nothing half-written behind.
"""

from __future__ import annotations

import hashlib
import io
import urllib.request

import pytest

from minemanager_agent import files, updater

GOOD = b"new server jar"
GOOD_SHA256 = hashlib.sha256(GOOD).hexdigest()


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def served(monkeypatch):
    """Serve ``served.body`` for any URL, without the network."""
    state = type("Served", (), {"body": GOOD, "urls": []})()

    class FakeOpener:
        def open(self, req, timeout=None):
            state.urls.append(req.full_url)
            return _FakeResponse(state.body)

    monkeypatch.setattr(updater, "_opener", FakeOpener())
    return state


def _download(**kw) -> dict:
    d = {"url": "https://fill-data.papermc.io/paper.jar", "filename": "paper.jar",
         "version": "1.21.8", "build": "60", "checksum": GOOD_SHA256, "checksum_algo": "sha256"}
    d.update(kw)
    return d


@pytest.fixture
def root(instance_root):
    (instance_root / "paper.jar").write_bytes(b"old jar")
    return instance_root


def _leftovers(root) -> list[str]:
    work = root / files.HIDDEN_DIR
    return [p.name for p in work.glob("*.tmp")] if work.exists() else []


class TestHappyPath:
    async def test_replaces_and_backs_up(self, root, served):
        result = await updater.apply_update(str(root), "paper.jar", _download())
        assert (root / "paper.jar").read_bytes() == GOOD
        backup = root / result["backup"]
        assert backup.read_bytes() == b"old jar"
        assert _leftovers(root) == []


class TestFailsClosed:
    async def test_checksum_mismatch_keeps_the_old_jar(self, root, served):
        served.body = b"tampered jar"
        with pytest.raises(updater.UpdateError, match="checksum mismatch"):
            await updater.apply_update(str(root), "paper.jar", _download())
        assert (root / "paper.jar").read_bytes() == b"old jar"
        assert _leftovers(root) == []

    @pytest.mark.parametrize("missing", [{"checksum": None}, {"checksum_algo": None}])
    async def test_no_checksum_no_install(self, root, served, missing):
        with pytest.raises(updater.UpdateError, match="checksum"):
            await updater.apply_update(str(root), "paper.jar", _download(**missing))
        assert (root / "paper.jar").read_bytes() == b"old jar"
        assert served.urls == []          # refused before any request

    async def test_plain_http_is_refused_before_connecting(self, root, served):
        with pytest.raises(updater.UpdateError, match="https required"):
            await updater.apply_update(str(root), "paper.jar",
                                       _download(url="http://fill-data.papermc.io/paper.jar"))
        assert served.urls == []

    async def test_oversized_download_is_refused(self, root, served, monkeypatch):
        monkeypatch.setattr(updater, "MAX_BYTES", 4)
        with pytest.raises(updater.UpdateError, match="size limit"):
            await updater.apply_update(str(root), "paper.jar", _download())
        assert (root / "paper.jar").read_bytes() == b"old jar"
        assert _leftovers(root) == []

    @pytest.mark.parametrize("jar", ["../escape.jar", "../../etc/cron.d/x"])
    async def test_the_target_is_jailed(self, root, served, jar):
        with pytest.raises(files.JailError):
            await updater.apply_update(str(root), jar, _download(), allow_create=True)

    async def test_a_parsed_jar_that_does_not_exist_is_not_created(self, root, served):
        with pytest.raises(updater.UpdateError, match="does not exist"):
            await updater.apply_update(str(root), "other.jar", _download())
        assert not (root / "other.jar").exists()


class TestRedirects:
    def _redirect(self, newurl: str):
        req = urllib.request.Request("https://fill-data.papermc.io/paper.jar")
        return updater._HttpsOnlyRedirects().redirect_request(
            req, None, 302, "Found", {}, newurl)

    @pytest.mark.parametrize("target", ["http://fill-data.papermc.io/paper.jar",
                                        "http://169.254.169.254/latest/meta-data/",
                                        "ftp://mirror/paper.jar"])
    def test_a_redirect_off_https_is_refused(self, target):
        with pytest.raises(updater.UpdateError, match="https required"):
            self._redirect(target)

    def test_an_https_redirect_is_followed(self):
        assert self._redirect("https://cdn.papermc.io/paper.jar") is not None


class TestConcurrency:
    async def test_a_second_update_is_refused_while_one_runs(self, root, monkeypatch):
        import asyncio

        from minemanager_agent.supervisor import Supervisor
        from minemanager_shared.protocol import InstanceSpec

        async def no_session(_name):
            return False

        monkeypatch.setattr("minemanager_agent.supervisor.tmux.has_session", no_session)
        gate = asyncio.Event()

        async def slow_apply(*_a, **_kw):
            await gate.wait()
            return {"ok": True}

        monkeypatch.setattr("minemanager_agent.supervisor.updater.apply_update", slow_apply)

        async def emit(_event):
            return None

        sup = Supervisor(emit)
        spec = InstanceSpec(id="i1", type="paper", name="s", root_dir=str(root),
                            start_command="java -jar paper.jar")
        first = asyncio.ensure_future(sup.apply_update(spec, "paper.jar", _download()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="already in progress"):
            await sup.apply_update(spec, "paper.jar", _download())
        # And the server cannot be started underneath it.
        with pytest.raises(RuntimeError, match="update is in progress"):
            await sup.start(spec)
        gate.set()
        await first
