"""
Outbound mail through somebody else's SMTP relay. No mail server here.

**Mail is a degradable dependency.** Nothing in this module raises. Every caller
gets `(ok, code)` and the app behaves identically when the relay is dead: the
in-app dot is the truth, the mail is a reminder. A tool that stops being usable
because the post is off has put the post on the critical path, which is a
mistake you only get to make once.

**Every message is in English, and every message lives here.** When we write to
somebody we usually do not know their language — an invitation goes out before
that person has ever touched the interface, and it is the first thing they see
of the system. Keeping the copy in one file is what makes "all mail is in
English" a claim you can check by reading one module instead of grepping four.

`send()` returns a **code**, never a sentence: `ok`, `smtp_off`,
`smtp_incomplete`, `no_recipient`, `auth_refused <smtp code>`, `unreachable
<raw>`. Whoever produces an error does not know what language it will be read
in; whoever renders the page does. The raw library text passes through
untranslated on purpose — `ConnectError: [Errno 111]` is nobody's language and
translating it would only make it harder to search for.

Configured from the environment, unlike Borant ID: there the mailbox was not
known at deploy time, here it already exists.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from models import NOTIFICATIONS, NotificationPref

log = logging.getLogger("thelist.mail")

TIMEOUT = 15


def _cfg() -> dict:
    return {
        "enabled": os.environ.get("SMTP_ENABLED", "").strip().lower() in ("1", "true", "yes"),
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "security": os.environ.get("SMTP_SECURITY", "starttls").strip().lower(),
        "username": os.environ.get("SMTP_USERNAME", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from_email": os.environ.get("SMTP_FROM", "").strip(),
        "from_name": os.environ.get("SMTP_FROM_NAME", "TheList").strip(),
    }


def send(to: str, subject: str, body: str) -> tuple[bool, str]:
    """(ok, code). Never raises — see the module docstring."""
    cfg = _cfg()
    if not cfg["enabled"]:
        return False, "smtp_off"
    if not cfg["host"] or not cfg["from_email"]:
        return False, "smtp_incomplete"
    if not to:
        return False, "no_recipient"

    msg = EmailMessage()
    msg["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if cfg["security"] == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=TIMEOUT,
                                  context=ctx) as s:
                if cfg["username"]:
                    s.login(cfg["username"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=TIMEOUT) as s:
                if cfg["security"] == "starttls":
                    s.starttls(context=ssl.create_default_context())
                if cfg["username"]:
                    s.login(cfg["username"], cfg["password"])
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        return False, f"auth_refused {e.smtp_code}"
    except Exception as e:  # noqa: BLE001 — nothing here may raise
        return False, f"unreachable {type(e).__name__}: {e}"
    return True, "ok"


def wants(db, user, event_type: str) -> bool:
    """Whether this person asked to hear about this kind of thing."""
    if user is None or not user.email or not user.is_active:
        return False
    pref = (db.query(NotificationPref)
              .filter(NotificationPref.user_id == user.id,
                      NotificationPref.event_type == event_type).first())
    if pref is not None:
        return bool(pref.enabled)
    return NOTIFICATIONS.get(event_type, False)


def notify(db, user, event_type: str, subject: str, body: str) -> tuple[bool, str]:
    """Send if the recipient wants it. Failures are logged, never surfaced as
    errors to whoever triggered them: their action succeeded either way."""
    if not wants(db, user, event_type):
        return False, "opted_out"
    ok, code = send(user.email, subject, body)
    if not ok and code != "smtp_off":
        log.warning("mail %s to %s failed: %s", event_type, user.email, code)
    return ok, code


def base_url() -> str:
    return os.environ.get("PUBLIC_URL", "http://localhost:8020").rstrip("/")


# ── the copy, all of it, in one place ─────────────────────────────────────────

def proposal_received(proposer_name: str, title: str, ws_id: int) -> tuple[str, str]:
    return (
        f"New proposal on your list: {title}",
        f"{proposer_name} proposed a task for your list:\n\n"
        f"  {title}\n\n"
        f"Accept or decline it here:\n"
        f"  {base_url()}/app/{ws_id}/proposals\n",
    )


def proposal_accepted(owner_name: str, title: str, ws_id: int) -> tuple[str, str]:
    return (
        f"Accepted: {title}",
        f"{owner_name} accepted your proposal:\n\n"
        f"  {title}\n\n"
        f"It is now on the list:\n"
        f"  {base_url()}/app/{ws_id}\n",
    )


def proposal_declined(owner_name: str, title: str, reason: str,
                      ws_id: int) -> tuple[str, str]:
    return (
        f"Declined: {title}",
        f"{owner_name} declined your proposal:\n\n"
        f"  {title}\n\n"
        f"Reason given:\n"
        f"  {reason}\n\n"
        f"It stays in the archive, with the reason, here:\n"
        f"  {base_url()}/app/{ws_id}/proposals\n",
    )


def invitation(inviter_name: str, ws_name: str, token: str) -> tuple[str, str]:
    return (
        f"{inviter_name} invited you to {ws_name}",
        f"{inviter_name} invited you to work on their list on TheList.\n\n"
        f"As an editor you can add notes, reorder the list, mark things done and\n"
        f"propose new tasks. Creating and deleting them stays with the owner.\n\n"
        f"Accept the invitation here:\n"
        f"  {base_url()}/invite/{token}\n",
    )


def due_digest(rows: list) -> tuple[str, str]:
    """One message a person a day, not one per task."""
    lines = []
    for t in rows:
        when = t.due_date.isoformat() if t.due_date else "no date"
        lines.append(f"  [{when}] {t.title}")
    return (
        f"Due within a week: {len(rows)} item{'s' if len(rows) != 1 else ''}",
        "These are coming up on your list:\n\n" + "\n".join(lines) +
        f"\n\n  {base_url()}/app\n",
    )
