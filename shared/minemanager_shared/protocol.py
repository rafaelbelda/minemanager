"""Wire protocol shared by the hub and the agent.

The transport is a single persistent WebSocket per agent (the agent dials out
to the hub). Every message on that socket is one JSON-encoded :class:`Frame`.

Frame kinds
-----------
- ``hello`` / ``welcome``: the enrollment/identity handshake, sent once when a
  connection opens.
- ``command``: a request from the hub to the agent, with a correlation ``id``
  and an ``action`` string (e.g. ``power.start``).
- ``response``: the agent's reply to a command, echoing the same ``id``.
- ``event``: an unsolicited push from the agent (console output, log lines,
  state changes, heartbeats). No correlation id; consumers match on ``action``.

Payloads are free-form ``dict`` on the *envelope* so the transport layer never
needs to know every action, but each action's payload has a model below and both
sides use it: the hub builds command payloads by constructing the model, and the
agent validates incoming ones against it before handling.

Unknown fields are ignored rather than rejected (pydantic's default), so a
newer hub adding a field does not break an older agent.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field

from minemanager_shared.version import PROTOCOL_VERSION


def new_id() -> str:
    """Generate a correlation id for a command/response pair."""
    return uuid.uuid4().hex


def now_ts() -> float:
    """Current unix timestamp (seconds, float)."""
    return time.time()


#: Used in file names, tmux targets and URL paths.
SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


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


#: Software governed by Mojang's EULA: creation needs an explicit acceptance
#: and writes an ``eula.txt``. Velocity is a proxy and holds no world. Shared
#: so the hub, the agent and the UI all read one list.
EULA_TYPES = frozenset({InstanceType.paper, InstanceType.vanilla})


class RestartPolicy(str, Enum):
    """What the agent does when an instance's session ends by itself.

    One ordered field rather than two booleans: the states are three, not four.

    None of these can make a server unstoppable. Stop, Kill and agent shutdown
    clear the supervision flag before the monitor sees the session vanish.
    """

    never = "never"          # it ended; leave it down whatever the reason
    on_crash = "on_crash"    # relaunch a failure, honour a deliberate stop
    always = "always"        # relaunch even a clean shutdown from the console


class RunState(str, Enum):
    """Observed runtime state of an instance, reported by the agent."""

    stopped = "stopped"
    starting = "starting"
    running = "running"
    stopping = "stopping"
    crashed = "crashed"
    updating = "updating"   # a server-binary update is in progress (start refused)
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

    # Files (all jailed to the instance root on the agent side) -------------
    files_list = "files.list"
    files_read = "files.read"
    files_write = "files.write"
    files_delete = "files.delete"
    files_upload = "files.upload"
    files_fetch = "files.fetch"      # download a file's bytes, or a dir as a zip
    files_rename = "files.rename"
    files_extract = "files.extract"  # extract an archive into its directory

    # Logs ------------------------------------------------------------------
    logs_tail = "logs.tail"

    # Version / build updater -----------------------------------------------
    update_apply = "update.apply"   # transactionally replace the server jar

    # Provisioning ----------------------------------------------------------
    instance_create = "instance.create"   # make a new server under the node root

    # Large-file streaming transfers ----------------------------------------
    transfer_start = "transfer.start"   # hub tells agent to open a data connection

    # Introspection ---------------------------------------------------------
    instance_states = "instance.states"   # batch: real run-state for a set of ids

    # ---- Event actions (agent -> hub, unsolicited) ------------------------
    ev_console_output = "console.output"
    ev_state_changed = "state.changed"
    ev_heartbeat = "heartbeat"

    # ---- Event actions the hub itself raises ------------------------------
    # Creation outruns its HTTP request, so the outcome comes back here.
    ev_provision_done = "provision.done"


# --------------------------------------------------------------------------- #
# Frame envelopes
# --------------------------------------------------------------------------- #


class Hello(BaseModel):
    """First frame from the agent when a connection opens."""

    # Bounded: parsed before authentication.
    kind: Literal[FrameKind.hello] = FrameKind.hello
    protocol: str = Field(default=PROTOCOL_VERSION, max_length=32)
    agent_version: str = Field(max_length=64)
    # set on re-connect; None on first enroll
    node_id: Optional[str] = Field(default=None, max_length=64)
    # one-time token, first enroll only
    enrollment_token: Optional[str] = Field(default=None, max_length=256)
    # long-lived per-agent token on reconnect
    credential: Optional[str] = Field(default=None, max_length=256)
    hostname: str = Field(max_length=255)
    session_id: Optional[str] = Field(default=None, max_length=64)
    capabilities: list[Annotated[str, Field(max_length=32)]] = Field(
        default_factory=list, max_length=32
    )
    # The agent's own instance root, which it enforces against this local value
    # rather than anything the hub sends. None = none configured, which
    # disables provisioning for the node.
    root_dir: Optional[str] = Field(default=None, max_length=4096)


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

    id: SafeId
    type: InstanceType
    name: str
    root_dir: str
    start_command: str
    # Optional JDK directory containing ``bin/java``. The agent points
    # JAVA_HOME/PATH at it, so the start command need not name an interpreter.
    # Empty = the node's default java.
    java_home: Optional[str] = None
    restart_policy: RestartPolicy = RestartPolicy.on_crash


class UpdateDownload(BaseModel):
    """A resolved server-binary download, produced by a hub version provider.

    Provider-agnostic: the agent downloads a URL, optionally verifies a
    checksum, and swaps the jar.
    """

    url: str
    filename: str                    # source filename (informational)
    size: Optional[int] = None       # expected bytes, when known
    checksum: Optional[str] = None   # hex digest, when the provider supplies one
    checksum_algo: Optional[Literal["sha256", "sha1"]] = None
    version: str                     # what we're installing (for display/record)
    build: Optional[str] = None


class UpdateApplyData(BaseModel):
    """Payload for the ``update.apply`` command (hub -> agent).

    Declared here rather than with the other commands because it needs
    :class:`UpdateDownload` above; it is instance-scoped like the rest.
    """

    instance: "InstanceSpec"
    jar_name: str                    # target jar to replace, relative to root
    # Only an operator-supplied jar path may be created when missing, so a
    # mis-parsed one fails loudly.
    allow_create: bool = False
    download: UpdateDownload


class InstanceCreateData(BaseModel):
    """Payload for the ``instance.create`` command (hub -> agent).

    Provisions a brand-new server in ``instance.root_dir``, which the agent
    re-checks against its own node root before touching anything. Declared here
    because it needs :class:`UpdateDownload`.
    """

    instance: "InstanceSpec"
    jar_name: str                    # where the download lands, relative to root
    download: UpdateDownload
    # The operator accepted Mojang's EULA in the UI. Without it the agent
    # refuses, and no ``eula.txt`` is written.
    accept_eula: bool = False
    # Start the server again once its configuration has been generated.
    run_after: bool = False


# --------------------------------------------------------------------------- #
# Command payloads (hub -> agent)
# --------------------------------------------------------------------------- #


class InstanceCommand(BaseModel):
    """Base for every instance-scoped command.

    The hub attaches the declared spec to each one, which is what lets the agent
    stay stateless about instance config.
    """

    instance: InstanceSpec


class PowerData(InstanceCommand):
    """power.start / power.stop / power.restart / power.kill; spec only."""


class ConsoleSendData(InstanceCommand):
    line: str


class LogsTailData(InstanceCommand):
    path: str = "logs/latest.log"
    lines: int = 200


class FilesListData(InstanceCommand):
    path: str = "."


class FilesReadData(InstanceCommand):
    path: str
    # None = the agent's own default. Optional, so an older hub still works.
    max_bytes: Optional[int] = None


class FilesWriteData(InstanceCommand):
    path: str
    content: str  # utf-8; binary uploads use files.upload with base64


class FilesUploadData(InstanceCommand):
    path: str
    content_b64: str


class FilesDeleteData(InstanceCommand):
    path: str
    recursive: bool = False


class FilesFetchData(InstanceCommand):
    path: str
    cap: Optional[int] = None      # as max_bytes above


class FilesRenameData(InstanceCommand):
    path: str
    new_name: str                  # bare filename; the agent rejects separators


class FilesExtractData(InstanceCommand):
    path: str
    overwrite: bool = False


class TransferStartData(InstanceCommand):
    tid: SafeId
    # Closed set: anything else must not reach the upload path.
    direction: Literal["download", "upload"]
    path: str
    total: Optional[int] = None


class InstanceStatesData(BaseModel):
    """instance.states; node-scoped, so no instance spec."""

    ids: list[SafeId] = Field(default_factory=list, max_length=10_000)


# --------------------------------------------------------------------------- #
# Event payloads (agent -> hub), built by the agent at each emit site
# --------------------------------------------------------------------------- #


class ConsoleOutputData(BaseModel):
    line: str
    # "log" = tail of logs/latest.log, the normal stream. "pty" = replayed tail
    # of the mirrored tmux pane, which explains a crash that happened before the
    # server's own logger started.
    source: Literal["log", "pty"] = "log"


class StateChangedData(BaseModel):
    state: RunState
    detail: Optional[str] = None
    # The instance exited with a success status after having come up: somebody
    # shut it down from the console. An observation, not a decision; the hub
    # reads it and clears start_with_node, so a console "stop" means what the
    # UI's Stop means.
    clean_exit: bool = False
    # Is the agent still keeping this instance up? False once it has given up
    # (policy never, crash-loop, launch failures, a bad path), so the UI can say
    # "abandoned" rather than "restarting". Does not touch start_with_node: a
    # reboot is a fair retry.
    supervised: bool = True


class HeartbeatData(BaseModel):
    uptime_s: float
    instances: dict[str, RunState] = Field(default_factory=dict)


class ProvisionDoneData(BaseModel):
    """Outcome of a creation, raised by the hub (not an agent).

    The instance id is on the event envelope. ``ok`` false means it has been
    un-declared, so this is the last thing said about that id; ``error`` is the
    agent's reason, including the server's last terminal output.
    """

    ok: bool
    name: str
    error: Optional[str] = None


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
