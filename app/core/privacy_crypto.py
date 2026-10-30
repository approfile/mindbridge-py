"""Authenticated encryption for application-owned free text, not search keys.

The key lives in a private mounted file, never a source constant or DB column.
Missing/wrong keys and plaintext after migration fail closed on ORM reads.
"""
import hashlib
import os
from pathlib import Path
import stat
import threading
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings

PREFIX = 'mbenc:v1:'
_LOCK = threading.RLock()
_CIPHERS = {}
_EXPECTED_KEYS = {}


class PrivacyStorageError(RuntimeError):
    pass


def key_path(settings=None):
    settings = settings or get_settings()
    path = Path(settings.privacy_key_file)
    return path if path.is_absolute() else settings.project_root / path


def cipher(settings=None, *, create=True):
    path = key_path(settings)
    with _LOCK:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create or str(path) in _EXPECTED_KEYS:
                raise PrivacyStorageError('Sensitive storage key missing') from None
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.privacy-key-')
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(Fernet.generate_key()); output.flush(); os.fsync(output.fileno())
                try:os.link(temporary,path)  # atomic publication; never replaces a key
                except FileExistsError:pass
            finally:Path(temporary).unlink(missing_ok=True)
            info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise PrivacyStorageError('Sensitive storage key must be a private regular file (0600)')
        stamp = (str(path), info.st_ino, info.st_mtime_ns, info.st_size)
        if stamp not in _CIPHERS:
            try:
                raw = path.read_bytes().strip()
                _CIPHERS[stamp] = (Fernet(raw), hashlib.sha256(raw).hexdigest()[:24])
            except (ValueError, OSError):
                raise PrivacyStorageError('Sensitive storage key unavailable or invalid') from None
        value=_CIPHERS[stamp]
        expected=_EXPECTED_KEYS.setdefault(str(path),value[1])
        if expected!=value[1]:
            raise PrivacyStorageError('Sensitive storage key changed; explicit offline rotation required')
        return value


def encrypt_text(value, settings=None):
    if value is None:
        return None
    # Always encrypt: a caller-supplied prefix is ordinary input, never trusted
    # as a pre-encrypted value (avoids ciphertext injection/double-use).
    return PREFIX + cipher(settings,create=False)[0].encrypt(str(value).encode()).decode('ascii')


def decrypt_text(value, settings=None):
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise PrivacyStorageError('Unmigrated sensitive storage')
    try:
        return cipher(settings, create=False)[0].decrypt(value[len(PREFIX):].encode('ascii')).decode()
    except (InvalidToken, ValueError, UnicodeError):
        raise PrivacyStorageError('Sensitive storage authentication failed') from None


class SensitiveText(TypeDecorator):
    impl = Text
    cache_ok = True

    class comparator_factory(Text.Comparator):
        def __eq__(self, other):
            if other is None:return super().__eq__(other)
            raise PrivacyStorageError('Encrypted free text is not a searchable database key')

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(MEDIUMTEXT() if dialect.name == 'mysql' else Text())

    def process_bind_param(self, value, dialect):
        return encrypt_text(value)

    def process_result_value(self, value, dialect):
        return decrypt_text(value)
