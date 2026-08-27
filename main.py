"""
TheList — a list of macro-tasks, owned by one person and annotated by another.

Surfaces: the web app (session cookie, or an SSO gate in front of it), and
**/mcp** for the model, gated by a per-user key. The MCP key carries an identity,
so the model reaches exactly what its owner reaches, no more (mcp_app.py).

What this is not, and it shapes every route below: not a task manager. No
sub-tasks, no generated instances, no scheduler. A row is a thing worth
remembering exists.

Fase 1: the list with drag-and-drop, proposals, notes, people, tags, the report,
invitations, mail, and the read-only MCP surface.
"""
import contextlib
import csv
import io
import json
import logging
import os
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import mailer
from auth import (
    WorkspaceAccess, check_api_key, create_token,
    gateway_mode as auth_gateway_mode, get_current_user, hash_password,
    set_caller, verify_password, workspace_dep,
)
from models import (
    DEFAULT_EFFORT, EFFORTS, EFFORT_LABELS, EFFORT_WEIGHTS, NOTIFICATIONS,
    NOTIFICATION_LABELS, ROLE_LABELS, STATUS_LABELS,
    ApiKey, Event, Invitation, Membership, Note, NotificationPref, Person,
    SessionLocal, Task, Tag, User, Workspace,
    active_tasks, apply_order, ensure_workspace, get_or_create_person, get_db,
    init_db, log_event, mark_done, new_api_key, new_invite_token, next_position,
    normalise, people_of, person_for_proposal, reopen, report, role_for,
    self_person, set_tags, tags_of, utcnow, workspaces_of,
)

log = logging.getLogger("thelist")

# ── public paths, declared in one place ───────────────────────────────────────
#
# Everything else is gated. Declared here and nowhere else so that the day
# somebody adds a public route they notice while writing it; `caddy.py` derives
# the reverse-proxy block by reading this list rather than by remembering it.
#
# The trap worth naming (it cost ArguMap a real incident): check that no private
# route is a LONGER PREFIX of a public one. Here every private route lives under
# /app, and nothing public starts with /app, so the property holds by
# construction rather than by attention.
PUBLIC_PATHS = ["/", "/health", "/static/*", "/login", "/logout", "/invite/*"]

# Not public — these carry a per-user key — but they must not go through the
# gate either: they talk to programs, and a redirect to a login page is the last
# thing an MCP client can handle. In the Caddy block they sit in the same matcher
# as the public paths, which is what makes `noforge` apply to them: that snippet
# is where the X-Borant-* headers get stripped, and it lives only on the branches
# that skip the gate. Borant ID paid for that lesson once already — on a public
# path, a forged identity header went straight through.
MACHINE_PATHS = ["/mcp", "/mcp/*"]

from mcp_app import mcp  # noqa: E402


