"""Encrypt saved database passwords, so they are never stored as plain text.

The key comes from SECRET_KEY in .env if set, otherwise from data/secret.key (created on first use, git-ignored,
shared by the api and worker containers because data/ is a mounted folder).
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILE = Path("data") / "secret.key"


def _fernet() -> Fernet:
    key = os.getenv("SECRET_KEY")
    if not key:
        if not KEY_FILE.exists():
            KEY_FILE.parent.mkdir(exist_ok=True)
            KEY_FILE.write_bytes(Fernet.generate_key())
        key = KEY_FILE.read_bytes()
    return Fernet(key)


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
