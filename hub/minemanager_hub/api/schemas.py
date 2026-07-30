"""Request/response models for the hub's REST API (distinct from wire protocol)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

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


class InstanceCreate(BaseModel):
    name: str
    type: InstanceType
    root_dir: str
    start_command: str
    jar_path: Optional[str] = _JAR_PATH
    auto_restart: bool = True
    rcon_host: str = "127.0.0.1"
    rcon_port: Optional[int] = None


class InstanceUpdate(BaseModel):
    """Partial update — only the fields present are changed (PATCH semantics)."""

    name: Optional[str] = None
    type: Optional[InstanceType] = None
    root_dir: Optional[str] = None
    start_command: Optional[str] = None
    jar_path: Optional[str] = _JAR_PATH
    auto_restart: Optional[bool] = None
    rcon_host: Optional[str] = None
    rcon_port: Optional[int] = None


class InstanceOut(BaseModel):
    id: str
    node_id: str
    name: str
    type: str
    root_dir: str
    start_command: str
    jar_path: Optional[str] = None
    desired_running: bool
    auto_restart: bool
    rcon_host: str
    rcon_port: Optional[int] = None
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


class SecretSet(BaseModel):
    key: str = Field(..., examples=["rcon_password", "forwarding_secret"])
    value: str


class UpdateRequest(BaseModel):
    version: str
    build: Optional[str] = None   # required only for software that has builds