async def _due_digest_loop():
    """One message a person a day, not one per task (SPEC.md §7).

    Same shape as Grant Radar's link monitor: a loop in the lifespan, not a cron
    outside the container.
    """
    while True:
        try:
            await asyncio.sleep(3600)
            now = utcnow()
            if now.hour != 7:            # roughly once a day, UTC morning
                continue
            db = SessionLocal()
            try:
                horizon = date.today() + timedelta(days=7)
                for ws in db.query(Workspace).all():
                    rows = [t for t in active_tasks(db, ws).all()
                            if t.due_date and t.due_date <= horizon]
                    if not rows:
                        continue
                    subject, body = mailer.due_digest(rows)
                    owner = db.query(User).get(ws.owner_user_id)
                    mailer.notify(db, owner, "due_soon", subject, body)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a digest must never kill the app
            log.exception("due digest failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(_due_digest_loop())
    # The MCP session manager runs inside the parent app's lifespan: mounts do
    # not propagate lifespans, and without the transport the surface answers 500
    # without saying why.
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


BASE = Path(__file__).parent
app = FastAPI(title="TheList", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals.update(
    EFFORTS=EFFORTS, EFFORT_LABELS=EFFORT_LABELS, EFFORT_WEIGHTS=EFFORT_WEIGHTS,
    STATUS_LABELS=STATUS_LABELS, ROLE_LABELS=ROLE_LABELS,
    NOTIFICATION_LABELS=NOTIFICATION_LABELS, NOTIFICATIONS=NOTIFICATIONS,
    today=date.today,
)


# ── MCP mount ─────────────────────────────────────────────────────────────────

def _allowed_hosts() -> list[str]:
    """The transport checks Host against DNS rebinding, so the public domain has
    to be allowed or every Caddy-proxied request is refused — a tool that looks
    broken and a variable that is missing."""
    from urllib.parse import urlparse
    hosts = ["localhost:8020", "127.0.0.1:8020", "localhost", "127.0.0.1"]
    public = urlparse(os.environ.get("PUBLIC_URL", "")).netloc
    if public:
        hosts.append(public)
    return hosts


from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

app.mount("/mcp", mcp.streamable_http_app(
    streamable_http_path="/", json_response=True, stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_allowed_hosts(),
        allowed_origins=[os.environ.get("PUBLIC_URL", "http://localhost:8020")])))


@app.middleware("http")
async def api_key_gate(request: Request, call_next):
    """Resolve the MCP caller, or refuse.

    Two ways in, one table: the header is the normal path, and /mcp/k/{key}
    carries the same key as a path segment for clients that cannot set headers.
    The key is stripped before the mounted app sees it, so the MCP layer stays
    unaware of how the caller authenticated. In the URL form the key lands in
    access logs, which is why keys are per-client and revocable.

    The trailing-slash normalisation matters more than it looks: the endpoint we
    advertise is the one WITHOUT the slash, a Starlette mount answers it with a
    307, and MCP clients do not follow redirects on POST — behind TLS
    termination it is worse, because the app does not know it is on https and
    builds an http:// redirect.
    """
    path = request.url.path
    if not path.startswith("/mcp"):
        return await call_next(request)

    if path.startswith("/mcp/k/"):
        key, _, rest = path[len("/mcp/k/"):].partition("/")
        request.scope["path"] = "/mcp/" + rest
        request.scope["raw_path"] = request.scope["path"].encode()
    else:
        key = request.headers.get("X-API-Key", "")
        if path == "/mcp":
            request.scope["path"] = "/mcp/"
            request.scope["raw_path"] = b"/mcp/"

    db = SessionLocal()
    try:
        row = check_api_key(db, key)
        set_caller(row.user if row else None)
    finally:
        db.close()
    if not row:
        return JSONResponse({"error": "missing or invalid API key"}, status_code=401)
    return await call_next(request)


def _wants_page(request: Request) -> bool:
    """Is this a navigation, or a script call?

    Decided on `Sec-Fetch-Mode`, which is the only signal that actually knows —
    and in the absence of evidence the answer is "a page". PaperTrail learned
    this the expensive way: classifying `Accept: */*` as XHR answered 401 to
    everything that was really a person typing a URL, and browsers were the only
    clients that happened to escape it.
    """
    mode = request.headers.get("sec-fetch-mode")
    if mode:
        return mode == "navigate"
    return "application/json" not in request.headers.get("accept", "")


@app.exception_handler(HTTPException)
async def http_errors(request: Request, exc: HTTPException):
    accepts_html = _wants_page(request)
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and accepts_html:
        if auth_gateway_mode():
            # Never a redirect to /login here: in gateway mode this app turns
            # /login off, and sending a 401 there would close a loop —
            # /app -> 401 -> /login -> /app — that nobody can get out of.
            exc = HTTPException(status_code=503, detail=(
                "Gateway mode: no valid identity in the X-Borant-* headers. Check that "
                "the gate really sits in front of this app and that BORANT_TRUSTED_PROXY "
                "lists the address the proxy connects from."))
        else:
            return RedirectResponse("/login", status_code=302)
    if accepts_html:
        return templates.TemplateResponse(
            request, "error.html",
            {"user": None, "code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ── public surface ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """A public shop window that never looks at who is reading it.

    Not a stylistic rule: on the public branch the identity headers are stripped
    by construction, so an `{% if user %}` here would be always false with the
    gate and sometimes true without it — the same page behaving two ways. By not
    looking, one button covers all four cases (gated or standalone, already in
    or not).
    """
    return templates.TemplateResponse(request, "landing.html", {})


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if auth_gateway_mode():
        return RedirectResponse("/app", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...),
                db: Session = Depends(get_db)):
    if auth_gateway_mode():
        return RedirectResponse("/app", status_code=302)
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user or not user.is_active or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "bad_credentials"}, status_code=401)
    ensure_workspace(db, user)
    db.commit()
    resp = RedirectResponse("/app", status_code=302)
    resp.set_cookie("session", create_token(user.id), httponly=True, samesite="lax",
                    secure=mailer.base_url().startswith("https"), max_age=7 * 24 * 3600)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("session")
    return resp


@app.get("/invite/{token}", response_class=HTMLResponse)
async def invite_landing(request: Request, token: str, db: Session = Depends(get_db)):
    inv = db.query(Invitation).filter(Invitation.token == token).first()
    if inv is None or inv.accepted_at is not None:
        raise HTTPException(status_code=404, detail="This invitation is not valid any more.")
    return templates.TemplateResponse(request, "invite.html",
                                      {"inv": inv, "gateway": auth_gateway_mode()})


@app.post("/invite/{token}")
async def invite_accept(request: Request, token: str, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    inv = db.query(Invitation).filter(Invitation.token == token).first()
    if inv is None or inv.accepted_at is not None:
        raise HTTPException(status_code=404, detail="This invitation is not valid any more.")
    existing = role_for(db, inv.workspace_id, user)
    if existing is None:
        db.add(Membership(workspace_id=inv.workspace_id, user_id=user.id,
                          role=inv.role, invited_by=inv.invited_by))
        log_event(db, inv.workspace_id, "member_added", actor=user, email=user.email)
    inv.accepted_at = utcnow()
    ensure_workspace(db, user)
    db.commit()
    return RedirectResponse(f"/app/{inv.workspace_id}", status_code=302)


# ── the app ───────────────────────────────────────────────────────────────────

@app.get("/app")
async def app_home(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = ensure_workspace(db, user)
    db.commit()
    return RedirectResponse(f"/app/{ws.id}", status_code=302)


def _visible_task(db: Session, acc: WorkspaceAccess, task_id: int,
                  deleted: bool = False) -> Task:
    t = (db.query(Task)
           .filter(Task.id == task_id, Task.workspace_id == acc.workspace.id).first())
    if t is None or (t.deleted_at is not None) != deleted:
        raise HTTPException(status_code=404, detail="Not found")
    return t


def _ctx(db: Session, acc: WorkspaceAccess, **extra) -> dict:
    """What every app page needs. `role` is resolved here and the templates only
    decide what to draw with it — permissions are never checked in a template."""
    ctx = {
        "user": acc.user, "ws": acc.workspace, "role": acc.role,
        "is_owner": acc.is_owner,
        "boards": workspaces_of(db, acc.user),
        "pending": (db.query(Task)
                      .filter(Task.workspace_id == acc.workspace.id,
                              Task.status == "proposed",
                              Task.deleted_at.is_(None)).count()),
    }
    ctx.update(extra)
    return ctx


@app.get("/app/{workspace_id}", response_class=HTMLResponse)
async def board(request: Request, view: str = "list", tag: str = "", person: str = "",
                archive: int = 0,
                acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                db: Session = Depends(get_db)):
    ws = acc.workspace
    if archive:
        rows = (db.query(Task)
                  .filter(Task.workspace_id == ws.id, Task.status == "done",
                          Task.deleted_at.is_(None))
                  .order_by(Task.done_at.desc()).all())
    else:
        rows = active_tasks(db, ws).all()

    if tag:
        rows = [t for t in rows if tag in [x.name for x in t.tag_list]]
    if person:
        try:
            pid = int(person)
            rows = [t for t in rows if t.for_person_id == pid]
        except ValueError:
            pass

    if view == "due":
        # A reading of the same table, never a rewrite of `position`. Tasks with
        # no date do not appear here, which is exactly why this is not the
        # default view: it would hide half the list.
        rows = sorted([t for t in rows if t.due_date], key=lambda t: t.due_date)

    return templates.TemplateResponse(request, "board.html", _ctx(
        db, acc, tasks=rows, view=view, tag=tag, person=person, archive=archive,
        people=people_of(db, ws), tags=tags_of(db, ws), self_id=self_person(db, ws).id))


@app.get("/app/{workspace_id}/task/{task_id}", response_class=HTMLResponse)
async def task_panel(request: Request, task_id: int,
                     acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                     db: Session = Depends(get_db)):
    t = _visible_task(db, acc, task_id)
    notes = (db.query(Note).filter(Note.task_id == t.id, Note.deleted_at.is_(None))
               .order_by(Note.created_at).all())
    events = (db.query(Event).filter(Event.task_id == t.id)
                .order_by(Event.created_at.desc()).limit(12).all())
    return templates.TemplateResponse(request, "_panel.html", _ctx(
        db, acc, task=t, notes=notes, events=events, people=people_of(db, acc.workspace),
        tags=tags_of(db, acc.workspace)))


def _read_task_form(db, ws, form: dict, task: Task, actor) -> None:
    task.title = (form.get("title") or "").strip()
    task.description = (form.get("description") or "").strip()
    task.recurring = form.get("recurring") in ("1", "on", "true")
    effort = (form.get("effort") or DEFAULT_EFFORT).upper()
    task.effort = effort if effort in EFFORTS else DEFAULT_EFFORT
    raw_due = (form.get("due_date") or "").strip()
    task.due_date = date.fromisoformat(raw_due) if raw_due else None
    task.link_url = (form.get("link_url") or "").strip()
    task.link_label = (form.get("link_label") or "").strip()

    # `for` is mandatory with a default: an optional field a report is built on
    # produces an "unspecified" slice that grows until the chart is useless.
    who = (form.get("for_person") or "").strip()
    if who:
        p = get_or_create_person(db, ws, who)
        if p is not None and p.id != task.for_person_id:
            task.for_person_id = p.id
    if task.for_person_id is None:
        task.for_person_id = self_person(db, ws).id


@app.post("/app/{workspace_id}/tasks")
async def create_task(request: Request,
                      acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                      db: Session = Depends(get_db)):
    """The owner creates, an editor proposes. Same form, different landing."""
    form = dict(await request.form())
    ws = acc.workspace
    t = Task(workspace_id=ws.id, title="", created_by=acc.user.id,
             status="active" if acc.is_owner else "proposed")
    _read_task_form(db, ws, form, t, acc.user)
    if not t.title:
        raise HTTPException(status_code=400, detail="A task needs a title.")
    if not acc.is_owner and not (form.get("for_person") or "").strip():
        # The one case where the right default is deducible: a task proposed by
        # an editor is for that editor until somebody says otherwise.
        t.for_person_id = person_for_proposal(db, ws, acc.user).id
    t.position = next_position(db, ws) if acc.is_owner else 0
    db.add(t)
    db.flush()
    set_tags(db, ws, t, form.get("tags", ""))
    log_event(db, ws.id, "created" if acc.is_owner else "proposed",
              actor=acc.user, task=t, title=t.title)

    if not acc.is_owner:
        owner = db.query(User).get(ws.owner_user_id)
        subject, body = mailer.proposal_received(acc.user.label, t.title, ws.id)
        mailer.notify(db, owner, "proposal_received", subject, body)
    db.commit()
    dest = f"/app/{ws.id}" if acc.is_owner else f"/app/{ws.id}/proposals"
    return RedirectResponse(dest, status_code=302)


@app.post("/app/{workspace_id}/tasks/{task_id}")
async def edit_task(request: Request, task_id: int,
                    acc: WorkspaceAccess = Depends(workspace_dep("owner")),
                    db: Session = Depends(get_db)):
    form = dict(await request.form())
    t = _visible_task(db, acc, task_id)
    before = t.for_person_id
    _read_task_form(db, acc.workspace, form, t, acc.user)
    set_tags(db, acc.workspace, t, form.get("tags", ""))
    log_event(db, acc.workspace.id, "edited", actor=acc.user, task=t)
    if before != t.for_person_id:
        log_event(db, acc.workspace.id, "for_changed", actor=acc.user, task=t,
                  before=before, after=t.for_person_id)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}", status_code=302)


@app.post("/app/{workspace_id}/tasks/{task_id}/done")
async def done(task_id: int, acc: WorkspaceAccess = Depends(workspace_dep("editor")),
               db: Session = Depends(get_db)):
    """Both roles, on purpose: "it has been done" is a fact, not a decision about
    the list. The event records who."""
    t = _visible_task(db, acc, task_id)
    if t.status != "active":
        raise HTTPException(status_code=400, detail="Only a task on the list can be done.")
    mark_done(db, acc.workspace, t, actor=acc.user)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}", status_code=302)


@app.post("/app/{workspace_id}/tasks/{task_id}/reopen")
async def reopen_task(task_id: int, acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                      db: Session = Depends(get_db)):
    t = _visible_task(db, acc, task_id)
    reopen(db, acc.workspace, t, actor=acc.user)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}", status_code=302)


@app.post("/app/{workspace_id}/tasks/{task_id}/delete")
async def delete_task(task_id: int, acc: WorkspaceAccess = Depends(workspace_dep("owner")),
                      db: Session = Depends(get_db)):
    """Soft, always. A deleted task takes its notes with it — a conversation
    between two people — and deleting the row deletes somebody else's history."""
    t = _visible_task(db, acc, task_id)
    t.deleted_at = utcnow()
    log_event(db, acc.workspace.id, "deleted", actor=acc.user, task=t, title=t.title)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}", status_code=302)


@app.post("/app/{workspace_id}/tasks/{task_id}/restore")
async def restore_task(task_id: int, acc: WorkspaceAccess = Depends(workspace_dep("owner")),
                       db: Session = Depends(get_db)):
    t = _visible_task(db, acc, task_id, deleted=True)
    t.deleted_at = None
    if t.status == "active":
        t.position = next_position(db, acc.workspace)
    log_event(db, acc.workspace.id, "restored", actor=acc.user, task=t)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}?archive=1", status_code=302)


@app.post("/app/{workspace_id}/order")
async def reorder(request: Request,
                  acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                  db: Session = Depends(get_db)):
    payload = await request.json()
    ids = [int(x) for x in payload.get("ids", [])]
    outcome = apply_order(db, acc.workspace, ids, int(payload.get("version", -1)),
                          actor=acc.user)
    if outcome != "ok":
        db.rollback()
        return JSONResponse({"error": outcome}, status_code=409)
    db.commit()
    return {"ok": True, "version": acc.workspace.order_version}


# ── proposals ─────────────────────────────────────────────────────────────────

@app.get("/app/{workspace_id}/proposals", response_class=HTMLResponse)
async def proposals(request: Request,
                    acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                    db: Session = Depends(get_db)):
    ws = acc.workspace
    q = db.query(Task).filter(Task.workspace_id == ws.id, Task.deleted_at.is_(None))
    return templates.TemplateResponse(request, "proposals.html", _ctx(
        db, acc,
        open_=q.filter(Task.status == "proposed").order_by(Task.created_at).all(),
        declined=q.filter(Task.status == "rejected").order_by(Task.decided_at.desc()).all(),
        people=people_of(db, ws), tags=tags_of(db, ws)))


@app.post("/app/{workspace_id}/tasks/{task_id}/accept")
async def accept(task_id: int, acc: WorkspaceAccess = Depends(workspace_dep("owner")),
                 db: Session = Depends(get_db)):
    t = _visible_task(db, acc, task_id)
    if t.status != "proposed":
        raise HTTPException(status_code=400, detail="That is not an open proposal.")
    t.status = "active"
    t.position = next_position(db, acc.workspace)
    t.decided_by, t.decided_at = acc.user.id, utcnow()
    log_event(db, acc.workspace.id, "accepted", actor=acc.user, task=t)
    proposer = db.query(User).get(t.created_by) if t.created_by else None
    if proposer is not None and proposer.id != acc.user.id:
        subject, body = mailer.proposal_accepted(acc.user.label, t.title, acc.workspace.id)
        mailer.notify(db, proposer, "proposal_decided", subject, body)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}/proposals", status_code=302)


