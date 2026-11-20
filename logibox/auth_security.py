"""Shared, backward-compatible password hashing and local recovery exclusion."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + '$' + hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200000).hex()


def check_pw(pw, stored):
    if not isinstance(stored, str) or '$' not in stored:
        return False
    return secrets.compare_digest(hash_pw(pw, stored.split('$')[0]), stored)


@contextmanager
def data_lock(data):
    """OS releases the lock even if the server/recovery process crashes."""
    path = Path(data) / '.logibox-auth.lock'
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('LOGIBOXまたは復旧ツールが起動中です。PCを再起動し、LOGIBOXを開かずに再設定してください。') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
