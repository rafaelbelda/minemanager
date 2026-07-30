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

import logging
import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from minemanager_hub.config import get_settings

log = logging.getLogger("minemanager.hub")

_fernet: Fernet | None = None
_key_source: str = "unknown"


def _load_key() -> bytes:
    global _key_source
    settings = get_settings()
    if settings.secret_key:
        _key_source = "MM_SECRET_KEY"
        return settings.secret_key.encode()

    key_file = settings.secret_key_file
    if key_file.exists():
        _key_source = str(key_file)
        return key_file.read_bytes().strip()

    # Dev fallback: mint and persist a key, created 0600 from the outset (writing
    # first and chmod-ing after left it world-readable under a default umask).
    key = Fernet.generate_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    _key_source = f"{key_file} (newly generated)"
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = _load_key()
        try:
            _fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            # Previously this surfaced on the first encrypt/decrypt — i.e. the hub
            # started fine and then failed at runtime on a real request.
            raise SystemExit(
                f"secret vault: key from {_key_source} is not a valid Fernet key ({exc}). "
                'Generate one with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from None
    return _fernet


def verify_existing_secrets_readable() -> None:
    #Fail fast at startup if the configured key cannot read stored secrets.
    from minemanager_hub.db.models import Secret
    from minemanager_hub.db.session import session_scope

    _get_fernet()  # also validates the key format

    with session_scope() as db:
        row = db.query(Secret).order_by(Secret.created_at).first()
        if row is None:
            log.info("secret vault: key from %s (no stored secrets yet)", _key_source)
            return
        try:
            decrypt(row.ciphertext)
        except ValueError:
            raise SystemExit(
                f"secret vault: the key from {_key_source} cannot decrypt the secrets already "
                f"stored in this database. Refusing to start - every instance command would "
                f"fail once a secret is read. Restore the original key, or clear and re-enter "
                f"the stored secrets."
            ) from None
    log.info("secret vault: key from %s verified against stored secrets", _key_source)


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