@app.post("/app/{workspace_id}/tasks/{task_id}/reject")
async def reject(task_id: int, reason: str = Form(""),
                 acc: WorkspaceAccess = Depends(workspace_dep("owner")),
                 db: Session = Depends(get_db)):
    """The reason is required: a refusal with no reason comes back identical in
    two months (SPEC.md §5.1)."""
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Declining needs a reason.")
    t = _visible_task(db, acc, task_id)
    if t.status != "proposed":
        raise HTTPException(status_code=400, detail="That is not an open proposal.")
    t.status = "rejected"
    t.decision_reason = reason
    t.decided_by, t.decided_at = acc.user.id, utcnow()
    log_event(db, acc.workspace.id, "rejected", actor=acc.user, task=t, reason=reason)
    proposer = db.query(User).get(t.created_by) if t.created_by else None
    if proposer is not None and proposer.id != acc.user.id:
        subject, body = mailer.proposal_declined(acc.user.label, t.title, reason,
                                                 acc.workspace.id)
        mailer.notify(db, proposer, "proposal_decided", subject, body)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}/proposals", status_code=302)


# ── notes ─────────────────────────────────────────────────────────────────────

@app.post("/app/{workspace_id}/tasks/{task_id}/notes")
async def add_note(task_id: int, body: str = Form(...),
                   acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                   db: Session = Depends(get_db)):
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="An empty note is not a note.")
    t = _visible_task(db, acc, task_id)
    db.add(Note(task_id=t.id, author_user_id=acc.user.id, body=body))
    log_event(db, acc.workspace.id, "note_added", actor=acc.user, task=t)
    for m in db.query(Membership).filter(Membership.workspace_id == acc.workspace.id).all():
        if m.user_id != acc.user.id:
            subject = f"Note on: {t.title}"
            text = (f"{acc.user.label} wrote on \"{t.title}\":\n\n{body}\n\n"
                    f"  {mailer.base_url()}/app/{acc.workspace.id}\n")
            mailer.notify(db, m.user, "note_added", subject, text)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}#task-{t.id}", status_code=302)


