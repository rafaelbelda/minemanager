"""Agent enrollment tokens and long-lived credentials.

Both are high-entropy random strings, so we store only a SHA-256 hash and
compare in constant time — no bcrypt needed (these aren't human passwords).

Enrollment flow:
1. Operator creates a node -> hub mints a one-time enrollment token (shown once).
2. Agent connects with the enrollment token -> hub verifies, then issues a
   long-lived per-agent credential and clears the enrollment token.
3. On every reconnect the agent presents the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_token(nbytes: int = 32) -> str:
    """Return a fresh urlsafe random token."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token, for at-rest storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, stored_hash: str | None) -> bool:
    """Constant-time compare a presented token against a stored hash."""
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)
