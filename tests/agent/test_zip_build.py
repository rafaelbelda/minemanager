"""Directory archiving: what goes in, and how hard it is compressed.

Both are throughput concerns rather than correctness ones — the cost is paid
before the first byte reaches the browser, so it presents as a slow download.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from minemanager_agent import files
from minemanager_agent.transfer import _build_zip


def _zip_to(root: Path) -> Path:
    """Archive ``root`` the way a streaming download does: to a temp file inside
    the scratch dir, which lives *under* the directory being zipped."""
    work = root / files.HIDDEN_DIR
    work.mkdir(parents=True, exist_ok=True)
    dest = work / "dl-test.zip"
    _build_zip(root, dest)
    return dest


class TestSelfConsumption:
    def test_zipping_the_instance_root_terminates(self, instance_root):
        """Regression: the temp zip lives under the root, so zipping the root
        itself archived the output file as it grew, filling the disk."""
        root = Path(instance_root)
        (root / "server.properties").write_text("level-name=world\n")
        payload = b"x" * (256 * 1024)
        (root / "world.mca").write_bytes(payload)
        # Jar backups live in the scratch dir too; they are not user content.
        (root / files.HIDDEN_DIR).mkdir()
        (root / files.HIDDEN_DIR / "server.jar.bak").write_bytes(b"old jar")

        dest = _zip_to(root)

        # Terminating is the regression; the size proves it did not swallow its
        # own output on the way.
        assert dest.stat().st_size < 4 * len(payload)
        with zipfile.ZipFile(dest) as zf:
            assert zf.testzip() is None
            names = set(zf.namelist())
        assert "server.properties" in names
        assert not any(files.HIDDEN_DIR in n for n in names)

class TestCompressionPolicy:
    def test_already_compressed_files_are_stored_verbatim(self, instance_root):
        root = Path(instance_root)
        (root / "r.0.0.mca").write_bytes(b"\0" * (128 * 1024))   # trivially deflatable
        with zipfile.ZipFile(_zip_to(root)) as zf:
            info = zf.getinfo("r.0.0.mca")
        # Compressible content, yet stored: the choice is by suffix, because a
        # real `.mca` is zlib-compressed internally.
        assert info.compress_type == zipfile.ZIP_STORED
        assert info.compress_size == info.file_size

    def test_everything_else_is_still_deflated(self, instance_root):
        root = Path(instance_root)
        (root / "latest.log").write_bytes(b"[INFO]: Saving chunks\n" * 4000)
        with zipfile.ZipFile(_zip_to(root)) as zf:
            info = zf.getinfo("latest.log")
        assert info.compress_type == zipfile.ZIP_DEFLATED
        assert info.compress_size < info.file_size // 10