@app.post("/app/{workspace_id}/notes/{note_id}/delete")
async def delete_note(note_id: int, acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                      db: Session = Depends(get_db)):
    n = (db.query(Note).join(Task, Task.id == Note.task_id)
           .filter(Note.id == note_id, Task.workspace_id == acc.workspace.id).first())
    if n is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not n.deletable_by(acc.user):
        # Not "forbidden": the window is the rule, and after it the correction is
        # another note.
        raise HTTPException(status_code=400,
                            detail="Notes can only be removed by their author, within 15 minutes.")
    n.deleted_at = utcnow()
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}", status_code=302)


# ── report ────────────────────────────────────────────────────────────────────

def _period(request: Request) -> tuple[datetime, datetime, str]:
    """From ?from= / ?to=, defaulting to the current quarter."""
    today = date.today()
    q_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    raw_from = request.query_params.get("from") or q_start.isoformat()
    raw_to = request.query_params.get("to") or today.isoformat()
    try:
        start = datetime.fromisoformat(raw_from)
        end = datetime.fromisoformat(raw_to) + timedelta(days=1)
    except ValueError:
        start, end = datetime.combine(q_start, datetime.min.time()), datetime.now()
    return start, end, f"{raw_from} → {raw_to}"


@app.get("/app/{workspace_id}/report", response_class=HTMLResponse)
async def report_page(request: Request,
                      acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                      db: Session = Depends(get_db)):
    start, end, label = _period(request)
    return templates.TemplateResponse(request, "report.html", _ctx(
        db, acc, data=report(db, acc.workspace, start, end), label=label,
        raw_from=request.query_params.get("from", start.date().isoformat()),
        raw_to=request.query_params.get("to", (end - timedelta(days=1)).date().isoformat())))


