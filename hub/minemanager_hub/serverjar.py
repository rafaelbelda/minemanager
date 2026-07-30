"""Work out which file on disk a start command actually executes.

The updater replaces the *server executable*, so it has to know exactly which
file that is. A wrong answer is worse than no answer: writing a fresh jar to a
filename nothing runs leaves the server booting the old binary while the hub
records the new version, and nothing anywhere reveals the divergence (there is
no independent version detector until v2 — see ``PLAN.md §11``).

So resolution is explicit about how sure it is:

``explicit``
    The operator set ``Instance.jar_path``. Authoritative — they can see the
    files; we cannot read them from here.
``parsed``
    Unambiguously derived from the start command.
``unknown``
    Not determinable. The caller must refuse rather than guess.

Deliberately **not** guessed:

* ``@argfile`` launches (Forge/NeoForge 1.17+) — the real classpath is named
  inside the file, not on the command line.
* ``-cp`` with several candidate jars — any pick would be arbitrary.
* wrapper scripts (``./start.sh``) — the command starts a shell, and the
  executable is chosen inside the script.

For those the operator sets ``jar_path`` once, and it is used from then on. That
is a far better trade than a default filename that is silently wrong: the old
code fell back to ``paper.jar`` / ``server.jar`` / ``velocity.jar`` whenever
parsing failed, i.e. exactly when it was least likely to be right.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal

Confidence = Literal["explicit", "parsed", "unknown"]

# `java` spells the classpath flag three ways.
_CP_FLAGS = {"-cp", "-classpath", "--class-path"}


@dataclass(frozen=True)
class ServerExecutable:
    """Where the server executable is, and how much we trust that."""

    path: str | None          # relative to the instance root; None when unknown
    confidence: Confidence

    @property
    def allow_create(self) -> bool:
        """Whether the agent may create this file when it is missing.

        Only for an operator-supplied path. A *parsed* path might be a mis-parse,
        and creating a new jar on a mis-parse is precisely the silent divergence
        this module exists to prevent — if the parse is right the file is already
        there, because the server has been running it.
        """
        return self.confidence == "explicit"


def _tokenize(start_command: str) -> list[str]:
    """Split a start command the way a shell would.

    ``shlex`` is what makes quoted paths work (``-jar "my server.jar"``), which a
    ``-jar\\s+(\\S+)`` regex got wrong by capturing ``"my``. Note it treats ``\\``
    as an escape, so Windows-style paths would mangle — irrelevant here, since
    every node is Linux (``PLAN.md §2``).
    """
    try:
        return shlex.split(start_command or "")
    except ValueError:            # unbalanced quotes — degrade, don't raise
        return (start_command or "").split()


def _is_java(token: str) -> bool:
    return token == "java" or token.endswith("/java")


def resolve(start_command: str, jar_path: str | None = None) -> ServerExecutable:
    """Resolve the server executable for an instance.

    ``jar_path`` is the operator's override and always wins when set.
    """
    if jar_path and jar_path.strip():
        return ServerExecutable(jar_path.strip(), "explicit")

    tokens = _tokenize(start_command)
    unknown = ServerExecutable(None, "unknown")
    if not tokens:
        return unknown

    # Not a JVM invocation at all — a wrapper script, or something exotic. The
    # executable is decided inside it.
    if not any(_is_java(t) for t in tokens):
        return unknown

    # An argfile hides the classpath entirely.
    if any(t.startswith("@") for t in tokens):
        return unknown

    # `-jar` is the unambiguous form; scan for it first so ordering cannot matter
    # (it is mutually exclusive with -cp in `java`, but do not rely on that).
    for i, tok in enumerate(tokens):
        if tok == "-jar" and i + 1 < len(tokens):
            return ServerExecutable(tokens[i + 1], "parsed")
        if tok.startswith("-jar="):        # `java` rejects this form; accept it anyway
            return ServerExecutable(tok[len("-jar="):], "parsed")

    # Classpath launch, e.g. `java -cp server.jar net.minecraft.server.Main`.
    # Wildcard entries (`libraries/*`) are dependency bundles, not the
    # executable, so they are ignored — but if that leaves more than one real jar
    # the choice would be arbitrary, so give up instead.
    for i, tok in enumerate(tokens):
        if tok in _CP_FLAGS and i + 1 < len(tokens):
            jars = [
                e for e in tokens[i + 1].split(":")
                if e.endswith(".jar") and "*" not in e
            ]
            return ServerExecutable(jars[0], "parsed") if len(jars) == 1 else unknown

    return unknown


CANNOT_RESOLVE_DETAIL = (
    "could not determine which jar this instance runs. Its start command does not name "
    "one directly (a wrapper script, an @argfile launch, or an ambiguous -cp). Set the "
    "instance's 'Jar path' — the executable's path relative to the instance root, e.g. "
    "'paper.jar' — and retry. Refusing rather than guessing: installing to a guessed "
    "filename would leave the server running its old binary while reporting success."
)
