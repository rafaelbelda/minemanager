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

    def test_an_understated_size_header_cannot_slip_past_the_budget(self, instance_root):
        # A decompression bomb that declares a tiny size to pass the budget:
        # zipfile stops at the declared size and the CRC then fails, so the
        # extraction aborts and what it had begun writing is removed.
        import io
        import struct

        real = 2 * 1024 * 1024
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bomb.bin", b"\0" * real)
        raw = buf.getvalue().replace(struct.pack("<I", real), struct.pack("<I", 10))
        (instance_root / "p.zip").write_bytes(raw)

        with pytest.raises(zipfile.BadZipFile):
            archive.extract(instance_root, "p.zip")
        assert not (instance_root / "bomb.bin").exists()


class TestLinksAreNeverMaterialised:
    """A link member would be a second, unchecked path out of the jail: the
    member name is checked, but whatever is written through the link is not."""

    def test_zip_symlink_member_is_skipped(self, instance_root):
        import stat

        with zipfile.ZipFile(instance_root / "p.zip", "w") as zf:
            link = zipfile.ZipInfo("evil")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(link, "/etc/passwd")
            zf.writestr("ok.txt", "fine")
        result = archive.extract(instance_root, "p.zip")
        assert result["extracted"] is True
        assert not (instance_root / "evil").exists()
        assert not (instance_root / "evil").is_symlink()
        assert (instance_root / "ok.txt").read_text() == "fine"

    @pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                      tarfile.FIFOTYPE])
    def test_tar_link_and_device_members_are_skipped(self, instance_root, kind):
        import io

        with tarfile.open(instance_root / "p.tar", "w") as tf:
            special = tarfile.TarInfo("evil")
            special.type = kind
            special.linkname = "/etc/passwd"
            tf.addfile(special)
            body = b"fine"
            regular = tarfile.TarInfo("ok.txt")
            regular.size = len(body)
            tf.addfile(regular, io.BytesIO(body))
        archive.extract(instance_root, "p.tar")
        assert not (instance_root / "evil").exists()
        assert not (instance_root / "evil").is_symlink()
        assert (instance_root / "ok.txt").read_bytes() == b"fine"


class TestFailedMemberLeavesNoDamage:
    def test_a_file_being_overwritten_survives_a_corrupt_member(self, instance_root):
        import io
        import struct

        (instance_root / "level.dat").write_bytes(b"precious world data")
        real = 64 * 1024
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("level.dat", b"\1" * real)
        raw = buf.getvalue().replace(struct.pack("<I", real), struct.pack("<I", 10))
        (instance_root / "p.zip").write_bytes(raw)

        with pytest.raises(zipfile.BadZipFile):
            archive.extract(instance_root, "p.zip", overwrite=True)
        assert (instance_root / "level.dat").read_bytes() == b"precious world data"
        assert not list(instance_root.glob(".*.part"))

    def test_a_file_being_overwritten_survives_an_oversized_gzip(self, instance_root, monkeypatch):
        import gzip

        monkeypatch.setattr(archive, "MAX_EXTRACT_BYTES", 1024)
        (instance_root / "server.properties").write_text("motd=keep me")
        (instance_root / "server.properties.gz").write_bytes(gzip.compress(b"x" * 4096))

        with pytest.raises(ValueError, match="extraction"):
            archive.extract(instance_root, "server.properties.gz", overwrite=True)
        assert (instance_root / "server.properties").read_text() == "motd=keep me"
        assert not list(instance_root.glob(".*.part"))
