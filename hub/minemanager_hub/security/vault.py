"""Symmetric encryption for in-app secrets (Fernet / AES-128-CBC + HMAC).

The key comes from ``MM_SECRET_KEY`` in the environment (production: systemd
EnvironmentFile or a secret manager). For dev convenience, if it is unset we
generate one once and persist it to ``<data_dir>/secret.key`` with 0600 perms —
this keeps local runs frictionless while making the "bring your own key in prod"
path explicit.

``MM_SECRET_KEY`` must be a urlsafe-base64 32-byte Fernet key. Generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from minemanager_hub.config import get_settings

_fernet: Fernet | None = None


def _load_key() -> bytes:
    settings = get_settings()
    if settings.secret_key:
        return settings.secret_key.encode()

    key_file = settings.secret_key_file
    if key_file.exists():
        return key_file.read_bytes().strip()

    # Dev fallback: mint and persist a key with tight perms.
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    try:
        os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600 (no-op on Windows)
    except OSError:
        pass
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string, returning urlsafe-base64 ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext produced by :func:`encrypt`.

    Raises ``ValueError`` if the ciphertext is invalid or the key is wrong.
    """
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - defensive
        raise ValueError("could not decrypt secret (wrong key or corrupt data)") from exc