@app.get("/app/{workspace_id}/report.csv")
async def report_csv(request: Request,
                     acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                     db: Session = Depends(get_db)):
    start, end, label = _period(request)
    data = report(db, acc.workspace, start, end)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["period", label])
    w.writerow(["weights", " ".join(f"{k}={v}" for k, v in EFFORT_WEIGHTS.items())])
    w.writerow([])
    for section in ("people", "tags"):
        w.writerow([section])
        w.writerow(["label", "on the list", "opened", "closed", "touches",
                    "weighted", "share %"])
        for r in data[section]:
            w.writerow([r["label"], r["listed"], r["opened"], r["closed"],
                        r["touches"], r["weighted"], r["share"]])
        w.writerow([])
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="thelist-{start:%Y%m%d}-{end:%Y%m%d}.csv"'})


# ── settings ──────────────────────────────────────────────────────────────────

@app.get("/app/{workspace_id}/settings", response_class=HTMLResponse)
async def settings_page(request: Request,
                        acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                        db: Session = Depends(get_db)):
    ws = acc.workspace
    members = db.query(Membership).filter(Membership.workspace_id == ws.id).all()
    prefs = {p.event_type: p.enabled for p in
             db.query(NotificationPref).filter(NotificationPref.user_id == acc.user.id).all()}
    keys = (db.query(ApiKey).filter(ApiKey.user_id == acc.user.id,
                                    ApiKey.revoked_at.is_(None)).all())
    return templates.TemplateResponse(request, "settings.html", _ctx(
        db, acc, members=members, people=people_of(db, ws, include_archived=True),
        prefs=prefs, keys=keys,
        invites=db.query(Invitation).filter(Invitation.workspace_id == ws.id,
                                            Invitation.accepted_at.is_(None)).all(),
        smtp_on=os.environ.get("SMTP_ENABLED", "").lower() in ("1", "true", "yes")))


