"""
Configuration that lives in the database instead of the environment.

Today that means the SMTP relay, set from /admin after the app is already
running: the mailbox is not known at deploy time and does not belong in a
compose file. Same reasoning as borant-id.

**The database is the only source for these, and the environment is not
consulted at all.** A fallback chain would be the comfortable choice and the
wrong one: the day mail stops working, "which of the two is winning" is a
question nobody can answer from the screen, and the answer would depend on
whether somebody once exported a variable. One place to look.

Everything security-critical stays in the environment — JWT_SECRET, FERNET_KEY —
because those are needed before the first request and must never be editable
from a web form.
"""
from crypto import decrypt_or_none, encrypt
from models import Setting

DEFAULTS = {
    "smtp_enabled": "0",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_security": "starttls",     # starttls | ssl | none
    "smtp_username": "",
    "smtp_password_enc": "",         # Fernet, write-only in the form
    "smtp_from_email": "",
    "smtp_from_name": "TheList",
}

SECURITIES = ["starttls", "ssl", "none"]


def get(db, key: str, default: str | None = None) -> str:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, "" if default is None else default)


def get_bool(db, key: str) -> bool:
    return get(db, key).strip() in ("1", "true", "yes", "on")


def get_int(db, key: str, default: int = 0) -> int:
    try:
        return int(get(db, key))
    except (TypeError, ValueError):
        return default


def put(db, key: str, value: str, actor=None) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value,
                       updated_by=actor.id if actor else None))
    else:
        row.value = value
        row.updated_by = actor.id if actor else None


def smtp_config(db) -> dict:
    """Everything the mailer needs, password already decrypted."""
    return {
        "enabled": get_bool(db, "smtp_enabled"),
        "host": get(db, "smtp_host").strip(),
        "port": get_int(db, "smtp_port", 587),
        "security": get(db, "smtp_security").strip() or "starttls",
        "username": get(db, "smtp_username").strip(),
        "password": decrypt_or_none(get(db, "smtp_password_enc")) or "",
        "from_email": get(db, "smtp_from_email").strip(),
        "from_name": get(db, "smtp_from_name").strip() or "TheList",
    }


def has_password(db) -> bool:
    """Whether one is stored — never what it is. The form shows this and nothing
    more, which is the whole point of an admin setting the relay up once for
    everybody: they all send mail through it, none of them can read it."""
    return bool(get(db, "smtp_password_enc"))


def set_smtp_password(db, plain: str, actor=None) -> str:
    """Codes, not sentences: `ok`, `unchanged`, `no_key`.

    An empty field means "leave it alone" and never "clear it": the form does not
    round-trip the password back to the browser, so a blank box is the normal
    state of a saved relay, not an instruction. Clearing is its own button.
    """
    if not plain:
        return "unchanged"
    try:
        put(db, "smtp_password_enc", encrypt(plain), actor=actor)
    except RuntimeError:
        return "no_key"
    return "ok"


def clear_smtp_password(db, actor=None) -> None:
    put(db, "smtp_password_enc", "", actor=actor)
