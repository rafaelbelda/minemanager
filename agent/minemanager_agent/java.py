"""Discover, inspect, install, and remove supported Java runtimes.

Probe system JDKs normally; ProtectHome hides home-based installs.
Install only under the agent data dir and remove only those installs.
Validate runtimes by executing bin/java with fixed arguments, no shell,
and a timeout; cache results until the binary changes. Directory probes
are no more privileged than the hub's existing ability to set commands.
Java version mismatches are allowed; supervisor launch limits prevent
endless retries, and mismatch_hint explains JVM errors.
"""

from __future__ import annotations

import errno
import os
import platform as _platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import uuid
from pathlib import Path
from typing import Any

from minemanager_agent import updater
from minemanager_shared.protocol import JavaPlatform, JavaRuntimeInfo

#: Subdirectory of the agent data dir holding managed runtimes.
JAVA_SUBDIR = "java"

# Parent directories whose children are checked for a bin/java. One level
# only: /opt/java/openjdk is found through /opt/java, not by walking /opt.
SCAN_DIRS = (
    "/usr/lib/jvm",
    "/usr/lib64/jvm",
    "/usr/java",
    "/usr/local/java",
    "/opt/java",
    "/opt/jdk",
    "/opt",
    "/usr/local",
)

PROBE_TIMEOUT_S = 10.0
# A JRE unpacks to ~150 MB, a JDK to ~350 MB.
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024

_ARCH = {
    "x86_64": "x64", "amd64": "x64",
    "aarch64": "aarch64", "arm64": "aarch64",
    "armv7l": "arm", "armv7": "arm",
    "ppc64le": "ppc64le", "s390x": "s390x", "riscv64": "riscv64",
}

_RELEASE_RE = re.compile(r'^([A-Z_]+)="?(.*?)"?\s*$')
_PROP_RE = re.compile(r"^\s*([a-z.]+)\s*=\s*(.*?)\s*$")

_installing: set[int] = set()
_install_lock = threading.Lock()


class JavaError(Exception):
    """A runtime operation was refused or failed; the message says why."""


# --------------------------------------------------------------------------- #
# Inspecting one runtime
# --------------------------------------------------------------------------- #


def parse_major(version: str | None) -> int | None:
    """``"1.8.0_412"`` -> 8, ``"21.0.5"`` -> 21, ``"25"`` -> 25."""
    m = re.match(r"^(\d+)(?:\.(\d+))?", (version or "").strip())
    if not m:
        return None
    first = int(m.group(1))
    if first == 1 and m.group(2):          # the pre-9 "1.x" scheme
        return int(m.group(2))
    return first


