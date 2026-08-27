"""
Symmetric encryption for the one secret that has to be stored recoverable: the
SMTP relay password.

Fernet with a server-side key from FERNET_KEY (generate once with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
Same pattern as borant-id/crypto.py and survey/crypto.py, deliberately — this is
not the place to invent something.

**The key is optional here, and that is a decision rather than an oversight.**
Mail in this app is a degradable dependency: without a relay the list works
exactly the same and invitation links appear on screen. So a missing FERNET_KEY
must not stop the app from booting — it makes the mail section of /admin
unusable, says so in as many words, and leaves everything else alone. What it
must never do is fail quietly: `available()` is checked before the form is drawn
and again before a password is saved.

User passwords are NOT handled here: those are bcrypt-hashed and never
recovered.
"""
import logging
import os

log = logging.getLogger("thelist.crypto")

_fernet = None
_key = os.environ.get("FERNET_KEY", "").strip()
if _key:
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_key.encode())
    except Exception as e:  # noqa: BLE001 — a bad key must not stop the boot
        log.warning("FERNET_KEY present but unusable (%s): mail configuration "
                    "will be disabled until it is fixed", type(e).__name__)


def available() -> bool:
    return _fernet is not None


def encrypt(plain: str) -> str:
    if _fernet is None:
        raise RuntimeError("no FERNET_KEY")
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_or_none(encrypted: str | None) -> str | None:
    """For call sites where a rotated or corrupt key must degrade instead of
    crashing the request — an unreadable SMTP password means "no relay", not
    a 500 on somebody adding a note."""
    if not encrypted or _fernet is None:
        return None
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except Exception:  # noqa: BLE001
        return None
