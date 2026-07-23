"""Wire protocol shared by the hub and the agent.

The transport is a single persistent WebSocket per agent (the agent dials out
to the hub). Every message on that socket is one JSON-encoded :class:`Frame`.

Frame kinds
-----------
- ``hello`` / ``welcome`` — the enrollment/identity handshake, sent once when a
  connection opens.
- ``command`` — a request from the hub to the agent. Carries a correlation
  ``id`` and an ``action`` string (e.g. ``power.start``).
- ``response`` — the agent's reply to a command, echoing the same ``id``.
- ``event`` — an unsolicited push from the agent (console output, log lines,
  state changes, heartbeats). No correlation id; consumers match on ``action``.

Payloads are intentionally left as free-form ``dict`` on the envelope so the
transport layer never needs to know every action. Typed payload models for the
known actions live below and are validated by whichever side handles them.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from minemanager_shared.version import PROTOCOL_VERSION


def new_id() -> str:
    """Generate a correlation id for a command/response pair."""
    return uuid.uuid4().hex


def now_ts() -> float:
    """Current unix timestamp (seconds, float)."""
    return time.time()


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class FrameKind(str, Enum):
    hello = "hello"
    welcome = "welcome"
    command = "command"
    response = "response"
    event = "event"


class InstanceType(str, Enum):
    paper = "paper"
    vanilla = "vanilla"
    velocity = "velocity"


class RunState(str, Enum):
    """Observed runtime state of an instance, reported by the agent."""

    stopped = "stopped"
    starting = "starting"
    running = "running"
    stopping = "stopping"
    crashed = "crashed"
    unknown = "unknown"


# --------------------------------------------------------------------------- #
# Action names (the ``action`` field on command / event frames)
# --------------------------------------------------------------------------- #


class Action(str, Enum):
    # Power / lifecycle -----------------------------------------------------
    power_start = "power.start"
    power_stop = "power.stop"
    power_restart = "power.restart"
    power_kill = "power.kill"

    # Console ---------------------------------------------------------------
    console_send = "console.send"       # command: send a line to the console
    console_subscribe = "console.subscribe"
    console_unsubscribe = "console.unsubscribe"

    # Files (all jailed to the instance root on the agent side) -------------
    files_list = "files.list"
    files_read = "files.read"
    files_write = "files.write"
    files_delete = "files.delete"
    files_upload = "files.upload"
    files_mkdir = "files.mkdir"

    # Logs ------------------------------------------------------------------
    logs_tail = "logs.tail"

    # RCON (secondary command channel) --------------------------------------
    rcon_command = "rcon.command"

    # Introspection ---------------------------------------------------------
    node_info = "node.info"
    instance_status = "instance.status"

    # ---- Event actions (agent -> hub, unsolicited) ------------------------
    ev_console_output = "console.output"
    ev_log_line = "log.line"
    ev_state_changed = "state.changed"
    ev_heartbeat = "heartbeat"


# --------------------------------------------------------------------------- #
# Frame envelopes
# --------------------------------------------------------------------------- #


class Hello(BaseModel):
    """First frame from the agent when a connection opens."""

    kind: Literal[FrameKind.hello] = FrameKind.hello
    protocol: str = PROTOCOL_VERSION
    agent_version: str
    node_id: Optional[str] = None          # set on re-connect; None on first enroll
    enrollment_token: Optional[str] = None  # one-time token, first enroll only
    credential: Optional[str] = None        # long-lived per-agent token on reconnect
    hostname: str
    capabilities: list[str] = Field(default_factory=list)


class Welcome(BaseModel):
    """Hub's reply to a successful handshake."""

    kind: Literal[FrameKind.welcome] = FrameKind.welcome
    protocol: str = PROTOCOL_VERSION
    ok: bool
    node_id: Optional[str] = None
    credential: Optional[str] = None   # issued on first enroll; agent persists it
    error: Optional[str] = None


class Command(BaseModel):
    """Hub -> agent request."""

    kind: Literal[FrameKind.command] = FrameKind.command
    id: str = Field(default_factory=new_id)
    action: str
    instance_id: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=now_ts)


class Response(BaseModel):
    """Agent -> hub reply, echoing the originating command ``id``."""

    kind: Literal[FrameKind.response] = FrameKind.response
    id: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    ts: float = Field(default_factory=now_ts)

    @classmethod
    def success(cls, command_id: str, data: Optional[dict[str, Any]] = None) -> "Response":
        return cls(id=command_id, ok=True, data=data or {})

    @classmethod
    def failure(cls, command_id: str, error: str) -> "Response":
        return cls(id=command_id, ok=False, error=error)


class Event(BaseModel):
    """Agent -> hub unsolicited push (streams, state changes, heartbeat)."""

    kind: Literal[FrameKind.event] = FrameKind.event
    action: str
    instance_id: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=now_ts)


# --------------------------------------------------------------------------- #
# Typed payloads for known actions (validated by the handling side)
# --------------------------------------------------------------------------- #


class InstanceSpec(BaseModel):
    """The declared spec the hub attaches to instance-scoped commands.

    The agent is stateless about instance *config*: the hub is the source of
    truth and ships the spec with each command, so the agent never has to keep
    its own copy in sync.
    """

    id: str
    type: InstanceType
    name: str
    root_dir: str
    start_command: str
    auto_restart: bool = True
    rcon_host: str = "127.0.0.1"
    rcon_port: Optional[int] = None
    rcon_password: Optional[str] = None  # decrypted just-in-time by the hub


class ConsoleSendData(BaseModel):
    line: str


class ConsoleOutputData(BaseModel):
    line: str
    source: Literal["log", "pty", "rcon"] = "log"


class StateChangedData(BaseModel):
    state: RunState
    pid: Optional[int] = None
    detail: Optional[str] = None


class HeartbeatData(BaseModel):
    uptime_s: float
    instances: dict[str, RunState] = Field(default_factory=dict)


class FilesListData(BaseModel):
    path: str = "."


class FileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    modified: float


class FilesReadData(BaseModel):
    path: str


class FilesWriteData(BaseModel):
    path: str
    content: str  # utf-8; binary uploads use files.upload with base64


class FilesDeleteData(BaseModel):
    path: str
    recursive: bool = False


class RconCommandData(BaseModel):
    command: str


# --------------------------------------------------------------------------- #
# Parsing helper
# --------------------------------------------------------------------------- #

_FRAME_BY_KIND: dict[str, type[BaseModel]] = {
    FrameKind.hello.value: Hello,
    FrameKind.welcome.value: Welcome,
    FrameKind.command.value: Command,
    FrameKind.response.value: Response,
    FrameKind.event.value: Event,
}

Frame = Hello | Welcome | Command | Response | Event


def parse_frame(raw: dict[str, Any]) -> Frame:
    """Parse a decoded JSON object into the matching frame model.

    Raises ``ValueError`` if the ``kind`` is missing or unknown.
    """
    kind = raw.get("kind")
    model = _FRAME_BY_KIND.get(kind)
    if model is None:
        raise ValueError(f"unknown or missing frame kind: {kind!r}")
    return model.model_validate(raw)
