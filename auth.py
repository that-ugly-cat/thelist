"""
Authentication and authorization for TheList.

Auth (borant house pattern, same as PaperTrail and LSSR): JWT in an httpOnly
cookie named 'session', 7-day lifetime, secret from JWT_SECRET — startup crashes
if it is missing, because a default secret is worse than no app.

Two ways of recognising a user, and `local` is the default on purpose: an app
that believes an identity header with nothing in front of it hands its identity
to anyone who can write that header. The gateway path stays dead code until
somebody turns it on deliberately, and even then the headers are believed only
when they arrive from the proxy address we were told to trust.

Authorization is one function. Every workspace-scoped route goes through
`workspace_dep(minimum)`, which resolves (user, workspace) -> role once and
raises **404** when there is no membership — never 403, because a 403 confirms
that the workspace exists. Templates never check permissions: they receive the
resolved `role` and only decide what to draw.
"""
import ipaddress
import logging
import os
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from models import (
    ApiKey, Membership, User, Workspace, bootstrap_admin, ensure_workspace,
    get_db, has_role, role_for, utcnow,
)

log = logging.getLogger("thelist.auth")

SECRET_KEY = os.environ["JWT_SECRET"]
ALGORITHM = "HS256"
EXPIRE_DAYS = 7

AUTH_MODE = os.environ.get("AUTH_MODE", "local").strip().lower()

# In gateway mode identity headers are believed only from here — the reverse
# proxy, never the internet. Under Docker this is a bridge gateway and NOT
# 127.0.0.1; DEPLOY.md shows how to read the real value off a running container.
TRUSTED_PROXY = os.environ.get("BORANT_TRUSTED_PROXY", "127.0.0.1")


def _parse_trusted(raw: str) -> list:
    nets = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            log.warning("BORANT_TRUSTED_PROXY: ignoring %r, not an address or CIDR", chunk)
    return nets


TRUSTED_PROXIES = _parse_trusted(TRUSTED_PROXY)


def gateway_mode() -> bool:
    return AUTH_MODE == "gateway"


def _from_trusted_proxy(request: Request) -> bool:
    peer = request.client.host if request.client else None
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(days=EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY,
                      algorithm=ALGORITHM)


def _decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid session")


