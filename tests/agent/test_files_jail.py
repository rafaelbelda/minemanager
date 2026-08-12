"""The path jail — every file operation depends on it staying closed.

``resolve_within`` is the agent's only defence against a hub (or a compromised
one) reaching outside an instance root. It had no tests at all.
"""

from __future__ import annotations

import os

import pytest

from minemanager_agent import files

ESCAPES = [
    "../outside.txt",
    "../../etc/passwd",
    "a/../../../etc/passwd",
    "./../../outside",
    "sub/../../..",
]


@pytest.mark.parametrize("rel", ESCAPES)
def test_traversal_is_rejected(instance_root, rel):
    with pytest.raises(files.JailError):
        files.resolve_within(instance_root, rel)


def test_absolute_input_is_treated_as_relative(instance_root):
    (instance_root / "etc").mkdir()
    (instance_root / "etc" / "passwd").write_text("inside")
    resolved = files.resolve_within(instance_root, "/etc/passwd")
    assert resolved == (instance_root / "etc" / "passwd").resolve()
    assert resolved.read_text() == "inside"


def test_plain_relative_path_resolves_inside(instance_root):
    assert files.resolve_within(instance_root, "world/level.dat") == \
        (instance_root / "world" / "level.dat").resolve()


def test_root_itself_is_allowed(instance_root):
    assert files.resolve_within(instance_root, ".") == instance_root.resolve()


@pytest.mark.skipif(os.name != "posix", reason="symlink creation needs privileges on Windows")
def test_symlink_escaping_the_root_is_rejected(instance_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("nope")
    (instance_root / "link").symlink_to(outside)
    with pytest.raises(files.JailError):
        files.resolve_within(instance_root, "link/secret")


class TestOperationsRespectTheJail:
    def test_read_outside_rejected(self, instance_root):
        with pytest.raises(files.JailError):
            files.read_for_editor(instance_root, "../../etc/passwd")

    def test_write_outside_rejected(self, instance_root):
        with pytest.raises(files.JailError):
            files.write_file(instance_root, "../escaped.txt", "x")

    def test_delete_refuses_the_root(self, instance_root):
        with pytest.raises(files.JailError):
            files.delete(instance_root, ".")

    def test_rename_refuses_the_root(self, instance_root):
        with pytest.raises(files.JailError):
            files.rename(instance_root, ".", "newname")

    @pytest.mark.parametrize("bad", ["../evil", "sub/evil", "..", "", "a\\b"])
    def test_rename_refuses_a_non_bare_name(self, instance_root, bad):
        (instance_root / "f.txt").write_text("x")
        with pytest.raises((ValueError, files.JailError)):
            files.rename(instance_root, "f.txt", bad)


class TestListing:
    def test_hides_the_agent_scratch_dir(self, instance_root):
        (instance_root / files.HIDDEN_DIR).mkdir()
        (instance_root / "visible.txt").write_text("x")
        names = [e["name"] for e in files.list_dir(instance_root, ".")]
        assert names == ["visible.txt"]

    def test_paths_are_root_relative_with_forward_slashes(self, instance_root):
        (instance_root / "plugins").mkdir()
        (instance_root / "plugins" / "a.yml").write_text("x")
        entry = files.list_dir(instance_root, "plugins")[0]
        assert entry["path"] == "plugins/a.yml"
        assert entry["is_dir"] is False

    def test_directories_sort_before_files(self, instance_root):
        (instance_root / "zdir").mkdir()
        (instance_root / "afile").write_text("x")
        assert [e["name"] for e in files.list_dir(instance_root, ".")] == ["zdir", "afile"]


class TestEditorGuards:
    def test_binary_content_is_flagged_not_returned(self, instance_root):
        (instance_root / "blob.bin").write_bytes(b"PK\x03\x04\x00\x00binary")
        assert files.read_for_editor(instance_root, "blob.bin")["binary"] is True

    def test_oversized_text_refused(self, instance_root):
        (instance_root / "big.txt").write_text("x" * 5000)
        with pytest.raises(ValueError):
            files.read_for_editor(instance_root, "big.txt", max_bytes=100)

    def test_text_round_trips(self, instance_root):
        files.write_file(instance_root, "cfg/server.properties", "motd=hi\n")
        out = files.read_for_editor(instance_root, "cfg/server.properties")
        assert (out["binary"], out["content"]) == (False, "motd=hi\n")


class TestTailLines:
    def test_missing_file_is_not_an_error(self, instance_root):
        out = files.tail_lines(instance_root, "logs/latest.log")
        assert (out["lines"], out["missing"]) == ([], True)

    def test_returns_only_the_last_n(self, instance_root):
        (instance_root / "log.txt").write_text("\n".join(f"line{i}" for i in range(100)))
        assert files.tail_lines(instance_root, "log.txt", lines=3)["lines"] == \
            ["line97", "line98", "line99"]

    def test_partial_first_line_dropped_when_truncated(self, instance_root):
        (instance_root / "log.txt").write_text("\n".join(f"line{i}" for i in range(500)))
        out = files.tail_lines(instance_root, "log.txt", lines=5, max_bytes=40)
        assert out["truncated"] is True
        assert all(line.startswith("line") for line in out["lines"])
