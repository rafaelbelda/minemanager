"""The common provider interface and its value types.

Everything above the providers (REST API, updater) speaks only in terms of these
dataclasses and the :class:`Provider` ABC — never in Paper/Vanilla/Velocity
specifics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class ProviderError(Exception):
    """A provider could not satisfy a request (unknown version, upstream down…)."""


@dataclass
class Version:
    id: str                       # canonical id used in URLs (e.g. "1.21.8")
    label: str                    # human label for the dropdown
    channel: Optional[str] = None  # e.g. "release" (informational)


@dataclass
class Build:
    id: str                       # e.g. "60"
    label: str                    # e.g. "Build 60"
    channel: Optional[str] = None  # e.g. "STABLE"


@dataclass
class Download:
    """A fully-resolved binary the agent can fetch and install."""

    url: str
    filename: str                       # upstream filename (informational)
    version: str
    build: Optional[str] = None
    size: Optional[int] = None
    checksum: Optional[str] = None      # hex digest when the provider has one
    checksum_algo: Optional[str] = None  # "sha256" | "sha1"


class Provider(ABC):
    """Interface every server software implements.

    ``has_builds`` tells the UI whether to show a build selector — the interface
    adapts to the metadata each software exposes rather than assuming builds.
    """

    software: str
    label: str
    has_builds: bool

    @abstractmethod
    async def versions(self) -> list[Version]:
        """Available versions, newest first."""

    @abstractmethod
    async def builds(self, version: str) -> list[Build]:
        """Available builds for a version, newest first. ``[]`` if not applicable."""

    @abstractmethod
    async def resolve(self, version: str, build: str | None) -> Download:
        """Resolve a concrete download. ``build`` is ignored when ``has_builds``
        is false, and defaults to the newest build when omitted."""