@app.post("/app/{workspace_id}/members")
async def invite_member(email: str = Form(...), role: str = Form("editor"),
                        acc: WorkspaceAccess = Depends(workspace_dep("owner")),
                        db: Session = Depends(get_db)):
    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="An address is needed.")
    inv = Invitation(workspace_id=acc.workspace.id, email=email,
                     role="editor" if role != "owner" else "editor",
                     token=new_invite_token(), invited_by=acc.user.id)
    db.add(inv)
    db.flush()
    subject, body = mailer.invitation(acc.user.label, acc.workspace.name, inv.token)
    ok, code = mailer.send(email, subject, body)
    db.commit()
    # When the relay is off the link is shown on screen: a dead mailbox should
    # cost a copy-paste, not a person who cannot be invited.
    return RedirectResponse(
        f"/app/{acc.workspace.id}/settings?invited={inv.token}&mail={code}",
        status_code=302)


@app.post("/app/{workspace_id}/members/{user_id}/remove")
async def remove_member(user_id: int,
                        acc: WorkspaceAccess = Depends(workspace_dep("owner")),
                        db: Session = Depends(get_db)):
    if user_id == acc.workspace.owner_user_id:
        raise HTTPException(status_code=400, detail="The owner cannot be removed.")
    m = (db.query(Membership).filter(Membership.workspace_id == acc.workspace.id,
                                     Membership.user_id == user_id).first())
    if m is None:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(m)
    log_event(db, acc.workspace.id, "member_removed", actor=acc.user, user_id=user_id)
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}/settings", status_code=302)