def _read_release(home: Path) -> dict[str, str]:
    try:
        text = (home / "release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines()[:200]:
        m = _RELEASE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _run_properties(exe: Path) -> dict[str, str]:
    """``java -XshowSettings:properties -version``; Java 8 and later have it.

    Raises :class:`JavaError` saying why the runtime cannot start here.
    """
    try:
        proc = subprocess.run(
            [str(exe), "-XshowSettings:properties", "-version"],
            capture_output=True, timeout=PROBE_TIMEOUT_S, check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"}, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise JavaError(f"did not answer within {PROBE_TIMEOUT_S:.0f}s") from None
    except OSError as exc:
        if exc.errno == errno.ENOEXEC:
            raise JavaError("cannot run on this node: built for another CPU or OS") from None
        raise JavaError(f"cannot run on this node: {exc.strerror or exc}") from None
    text = proc.stderr.decode("utf-8", "replace")
    props = {}
    for line in text.splitlines():
        m = _PROP_RE.match(line)
        if m:
            props.setdefault(m.group(1), m.group(2))
    if "java.version" not in props:
        # The loader's own complaint (a missing GLIBC_2.34, a missing libz)
        # says more than the exit status.
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        reason = first[:200] if first else f"exit status {proc.returncode}"
        raise JavaError(f"cannot run on this node: {reason}")
    return props


_verified: dict[tuple[str, int, int], dict[str, str] | str] = {}


def _verify(exe: Path) -> dict[str, str]:
    """:func:`_run_properties`, once per binary: keyed on its real path, size
    and mtime, so a reinstall or upgrade is checked afresh."""
    real = os.path.realpath(exe)
    st = os.stat(real)
    key = (real, st.st_size, st.st_mtime_ns)
    hit = _verified.get(key)
    if hit is None:
        try:
            hit = _run_properties(Path(real))
        except JavaError as exc:
            hit = str(exc)
        _verified[key] = hit
    if isinstance(hit, str):
        raise JavaError(hit)
    return hit


def _is_file(path: Path) -> bool:
    """``Path.is_file`` without the PermissionError Python < 3.14 lets escape
    for an untraversable directory."""
    try:
        return path.is_file()
    except OSError:
        return False


def _hidden_by_sandbox(home: str) -> bool:
    return home == "/home" or home.startswith(("/home/", "/root/", "/run/user/"))


def probe(home: str, source: str = "manual") -> JavaRuntimeInfo:
    """Inspect ``home``. Never raises: an unusable directory comes back with
    ``error`` set, so the UI can show why."""
    info = JavaRuntimeInfo(home=home, source=source)
    try:
        return _probe(info)
    except Exception as exc:  # noqa: BLE001 - one odd directory must not sink a listing
        info.error = f"could not be inspected: {exc}"
        return info


def _probe(info: JavaRuntimeInfo) -> JavaRuntimeInfo:
    home = info.home
    if not (posixpath.isabs(home) or os.path.isabs(home)):
        info.error = "must be an absolute path"
        return info
    path = Path(home)
    exe = path / "bin" / "java"
    if not _is_file(exe):
        if _hidden_by_sandbox(home):
            info.error = (f"{home} is not visible to the agent: its systemd unit sets "
                          "ProtectHome=true. Move the runtime outside /home, e.g. to /opt.")
        elif not path.exists():
            info.error = "directory does not exist on this node"
        else:
            info.error = f"no bin/java in {home}"
        return info
    if not os.access(exe, os.X_OK):
        info.error = f"{exe} is not executable by the agent"
        return info

    release = _read_release(path)
    version, vendor = release.get("JAVA_VERSION"), release.get("IMPLEMENTOR")
    image = (release.get("IMAGE_TYPE") or "").lower() or None
    if release.get("JAVA_RUNTIME_VERSION"):
        version = release["JAVA_RUNTIME_VERSION"]   # carries the build: 21.0.5+11
    try:
        props = _verify(exe)
    except JavaError as exc:
        # Still say what it is, so "Java 21 cannot run here" reads as such.
        info.version, info.vendor, info.major = version, vendor, parse_major(version)
        info.error = str(exc)
        return info
    # What the binary says beats a file lying next to it.
    if parse_major(props.get("java.version")) != parse_major(version):
        version = props.get("java.version")
    vendor = vendor or props.get("java.vendor")

    info.version = version
    info.vendor = vendor
    info.major = parse_major(version)
    if image in ("jdk", "jre"):
        info.image = image
    else:
        info.image = "jdk" if _is_file(path / "bin" / "javac") else "jre"
    if info.major is None:
        info.error = f"unrecognised version {version!r}"
    return info


def home_of_executable(exe: str | os.PathLike) -> Path:
    """The runtime directory a ``java`` executable belongs to, symlinks resolved.

    A Java 8 JDK keeps a second ``java`` under ``jre/bin``; that maps to the JDK
    itself so it does not list twice.
    """
    home = Path(os.path.realpath(exe)).parent.parent
    if home.name == "jre" and _is_file(home.parent / "bin" / "java"):
        home = home.parent
    return home


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def detect_platform() -> JavaPlatform:
    if sys.platform.startswith("linux"):
        musl = any(Path("/lib").glob("ld-musl-*"))
        os_name = "alpine-linux" if musl else "linux"
    elif sys.platform == "darwin":
        os_name = "mac"
    elif sys.platform.startswith("win"):
        os_name = "windows"
    else:
        os_name = sys.platform
    return JavaPlatform(os=os_name, arch=_ARCH.get(_platform.machine().lower()))


def _children_with_java(parent: Path) -> list[Path]:
    try:
        kids = sorted(parent.iterdir())
    except OSError:
        return []
    return [k for k in kids if not k.name.startswith(".") and _is_file(k / "bin" / "java")]


def _real(p: str | Path) -> str:
    return os.path.realpath(p)


def discover(java_dir: Path | None, extra: list[str] | tuple[str, ...] = (),
             scan_dirs: tuple[str, ...] = SCAN_DIRS) -> dict[str, Any]:
    """Every runtime on this node, plus the platform downloads must match.

    ``extra`` (the operator's registered paths) are listed as given, and win
    over a scanned path that resolves to the same place.
    """
    found: dict[str, JavaRuntimeInfo] = {}      # realpath -> info

    def add(home: str | Path, source: str) -> None:
        key = _real(home)
        if key not in found:
            found[key] = probe(str(home), source)

    for home in extra:
        add(home, "manual")

    managed_root = Path(java_dir) if java_dir else None
    if managed_root is not None:
        for home in _children_with_java(managed_root):
            add(home, "managed")

    for parent in scan_dirs:
        for home in _children_with_java(Path(parent)):
            add(_real(home), "system")

    env_home = os.environ.get("JAVA_HOME")
    if env_home and _is_file(Path(env_home) / "bin" / "java"):
        add(_real(env_home), "system")

    on_path = shutil.which("java")
    default_key = None
    if on_path:
        default_home = home_of_executable(on_path)
        default_key = _real(default_home)
        add(default_home, "system")
        found[default_key].default = True

    runtimes = sorted(
        found.values(),
        key=lambda r: (r.error is not None, -(r.major or 0), r.home),
    )
    return {
        "runtimes": [r.model_dump(mode="json") for r in runtimes],
        "platform": detect_platform().model_dump(mode="json"),
        "managed_dir": str(managed_root) if managed_root else None,
    }


# --------------------------------------------------------------------------- #
# Install / remove (managed dir only)
# --------------------------------------------------------------------------- #


def managed_home(java_dir: Path, feature: int) -> Path:
    """Stable per-release path, so reinstalling updates every instance using it
    at its next start."""
    return Path(java_dir) / f"temurin-{feature}-jre"


def _unpack(archive: Path, dest: Path) -> Path:
    """Unpack a runtime tarball into the empty ``dest`` and return its home.

    tarfile's ``data`` filter does the hard part: no absolute paths, nothing
    outside ``dest``, no links leaving it, no devices, no setuid bits.
    """
    if not hasattr(tarfile, "data_filter"):
        raise JavaError("installing Java needs Python 3.11.4 or newer on the agent")
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        total = sum(m.size for m in members if m.isfile())
        if total > MAX_UNPACKED_BYTES:
            raise JavaError(f"archive unpacks to {total} bytes, over the limit")
        try:
            free = shutil.disk_usage(dest.parent).free
        except OSError:
            free = None
        if free is not None and total > free:
            raise JavaError(f"archive unpacks to {total} bytes but only {free} are free")
        dest.mkdir()
        try:
            tf.extractall(dest, filter="data")
        except tarfile.FilterError as exc:
            raise JavaError(f"refusing an unsafe archive: {exc}") from None
    tops = [p for p in dest.iterdir()]
    if len(tops) != 1 or not tops[0].is_dir():
        raise JavaError("archive does not hold a single runtime directory")
    home = tops[0]
    if not (home / "bin" / "java").is_file():
        raise JavaError("archive does not contain bin/java")
    return home


def install(java_dir: Path, feature: int, download: dict[str, Any]) -> dict:
    """Download, verify, unpack and move a runtime into place. Blocking.

    The checksum is mandatory and must be SHA-256. The previous install of the
    same release stays in place until the new one is verified; running servers
    keep the files they have open.
    """
    if download.get("checksum_algo") != "sha256" or not download.get("checksum"):
        raise JavaError("a Java download must come with a SHA-256 checksum")
    if not str(download.get("filename", "")).endswith(".tar.gz"):
        raise JavaError("only .tar.gz runtimes can be installed on this node")

    with _install_lock:
        if feature in _installing:
            raise JavaError(f"Java {feature} is already being installed")
        _installing.add(feature)
    java_dir = Path(java_dir)
    tag = uuid.uuid4().hex
    archive = java_dir / f".download-{tag}.tar.gz"
    staging = java_dir / f".staging-{tag}"
    old = java_dir / f".old-{tag}"
    try:
        java_dir.mkdir(parents=True, exist_ok=True)
        _sweep(java_dir)
        try:
            size = updater.download_verified(
                download["url"], archive,
                algo="sha256", expected=download["checksum"],
                timeout=updater.DOWNLOAD_TIMEOUT_S, max_bytes=updater.MAX_BYTES,
            )
        except updater.UpdateError as exc:
            raise JavaError(str(exc)) from None

        unpacked = _unpack(archive, staging)
        info = probe(str(unpacked), "managed")
        if info.error or info.major != feature:
            raise JavaError(
                f"downloaded runtime reports Java {info.major or '?'}, expected {feature}"
                + (f" ({info.error})" if info.error else "")
            )

        final = managed_home(java_dir, feature)
        if final.exists():
            os.replace(final, old)
        try:
            os.replace(unpacked, final)
        except OSError:
            if old.exists() and not final.exists():
                os.replace(old, final)          # put the working one back
            raise
        info.home = str(final)
        return {"runtime": info.model_dump(mode="json"), "size": size}
    finally:
        for p in (staging, old):
            shutil.rmtree(p, ignore_errors=True)
        archive.unlink(missing_ok=True)
        with _install_lock:
            _installing.discard(feature)


def _sweep(java_dir: Path) -> None:
    """Drop what an agent killed mid-install left behind. Only while this is
    the sole install running, since those names are in use otherwise."""
    with _install_lock:
        if len(_installing) > 1:
            return
    try:
        leftovers = [p for p in java_dir.iterdir()
                     if p.name.startswith((".download-", ".staging-", ".old-"))]
    except OSError:
        return
    for p in leftovers:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


# Java's own words for "this class needs a newer runtime". Class file version
# N is Java N - 44.
_CLASS_VERSION_RE = re.compile(
    r"class file version (\d+)(?:\.\d+)?\).*?only recognizes class file versions up to (\d+)"
)


def mismatch_hint(lines: list[str]) -> str | None:
    """A plain sentence for a server started on too old a Java, or None."""
    for line in reversed(lines):
        m = _CLASS_VERSION_RE.search(line)
        if m:
            need, have = int(m.group(1)) - 44, int(m.group(2)) - 44
            return (f"This server needs Java {need} or newer but was started with Java "
                    f"{have}. Choose another Java in the instance's Settings.")
    return None


def remove(java_dir: Path, home: str) -> dict:
    """Delete a managed runtime. Anything outside the managed dir is refused."""
    root = Path(java_dir).resolve()
    target = Path(home).resolve()
    if target.parent != root or target.name.startswith("."):
        raise JavaError(f"{home} was not installed by MineManager; refusing to delete it")
    if not target.is_dir():
        raise FileNotFoundError(home)
    shutil.rmtree(target)
    return {"removed": home}
