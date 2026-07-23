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
class InstanceCreate(BaseModel):
    name: str
    type: InstanceType
    root_dir: str
    start_command: str
    auto_restart: bool = True
    rcon_host: str = "127.0.0.1"
    rcon_port: Optional[int] = None


class InstanceUpdate(BaseModel):
    """Partial update — only the fields present are changed (PATCH semantics)."""

    name: Optional[str] = None
    type: Optional[InstanceType] = None
    root_dir: Optional[str] = None
    start_command: Optional[str] = None
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
    desired_running: bool
    auto_restart: bool
    rcon_host: str
    rcon_port: Optional[int] = None


# --- Control ---------------------------------------------------------------
class ConsoleSend(BaseModel):
    line: str


class FileWrite(BaseModel):
    path: str
    content: str


class SecretSet(BaseModel):
    key: str = Field(..., examples=["rcon_password", "forwarding_secret"])
    value: str
