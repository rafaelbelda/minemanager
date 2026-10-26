"""Request/response models for the hub's REST API (distinct from wire protocol)."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from minemanager_shared.protocol import EULA_TYPES, InstanceType, RestartPolicy


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
    # Reported by the agent. None means it has no root configured, so new
    # servers cannot be created on this node.
    root_dir: Optional[str] = None


class EnrollmentOut(BaseModel):
    """Returned once when a node is created; the token is shown a single time."""

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

# The value ends up shell-quoted in the agent's launch command. Not a privilege
# boundary (whoever sets this can already set start_command): it rejects typos
# and paths that would silently mangle the launch line.
_JAVA_HOME_FORBIDDEN = set(";&|$`<>\n\r\t\"'\\")


# Fields backed by NOT NULL columns. A PATCH carrying an explicit null for one
# of these is a 422, not a 500 from the ORM flush.
def _require_value(value):
    if value is None:
        raise ValueError("may not be null")
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("may not be empty")
        return value.strip()
    return value


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
        raise ValueError("java_home is the JDK directory, not the java binary; "
                         "drop the trailing /bin/java")
    return value


class InstanceCreate(BaseModel):
    name: str
    type: InstanceType
    root_dir: str
    start_command: str
    jar_path: Optional[str] = _JAR_PATH
    java_home: Optional[str] = _JAVA_HOME
    restart_policy: RestartPolicy = RestartPolicy.on_crash

    @field_validator("name", "root_dir", "start_command")
    @classmethod
    def _check_required(cls, value: str) -> str:
        return _require_value(value)

    @field_validator("java_home")
    @classmethod
    def _check_java_home(cls, value: Optional[str]) -> Optional[str]:
        return _clean_java_home(value)


class InstanceUpdate(BaseModel):
    """Partial update: only the fields present are changed (PATCH semantics).

    Every field is Optional so it may be omitted, but those backed by NOT NULL
    columns still reject an explicit null. Pydantic v2 does not validate
    defaults, so these validators fire only for fields actually sent.
    """

    name: Optional[str] = None
    type: Optional[InstanceType] = None
    root_dir: Optional[str] = None
    start_command: Optional[str] = None
    jar_path: Optional[str] = _JAR_PATH
    java_home: Optional[str] = _JAVA_HOME
    start_with_node: Optional[bool] = None
    restart_policy: Optional[RestartPolicy] = None

    @field_validator(
        "name", "type", "root_dir", "start_command", "start_with_node", "restart_policy"
    )
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
    start_with_node: bool
    restart_policy: str
    version: Optional[str] = None    # installed server version (updater / detector)
    build: Optional[str] = None      # installed build, when applicable
    # This instance's root is not under its node's root, so the agent refuses
    # every command for it. Set when a node gained an MM_ROOT_DIR that does not
    # cover a server declared before it; surfaced rather than hidden, because
    # the only other symptom is every action failing with "path rejected".
    outside_node_root: bool = False


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


# --- New servers -----------------------------------------------------------
# A new server's directory is the node root joined with its name, so the name is
# a path component and is checked like one. Letters, digits and -_. only, and it
# must start with a letter or digit: that rejects "..", separators, NUL, leading
# dots and anything a shell would read specially, without enumerating them.
_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

# ``-Xmx`` takes a whole number and a unit. A bare number is *bytes* to the JVM
# ("2048" is 2 KB and refuses to launch) and a fractional one is rejected
# outright, so neither is accepted here: both would produce a declared server
# that cannot start, discovered only on its first run.
_MEMORY_RE = re.compile(r"^\d+[KMGkmg]$")


class ServerCreate(BaseModel):
    """Create a brand-new server under the node's root."""

    name: str
    type: InstanceType
    version: str
    build: Optional[str] = None      # required only for software that has builds
    memory: Optional[str] = "4G"
    restart_policy: RestartPolicy = RestartPolicy.on_crash
    accept_eula: bool = False
    run_after: bool = False

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        value = _require_value(value).strip()
        if not _SERVER_NAME_RE.match(value):
            raise ValueError(
                "name must be 1-64 characters of letters, digits, dot, dash or "
                "underscore, and start with a letter or digit"
            )
        return value

    @field_validator("memory")
    @classmethod
    def _check_memory(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _MEMORY_RE.match(value):
            raise ValueError(
                "memory must be a whole number with a unit, such as 4G or 6144M"
            )
        return value

    @model_validator(mode="after")
    def _check_eula(self) -> "ServerCreate":
        # Refused here rather than on the agent: the agent refuses it too, but
        # by then the hub has resolved a download and declared the instance, so
        # a missing acceptance would surface as an agent error instead of a bad
        # request. Velocity is not Mojang's, and is never asked.
        if self.type in EULA_TYPES and not self.accept_eula:
            raise ValueError(
                f"the Minecraft EULA must be accepted to create a {self.type.value} "
                "server; set accept_eula"
            )
        return self
