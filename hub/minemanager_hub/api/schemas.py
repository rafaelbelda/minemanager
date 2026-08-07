"""Request/response models for the hub's REST API (distinct from wire protocol)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from minemanager_shared.protocol import InstanceType


# --- Nodes -----------------------------------------------------------------
class NodeCreate(BaseModel):
    name: str


class NodeOut(BaseModel):
    id: str
    name: str
    hostname: str
    agent_version: str
    online: bool
    last_seen: Optional[datetime] = None
    enrolled: bool


class EnrollmentOut(BaseModel):
    """Returned once when a node is created — the token is shown a single time."""

    node_id: str
    enrollment_token: str
    expires_in_s: int


# --- Instances -------------------------------------------------------------
# Path to the server executable relative to the instance root. Optional: it is
# parsed from the start command when that names it directly. Required only for
# launches that hide it (wrapper script, @argfile, ambiguous -cp).
_JAR_PATH = Field(
    default=None,
    examples=["paper.jar", "server.jar"],
    description="Server executable, relative to the instance root. Leave empty to "
                "derive it from the start command.",
)

# Optional per-instance JDK, so one node can run servers on different Java
# versions. A plain path (not a secret): it is returned by the API and edited in
# the UI like any other field.
_JAVA_HOME = Field(
    default=None,
    examples=["/usr/lib/jvm/java-21-openjdk", "/opt/jdk-17"],
    description="Directory containing bin/java, used to launch this instance. "
                "Leave empty to use the node's default java.",
)

# The value ends up in the agent's launch command line. It is shell-quoted there,
# and anyone able to set it can already set start_command (arbitrary shell), so
# this is not a privilege boundary — it is here to reject typos and paths that
# would silently mangle the launch line rather than fail loudly.
_JAVA_HOME_FORBIDDEN = set(";&|$`<>\n\r\t\"'\\")


# Fields backed by NOT NULL columns. A PATCH carrying an explicit null for one
# of these used to reach the ORM and surface as a 500 on the flush; rejecting it
# here makes it the 422 it always was.
def _require_value(value):
    if value is None:
        raise ValueError("may not be null")
    if isinstance(value, str) and not value.strip():
        raise ValueError("may not be empty")
    return value.strip() if isinstance(value, str) else value


def _clean_java_home(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip().rstrip("/")
    if not value:
        return None            # empty means "use the node default"
    if not value.startswith("/"):
        raise ValueError("java_home must be an absolute path (e.g. /usr/lib/jvm/java-21-openjdk)")
    bad = sorted(_JAVA_HOME_FORBIDDEN & set(value))
    if bad:
        raise ValueError(f"java_home may not contain {' '.join(repr(c) for c in bad)}")
    if value.endswith("/bin/java"):
        raise ValueError("java_home is the JDK directory, not the java binary "
                         "— drop the trailing /bin/java")
    return value


class InstanceCreate(BaseModel):
    name: str
    type: InstanceType
    root_dir: str
    start_command: str
    jar_path: Optional[str] = _JAR_PATH
    java_home: Optional[str] = _JAVA_HOME
    auto_restart: bool = True

    @field_validator("name", "root_dir", "start_command")
    @classmethod
    def _check_required(cls, value: str) -> str:
        return _require_value(value)

    @field_validator("java_home")
    @classmethod
    def _check_java_home(cls, value: Optional[str]) -> Optional[str]:
        return _clean_java_home(value)


class InstanceUpdate(BaseModel):
    """Partial update — only the fields present are changed (PATCH semantics).

    Every field is Optional so it may be omitted, but the ones backed by NOT NULL
    columns still reject an explicit null. Defaults are not validated in pydantic
    v2, so these validators fire only when the field was actually sent.
    """

    name: Optional[str] = None
    type: Optional[InstanceType] = None
    root_dir: Optional[str] = None
    start_command: Optional[str] = None
    jar_path: Optional[str] = _JAR_PATH
    java_home: Optional[str] = _JAVA_HOME
    auto_restart: Optional[bool] = None

    @field_validator("name", "type", "root_dir", "start_command", "auto_restart")
    @classmethod
    def _check_required(cls, value):
        return _require_value(value)

    @field_validator("java_home")
    @classmethod
    def _check_java_home(cls, value: Optional[str]) -> Optional[str]:
        return _clean_java_home(value)


class InstanceOut(BaseModel):
    id: str
    node_id: str
    name: str
    type: str
    root_dir: str
    start_command: str
    jar_path: Optional[str] = None
    java_home: Optional[str] = None
    desired_running: bool
    auto_restart: bool
    version: Optional[str] = None    # installed server version (updater / detector)
    build: Optional[str] = None      # installed build, when applicable


# --- Control ---------------------------------------------------------------
class ConsoleSend(BaseModel):
    line: str


class FileWrite(BaseModel):
    path: str
    content: str


class FileUpload(BaseModel):
    path: str            # destination path, relative to the instance root
    content_b64: str     # file bytes, base64 (simple/non-streaming path)


class FileRename(BaseModel):
    path: str            # existing entry
    new_name: str        # bare filename (no path separators)


class FileExtract(BaseModel):
    path: str            # archive to extract, relative to root
    overwrite: bool = False


class UpdateRequest(BaseModel):
    version: str
    build: Optional[str] = None   # required only for software that has builds