def user_from_gateway(request: Request, db: Session) -> User | None:
    """The user the gate vouched for, or None.

    Lookup is by `borant_sub` and never by email: a typo in the gate's admin
    panel must not be able to hand one person another person's board. An unknown
    subject gets a fresh profile with its own empty board — the failure mode is
    an empty screen, not a leak.
    """
    if not gateway_mode():
        return None
    sub = request.headers.get("x-borant-sub")
    if not sub:
        return None
    if not _from_trusted_proxy(request):
        log.warning("X-Borant-Sub from %s, outside BORANT_TRUSTED_PROXY (%s): ignored",
                    request.client.host if request.client else "?", TRUSTED_PROXY)
        return None

    user = db.query(User).filter(User.borant_sub == sub).first()
    if user is not None:
        return user if user.is_active else None

    email = (request.headers.get("x-borant-email", "")
             or f"{sub}@borant.invalid").strip().lower()
    # A local password nobody knows, rather than none: AUTH_MODE=local has to
    # remain a working way back, and a row with no password is not a way back.
    existing = db.query(User).filter(User.email == email).first()
    if existing is not None and existing.borant_sub is None:
        # Same address, no link yet: this is the local account of the person the
        # gate is vouching for. Linking here is safe because the address comes
        # from the gate and not from the client — but it happens once, and it is
        # logged, so a wrong link is visible rather than silent.
        existing.borant_sub = sub
        db.commit()
        log.info("gateway: linked existing local account %s to %s", email, sub)
        return existing if existing.is_active else None

    # The gate may suggest `admin`, and it is honoured — with the same caution
    # PaperTrail uses: an unknown hint is a typo, not a role, and grants nothing.
    #
    # The deviation from "never provision privilege from a header" is narrow and
    # rests on what admin means HERE: it configures the mail relay and can
    # deactivate accounts. It does **not** open anybody's list — those are
    # membership rows and an admin has none. So the worst a wrong hint buys is
    # somebody editing an SMTP host, not reading a private board.
    hint = (request.headers.get("x-borant-hint", "") or "").strip().lower()
    wants_admin = hint == "admin"
    if hint and not wants_admin:
        log.warning("gateway: hint %r is not a role in this app, ignored", hint)

    user = User(email=email,
                name=request.headers.get("x-borant-name", "") or email,
                hashed_password=hash_password(secrets.token_urlsafe(32)),
                borant_sub=sub, is_active=True, is_admin=wants_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    # Somebody has to be able to configure the relay: the first account to exist
    # gets it. See models.bootstrap_admin — whoever walks in first.
    if bootstrap_admin(db, user):
        log.info("gateway: %s is the first account, made admin", email)
    ensure_workspace(db, user)
    db.commit()
    log.info("gateway: new profile for %s (%s), admin=%s", email, sub, user.is_admin)
    return user


def get_current_user(
    request: Request,
    session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    if gateway_mode():
        # The header wins over a local cookie, always: a leftover cookie must not
        # outlive a session the gate has revoked.
        user = user_from_gateway(request, db)
        if user is not None:
            touch(db, user)
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")
    user_id = _decode_token(session)
    user = db.query(User).filter(User.id == user_id,
                                 User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User not found")
    touch(db, user)
    return user


def touch(db: Session, user: User) -> None:
    now = utcnow()
    if user.last_seen_at is None or (now - user.last_seen_at).total_seconds() > 3600:
        user.last_seen_at = now
        db.commit()


def require_admin(user: User = Depends(get_current_user)) -> User:
    """The administration level, and what it deliberately is not.

    An admin configures the mail relay everybody sends through, and can
    deactivate an account. **It grants no access to anybody's list**: reaching a
    board is a `Membership` row and an admin has none, so /admin is a different
    axis from the boards rather than a level above them. Without that separation
    "administrator" would quietly mean "reads everyone's coordination with their
    colleagues", which is not what anyone is asking for when they ask for an
    admin level.
    """
    if not user.is_admin:
        raise HTTPException(status_code=404, detail="Not found")
    return user


class WorkspaceAccess:
    """(workspace, role) for one request, already checked."""

    def __init__(self, workspace: Workspace, role: str, user: User):
        self.workspace = workspace
        self.role = role
        self.user = user

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def workspace_dep(minimum: str = "editor"):
    """One access function, used by web routes and by MCP alike.

    404 and never 403: an app that answers 403 tells whoever asked that the
    board exists.
    """
    def dep(workspace_id: int,
            user: User = Depends(get_current_user),
            db: Session = Depends(get_db)) -> WorkspaceAccess:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        role = role_for(db, workspace_id, user) if ws else None
        if ws is None or role is None or not has_role(role, minimum):
            raise HTTPException(status_code=404, detail="Not found")
        return WorkspaceAccess(ws, role, user)
    return dep


def access_or_404(db: Session, workspace_id: int, user: User,
                  minimum: str = "editor") -> WorkspaceAccess:
    """The same check, callable outside the dependency machinery (MCP)."""
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    role = role_for(db, workspace_id, user) if ws else None
    if ws is None or role is None or not has_role(role, minimum):
        raise HTTPException(status_code=404, detail="Not found")
    return WorkspaceAccess(ws, role, user)


# ── MCP callers ───────────────────────────────────────────────────────────────

_caller: ContextVar = ContextVar("caller", default=None)


def set_caller(user: User | None) -> None:
    _caller.set(user.id if user is not None else None)


def caller_id() -> int | None:
    return _caller.get()


def check_api_key(db: Session, key: str) -> ApiKey | None:
    if not key:
        return None
    row = (db.query(ApiKey)
             .filter(ApiKey.key == key, ApiKey.revoked_at.is_(None)).first())
    if row is None or not row.user or not row.user.is_active:
        return None
    row.last_used_at = utcnow()
    db.commit()
    return row
