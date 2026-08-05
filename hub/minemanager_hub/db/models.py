"""SQLAlchemy ORM models — the hub's declared/desired state.

Runtime/observed state (is a process actually up, live logs, pid) is *not*
stored here; it lives on the agent and is reported over the WebSocket. This DB
holds only what the operator has declared.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Node(Base):
    """A managed machine — i.e. one agent."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), default="")

    # Per-agent long-lived credential (hashed, never stored in plaintext).
    credential_hash: Mapped[str | None] = mapped_column(String(128), default=None)

    # Pending one-time enrollment token (hashed) + expiry, cleared once used.
    enroll_token_hash: Mapped[str | None] = mapped_column(String(128), default=None)
    enroll_expires_at: Mapped[float | None] = mapped_column(Float, default=None)

    agent_version: Mapped[str] = mapped_column(String(32), default="")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    instances: Mapped[list["Instance"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )


class Instance(Base):
    """A server or proxy living on a node."""

    __tablename__ = "instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # "paper" | "vanilla" | "velocity"
    type: Mapped[str] = mapped_column(String(16), nullable=False)

    root_dir: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Command the agent runs inside the tmux session to launch the process.
    start_command: Mapped[str] = mapped_column(Text, nullable=False)

    # Explicit path to the server executable, relative to root_dir. Only needed
    # when the start command does not name it (wrapper script, @argfile launch,
    # ambiguous -cp); otherwise it is parsed from start_command. See
    # ``minemanager_hub.serverjar`` — the updater refuses rather than guess.
    jar_path: Mapped[str | None] = mapped_column(String(1024), default=None)

    # Optional JDK directory used to launch *this* instance (must contain
    # bin/java). Lets one node run servers on different Java versions. NULL =
    # whatever ``java`` the agent's PATH resolves to. A plain path, not a
    # secret — it is shown and edited in the UI like any other field.
    java_home: Mapped[str | None] = mapped_column(String(1024), default=None)

    # Desired lifecycle: should the agent keep this running?
    desired_running: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_restart: Mapped[bool] = mapped_column(Boolean, default=True)

    # Optional RCON coordinates (password stored separately as a Secret).
    rcon_host: Mapped[str] = mapped_column(String(255), default="127.0.0.1")
    rcon_port: Mapped[int | None] = mapped_column(Integer, default=None)

    # Installed server-binary version/build. Set by the updater; until a proper
    # Version Detector lands (v2) this reflects what we last installed, or NULL
    # when unknown.
    version: Mapped[str | None] = mapped_column(String(64), default=None)
    build: Mapped[str | None] = mapped_column(String(32), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    node: Mapped[Node] = relationship(back_populates="instances")


class Secret(Base):
    """Encrypted-at-rest secret (RCON password, Velocity forwarding secret, …).

    The ciphertext is opaque; decryption needs the vault key from the
    environment. Plaintext is never returned to the UI once set.
    """

    __tablename__ = "secrets"
    # Both _lookup_secret and set_secret use .one_or_none(), so a duplicate row
    # raises MultipleResultsFound — and because _lookup_secret runs inside
    # _agent_and_spec, that breaks *every* command for the instance, permanently.
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "key", name="uq_secret_scope_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Owning scope: an instance id or node id.
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # "instance" | "node"
    scope_id: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "rcon_password"

    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class AuditLog(Base):
    """Who did what, when — power actions, file writes, deletes."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(128), default="")  # from Authelia header
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(32), default=None)
    instance_id: Mapped[str | None] = mapped_column(String(32), default=None)
    detail: Mapped[str] = mapped_column(Text, default="")