@app.post("/app/{workspace_id}/people/{person_id}")
async def edit_person(person_id: int, display_name: str = Form(""),
                      archived: str = Form(""),
                      acc: WorkspaceAccess = Depends(workspace_dep("owner")),
                      db: Session = Depends(get_db)):
    p = (db.query(Person).filter(Person.id == person_id,
                                 Person.workspace_id == acc.workspace.id).first())
    if p is None:
        raise HTTPException(status_code=404, detail="Not found")
    new_name = display_name.strip()
    if new_name and normalise(new_name) != p.norm_name:
        clash = (db.query(Person)
                   .filter(Person.workspace_id == acc.workspace.id,
                           Person.norm_name == normalise(new_name),
                           Person.id != p.id).first())
        if clash is not None:
            raise HTTPException(status_code=400, detail="Somebody already has that name here.")
        # Renaming rewrites how the past reads. Fixing "markus" into "Markus
        # Christen" is right; turning "Nikola" into "BAG" would silently
        # reattribute everything. Not forbidden — that would be
        # disproportionate — but written down.
        log_event(db, acc.workspace.id, "person_renamed", actor=acc.user,
                  before=p.display_name, after=new_name)
        p.display_name, p.norm_name = new_name, normalise(new_name)
    if not p.is_owner_self:
        p.archived_at = utcnow() if archived in ("1", "on", "true") else None
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}/settings", status_code=302)


@app.post("/app/{workspace_id}/prefs")
async def save_prefs(request: Request,
                     acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                     db: Session = Depends(get_db)):
    form = dict(await request.form())
    for event_type in NOTIFICATIONS:
        enabled = form.get(event_type) in ("1", "on", "true")
        pref = (db.query(NotificationPref)
                  .filter(NotificationPref.user_id == acc.user.id,
                          NotificationPref.event_type == event_type).first())
        if pref is None:
            db.add(NotificationPref(user_id=acc.user.id, event_type=event_type,
                                    enabled=enabled))
        else:
            pref.enabled = enabled
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}/settings", status_code=302)


@app.post("/app/{workspace_id}/keys")
async def create_key(label: str = Form(""),
                     acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                     db: Session = Depends(get_db)):
    key = new_api_key()
    db.add(ApiKey(user_id=acc.user.id, key=key, label=label.strip()[:60]))
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}/settings?key={key}", status_code=302)


@app.post("/app/{workspace_id}/keys/{key_id}/revoke")
async def revoke_key(key_id: int, acc: WorkspaceAccess = Depends(workspace_dep("editor")),
                     db: Session = Depends(get_db)):
    row = db.query(ApiKey).filter(ApiKey.id == key_id,
                                  ApiKey.user_id == acc.user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    row.revoked_at = utcnow()
    db.commit()
    return RedirectResponse(f"/app/{acc.workspace.id}/settings", status_code=302)
