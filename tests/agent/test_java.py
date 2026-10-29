"""Finding, inspecting, installing and removing Java runtimes on a node.

Fake runtimes are directories with a ``bin/java`` file and a JDK ``release``
file. Running ``java`` is faked (see :func:`fake_exec`): it reports the
release file's version, or fails with the text of a ``bin/BROKEN`` file, which
stands for a runtime that unpacks fine and cannot start on this node.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from minemanager_agent import java, updater


def fake_runtime(home: Path, version: str = "21.0.5", vendor: str = "Eclipse Adoptium",
                 image: str = "JRE", broken: str | None = None) -> Path:
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("#!/bin/sh\n")
    (home / "bin" / "java").chmod(0o755)
    (home / "release").write_text(
        f'IMPLEMENTOR="{vendor}"\nJAVA_VERSION="{version}"\n'
        f'JAVA_RUNTIME_VERSION="{version}+11"\nIMAGE_TYPE="{image}"\n'
    )
    if broken:
        (home / "bin" / "BROKEN").write_text(broken)
    return home


@pytest.fixture(autouse=True)
def fake_exec(monkeypatch):
    """Stand in for running ``bin/java``, and start each test uncached."""
    runs: list[Path] = []

    def run(exe: Path) -> dict[str, str]:
        runs.append(exe)
        broken = exe.parent / "BROKEN"
        if broken.exists():
            raise java.JavaError(broken.read_text())
        release = java._read_release(exe.parent.parent)
        if "JAVA_VERSION" not in release:
            raise java.JavaError("cannot run on this node: exit status 1")
        return {"java.version": release["JAVA_VERSION"], "java.vendor": "Faked"}

    monkeypatch.setattr(java, "_run_properties", run)
    monkeypatch.setattr(java, "_verified", {})
    return runs


@pytest.fixture
def no_path_java(monkeypatch):
    monkeypatch.setattr(java.shutil, "which", lambda name: None)
    monkeypatch.delenv("JAVA_HOME", raising=False)


@pytest.mark.parametrize("raw,major", [
    ("1.8.0_412", 8), ("17.0.12", 17), ("21", 21), ("25.0.1+8-LTS", 25), ("", None), ("abc", None),
])
def test_parse_major(raw, major):
    assert java.parse_major(raw) == major


class TestProbe:
    def test_reads_release_file(self, tmp_path):
        info = java.probe(str(fake_runtime(tmp_path / "jre21")))
        assert (info.major, info.version, info.vendor, info.image, info.error) == (
            21, "21.0.5+11", "Eclipse Adoptium", "jre", None)

    def test_java8_scheme(self, tmp_path):
        assert java.probe(str(fake_runtime(tmp_path / "j8", version="1.8.0_412"))).major == 8

    def test_missing_dir(self, tmp_path):
        assert "does not exist" in java.probe(str(tmp_path / "nope")).error

    def test_dir_without_java(self, tmp_path):
        assert "no bin/java" in java.probe(str(tmp_path)).error

    def test_home_explains_the_sandbox(self):
        assert "ProtectHome" in java.probe("/home/someone/.sdkman/candidates/java/21").error

    def test_relative_path_refused(self):
        assert java.probe("opt/jdk").error


class TestDiscover:
    def test_scan_managed_and_manual(self, tmp_path, no_path_java):
        scan = tmp_path / "jvm"
        fake_runtime(scan / "java-17", version="17.0.12")
        managed = tmp_path / "java"
        fake_runtime(managed / "temurin-21-jre")
        manual = fake_runtime(tmp_path / "custom" / "jdk-25", version="25.0.1", image="JDK")

        out = java.discover(managed, [str(manual)], scan_dirs=(str(scan),))
        by_source = {r["source"]: r for r in out["runtimes"]}
        assert by_source["manual"]["major"] == 25
        assert by_source["managed"]["major"] == 21
        assert by_source["system"]["major"] == 17
        assert [r["major"] for r in out["runtimes"]] == [25, 21, 17]   # newest first
        assert out["platform"]["os"]

    def test_manual_wins_over_scan_of_same_dir(self, tmp_path, no_path_java):
        scan = tmp_path / "jvm"
        home = fake_runtime(scan / "java-21")
        out = java.discover(None, [str(home)], scan_dirs=(str(scan),))
        assert [r["source"] for r in out["runtimes"]] == ["manual"]

    def test_default_is_the_path_java(self, tmp_path, monkeypatch):
        scan = tmp_path / "jvm"
        fake_runtime(scan / "java-17", version="17.0.12")
        home21 = fake_runtime(scan / "java-21")
        monkeypatch.delenv("JAVA_HOME", raising=False)
        monkeypatch.setattr(java.shutil, "which", lambda name: str(home21 / "bin" / "java"))
        out = java.discover(None, [], scan_dirs=(str(scan),))
        defaults = [r for r in out["runtimes"] if r["default"]]
        assert len(defaults) == 1 and defaults[0]["major"] == 21

    def test_broken_manual_entry_is_listed_with_its_error(self, tmp_path, no_path_java):
        out = java.discover(None, [str(tmp_path / "gone")], scan_dirs=())
        assert out["runtimes"][0]["error"]


def test_java8_jre_subdir_maps_to_the_jdk(tmp_path):
    jdk = fake_runtime(tmp_path / "java-8", version="1.8.0_412")
    (jdk / "jre" / "bin").mkdir(parents=True)
    (jdk / "jre" / "bin" / "java").write_text("")
    assert java.home_of_executable(jdk / "jre" / "bin" / "java") == Path(jdk).resolve()


# --------------------------------------------------------------------------- #
# install / remove
# --------------------------------------------------------------------------- #


def _tarball(tmp_path: Path, build) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        build(tf, tmp_path)
    return buf.getvalue()


def _runtime_tar(feature: int):
    def build(tf, tmp):
        src = fake_runtime(tmp / "src" / f"jdk-{feature}.0.1+1-jre", version=f"{feature}.0.1")
        tf.add(src, arcname=src.name)
    return build


@pytest.fixture
def serve(monkeypatch):
    """Stand in for the network: 'download' the given bytes, checksum enforced."""
    def install(blob: bytes):
        def fake(url, dest, *, algo, expected, timeout, max_bytes):
            if hashlib.new(algo, blob).hexdigest() != expected:
                raise updater.UpdateError("checksum mismatch")
            Path(dest).write_bytes(blob)
            return len(blob)
        monkeypatch.setattr(updater, "download_verified", fake)
    return install


def _download(blob: bytes, **kw) -> dict:
    return {"url": "https://github.com/x.tar.gz", "filename": "x.tar.gz",
            "checksum": hashlib.sha256(blob).hexdigest(), "checksum_algo": "sha256",
            "version": "jdk-21", **kw}


needs_filter = pytest.mark.skipif(not hasattr(tarfile, "data_filter"), reason="needs tarfile filters")


@needs_filter
class TestInstall:
    def test_installs_to_a_stable_path(self, tmp_path, serve):
        blob = _tarball(tmp_path, _runtime_tar(21))
        serve(blob)
        java_dir = tmp_path / "java"
        out = java.install(java_dir, 21, _download(blob))
        home = java_dir / "temurin-21-jre"
        assert out["runtime"]["home"] == str(home) and out["runtime"]["major"] == 21
        assert (home / "bin" / "java").is_file()
        assert [p.name for p in java_dir.iterdir()] == ["temurin-21-jre"]   # no leftovers

    def test_reinstall_replaces(self, tmp_path, serve):
        java_dir = tmp_path / "java"
        for _ in range(2):
            blob = _tarball(tmp_path / str(_), _runtime_tar(21))
            serve(blob)
            java.install(java_dir, 21, _download(blob))
        assert [p.name for p in java_dir.iterdir()] == ["temurin-21-jre"]

    def test_wrong_major_is_refused_and_cleaned(self, tmp_path, serve):
        blob = _tarball(tmp_path, _runtime_tar(17))
        serve(blob)
        java_dir = tmp_path / "java"
        with pytest.raises(java.JavaError, match="expected 21"):
            java.install(java_dir, 21, _download(blob))
        assert list(java_dir.iterdir()) == []

    def test_checksum_is_mandatory(self, tmp_path):
        with pytest.raises(java.JavaError, match="SHA-256"):
            java.install(tmp_path, 21, {"url": "https://x", "filename": "x.tar.gz",
                                        "checksum": None, "checksum_algo": None})
        with pytest.raises(java.JavaError, match="SHA-256"):
            java.install(tmp_path, 21, {"url": "https://x", "filename": "x.tar.gz",
                                        "checksum": "ab", "checksum_algo": "sha1"})

    def test_bad_checksum_refused(self, tmp_path, serve):
        blob = _tarball(tmp_path, _runtime_tar(21))
        serve(blob)
        with pytest.raises(java.JavaError, match="checksum"):
            java.install(tmp_path / "java", 21, _download(blob, checksum="0" * 64))

    def test_traversal_member_refused(self, tmp_path, serve):
        def build(tf, tmp):
            data = b"x"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        blob = _tarball(tmp_path, build)
        serve(blob)
        with pytest.raises(java.JavaError, match="unsafe"):
            java.install(tmp_path / "java", 21, _download(blob))
        assert not (tmp_path / "escape.txt").exists()

    def test_link_out_of_tree_refused(self, tmp_path, serve):
        def build(tf, tmp):
            info = tarfile.TarInfo("jre/evil")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        blob = _tarball(tmp_path, build)
        serve(blob)
        with pytest.raises(java.JavaError, match="unsafe"):
            java.install(tmp_path / "java", 21, _download(blob))


class TestRemove:
    def test_removes_managed(self, tmp_path):
        home = fake_runtime(tmp_path / "java" / "temurin-21-jre")
        java.remove(tmp_path / "java", str(home))
        assert not home.exists()

    @pytest.mark.parametrize("rel", ["elsewhere/jdk", "java", "java/temurin-21-jre/bin", "java/../x"])
    def test_refuses_outside_managed_dir(self, tmp_path, rel):
        (tmp_path / "java" / "temurin-21-jre" / "bin").mkdir(parents=True)
        (tmp_path / "elsewhere" / "jdk").mkdir(parents=True)
        (tmp_path / "x").mkdir()
        with pytest.raises(java.JavaError):
            java.remove(tmp_path / "java", str(tmp_path / rel))
        assert (tmp_path / "elsewhere" / "jdk").exists()


# --------------------------------------------------------------------------- #
# failure paths: nothing may crash the agent or wedge a server
# --------------------------------------------------------------------------- #


class TestCannotRun:
    def test_unrunnable_runtime_is_listed_with_its_reason(self, tmp_path):
        home = fake_runtime(tmp_path / "jre", broken="cannot run on this node: GLIBC_2.34 not found")
        info = java.probe(str(home))
        assert info.major == 21                    # still says what it is
        assert "GLIBC_2.34" in info.error

    def test_the_binary_runs_once_per_build(self, tmp_path, fake_exec):
        home = fake_runtime(tmp_path / "jre")
        java.probe(str(home))
        java.probe(str(home))
        assert len(fake_exec) == 1

    def test_binary_version_beats_the_release_file(self, tmp_path, monkeypatch):
        home = fake_runtime(tmp_path / "jre", version="21.0.5")
        monkeypatch.setattr(java, "_run_properties", lambda exe: {"java.version": "17.0.2"})
        assert java.probe(str(home)).major == 17

    def test_an_exploding_probe_is_contained(self, tmp_path, no_path_java, monkeypatch):
        fake_runtime(tmp_path / "jvm" / "a")
        def boom(info):
            raise RuntimeError("weird filesystem")
        monkeypatch.setattr(java, "_probe", boom)
        out = java.discover(None, [], scan_dirs=(str(tmp_path / "jvm"),))
        assert "weird filesystem" in out["runtimes"][0]["error"]

    def test_unreadable_scan_dir_is_skipped(self, tmp_path, no_path_java):
        out = java.discover(None, [], scan_dirs=(str(tmp_path / "missing"), "/nonexistent/jvm"))
        assert out["runtimes"] == []


@needs_filter
class TestInstallFailures:
    def test_runtime_that_cannot_run_here_is_refused_and_cleaned(self, tmp_path, serve):
        def build(tf, tmp):
            src = fake_runtime(tmp / "src" / "jdk-21-jre", version="21.0.1",
                               broken="cannot run on this node: built for another CPU or OS")
            tf.add(src, arcname=src.name)
        blob = _tarball(tmp_path, build)
        serve(blob)
        java_dir = tmp_path / "java"
        with pytest.raises(java.JavaError, match="another CPU"):
            java.install(java_dir, 21, _download(blob))
        assert list(java_dir.iterdir()) == []

    def test_failed_swap_keeps_the_working_runtime(self, tmp_path, serve, monkeypatch):
        java_dir = tmp_path / "java"
        working = fake_runtime(java_dir / "temurin-21-jre")
        blob = _tarball(tmp_path, _runtime_tar(21))
        serve(blob)
        real_replace = java.os.replace

        def flaky(src, dst):
            if Path(src).parent.name.startswith(".staging-"):
                raise OSError("disk went away")
            return real_replace(src, dst)
        monkeypatch.setattr(java.os, "replace", flaky)
        with pytest.raises(OSError):
            java.install(java_dir, 21, _download(blob))
        assert (working / "bin" / "java").is_file()
        assert [p.name for p in java_dir.iterdir()] == ["temurin-21-jre"]

    def test_leftovers_of_a_killed_install_are_swept(self, tmp_path, serve):
        java_dir = tmp_path / "java"
        (java_dir / ".staging-dead" / "x").mkdir(parents=True)
        (java_dir / ".download-dead.tar.gz").write_bytes(b"partial")
        blob = _tarball(tmp_path, _runtime_tar(21))
        serve(blob)
        java.install(java_dir, 21, _download(blob))
        assert [p.name for p in java_dir.iterdir()] == ["temurin-21-jre"]

    def test_a_failed_install_can_be_retried(self, tmp_path, serve):
        blob = _tarball(tmp_path, _runtime_tar(21))
        serve(blob)
        with pytest.raises(java.JavaError):
            java.install(tmp_path / "java", 21, _download(blob, checksum="0" * 64))
        java.install(tmp_path / "java", 21, _download(blob))   # not stuck "in progress"


class TestMismatchHint:
    LINE = ("java.lang.UnsupportedClassVersionError: net/minecraft/bundler/Main has been "
            "compiled by a more recent version of the Java Runtime (class file version 65.0), "
            "this version of the Java Runtime only recognizes class file versions up to 52.0")

    def test_names_both_versions(self):
        hint = java.mismatch_hint(["Error: LinkageError occurred", self.LINE])
        assert "needs Java 21" in hint and "started with Java 8" in hint

    def test_none_for_other_failures(self):
        assert java.mismatch_hint(["Exception in thread main", "OutOfMemoryError"]) is None
