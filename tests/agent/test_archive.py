"""Archive extraction: zip-slip, size budget, and the conflict contract.

Extraction writes attacker-influenced paths to disk, so the member-path checks
are the highest-consequence code in the agent after the jail itself.
"""

from __future__ import annotations

import tarfile
import zipfile

import pytest

from minemanager_agent import archive, files


def make_zip(path, members: dict[str, str]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return path


class TestDetectFormat:
    @pytest.mark.parametrize("name,want", [
        ("a.zip", "zip"), ("a.tar", "tar"), ("a.tar.gz", "tar"), ("a.tgz", "tar"),
        ("a.tar.bz2", "tar"), ("a.tar.xz", "tar"), ("a.gz", "gz"), ("a.rar", "rar"),
        ("a.txt", None), ("noext", None), ("A.ZIP", "zip"),
    ])
    def test_by_extension(self, name, want):
        assert archive.detect_format(name) == want

    def test_tar_gz_wins_over_plain_gz(self):
        assert archive.detect_format("world.tar.gz") == "tar"


class TestZipSlip:
    @pytest.mark.parametrize("member", [
        "../escaped.txt",
        "../../etc/passwd",
        "sub/../../../escaped.txt",
        "..\\windows\\escaped.txt",
    ])
    def test_member_escaping_the_root_is_rejected(self, instance_root, member):
        make_zip(instance_root / "evil.zip", {member: "pwned"})
        with pytest.raises(files.JailError):
            archive.extract(instance_root, "evil.zip")

    def test_absolute_member_is_neutralised_not_rejected(self, instance_root):
        """A leading slash is stripped, so the member lands inside the root
        rather than at the filesystem path it names."""
        make_zip(instance_root / "abs.zip", {"/etc/passwd": "pwned"})
        assert archive.extract(instance_root, "abs.zip")["extracted"] is True
        assert (instance_root / "etc" / "passwd").read_text() == "pwned"

    def test_nothing_is_written_when_a_member_is_unsafe(self, instance_root):
        make_zip(instance_root / "evil.zip", {"good.txt": "ok", "../bad.txt": "pwned"})
        with pytest.raises(files.JailError):
            archive.extract(instance_root, "evil.zip")
        assert not (instance_root.parent / "bad.txt").exists()
        assert not (instance_root / "good.txt").exists()

    def test_tar_member_escaping_is_rejected(self, instance_root):
        payload = instance_root / "payload.txt"
        payload.write_text("pwned")
        with tarfile.open(instance_root / "evil.tar", "w") as tf:
            tf.add(payload, arcname="../escaped.txt")
        with pytest.raises(files.JailError):
            archive.extract(instance_root, "evil.tar")


class TestExtraction:
    def test_extracts_beside_the_archive(self, instance_root):
        (instance_root / "sub").mkdir()
        make_zip(instance_root / "sub" / "pack.zip", {"a.txt": "inside"})
        result = archive.extract(instance_root, "sub/pack.zip")
        assert result["extracted"] is True
        assert (instance_root / "sub" / "a.txt").read_text() == "inside"

    def test_nested_directories(self, instance_root):
        make_zip(instance_root / "p.zip", {"plugins/cfg/a.yml": "x: 1"})
        archive.extract(instance_root, "p.zip")
        assert (instance_root / "plugins" / "cfg" / "a.yml").read_text() == "x: 1"

    def test_missing_archive_raises(self, instance_root):
        with pytest.raises(FileNotFoundError):
            archive.extract(instance_root, "nope.zip")

    def test_unsupported_format_raises(self, instance_root):
        (instance_root / "a.txt").write_text("x")
        with pytest.raises(ValueError):
            archive.extract(instance_root, "a.txt")


class TestConflicts:
    def test_conflicts_reported_and_nothing_written(self, instance_root):
        (instance_root / "a.txt").write_text("original")
        make_zip(instance_root / "p.zip", {"a.txt": "replacement"})
        result = archive.extract(instance_root, "p.zip", overwrite=False)
        assert result["extracted"] is False
        assert result["conflicts"] == ["a.txt"]
        assert (instance_root / "a.txt").read_text() == "original"

    def test_overwrite_replaces(self, instance_root):
        (instance_root / "a.txt").write_text("original")
        make_zip(instance_root / "p.zip", {"a.txt": "replacement"})
        assert archive.extract(instance_root, "p.zip", overwrite=True)["extracted"] is True
        assert (instance_root / "a.txt").read_text() == "replacement"

    def test_conflict_list_is_capped(self, instance_root):
        names = {f"f{i}.txt": "new" for i in range(archive._MAX_CONFLICTS + 50)}
        for name in names:
            (instance_root / name).write_text("old")
        make_zip(instance_root / "p.zip", names)
        result = archive.extract(instance_root, "p.zip", overwrite=False)
        assert len(result["conflicts"]) == archive._MAX_CONFLICTS
        assert result["conflict_count"] == len(names)


class TestBudget:
    def test_refuses_an_archive_over_the_cap(self, instance_root, monkeypatch):
        monkeypatch.setattr(archive, "MAX_EXTRACT_BYTES", 10)
        make_zip(instance_root / "p.zip", {"a.txt": "x" * 500})
        with pytest.raises(ValueError, match="over the"):
            archive.extract(instance_root, "p.zip")

    def test_nothing_written_when_the_budget_is_blown(self, instance_root, monkeypatch):
        monkeypatch.setattr(archive, "MAX_EXTRACT_BYTES", 10)
        make_zip(instance_root / "p.zip", {"a.txt": "x" * 500})
        with pytest.raises(ValueError):
            archive.extract(instance_root, "p.zip")
        assert not (instance_root / "a.txt").exists()
