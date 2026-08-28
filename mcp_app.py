"""
The model-facing surface of TheList.

The questions this exists for are the ones that arrive in conversation rather
than in front of a page: what is due, what is waiting for me to decide, how much
of this quarter went to somebody else's work — and, since the surface writes too,
"put that on my list" said while talking about the thing itself.

Access. Every call runs as the human who owns the API key, and every board
lookup goes through the same access function the web app uses. The surface has
exactly the reach of its owner, no more. **The distinction that governs the
refusals**: a board the caller is not a member of answers "not found", because
otherwise the model could enumerate what it cannot read; but a board they CAN
see, with an action reserved to the owner, says so plainly — nothing is being
hidden there, and a model that knows the rule can explain it instead of
retrying.

The write tools enforce exactly the rules the web enforces, and they do it by
calling the same functions rather than by repeating them: an editor proposes
where the owner creates, declining needs a reason, deletion is soft, completing a
recurring task keeps it on the list. There is no path through here that the
interface would not allow.

Errors are returned as {"error": ...} rather than raised: a tool that throws
gives the model a stack trace to hallucinate around, while a message it can read
lets it correct course.
"""
from datetime import date, datetime, timedelta

from mcp.server.mcpserver import MCPServer

import auth
import mailer
from models import (
    DEFAULT_EFFORT, EFFORTS, EFFORT_WEIGHTS, STATUS_LABELS, Link, Membership,
    Note, SessionLocal, Task, User, Workspace, active_tasks, add_link,
    get_or_create_person, has_role, log_event, mark_done, next_position,
    people_of, person_for_proposal, reopen, report, role_for, self_person,
    set_tags, tags_of, utcnow, workspaces_of,
)

mcp = MCPServer(
    name="thelist",
    instructions=(
        "A list of macro-tasks, one board per person: what is on it, what is due, "
        "what is waiting for a decision, and who the work is for. Start with "
        "list_boards. Reads are free; **before writing anything — adding a task, "
        "noting, completing, accepting, declining, deleting — confirm with the user**, "
        "because this list is how two people coordinate and a surprise entry costs "
        "somebody a conversation. "
        "Two things to carry into every answer: `workload` counts tasks and "
        "completions, NOT time, so report those numbers as frequency and volume and "
        "never as hours; and the order of a board is the priority its owner declared "
        "by hand, so do not re-sort it before showing it."
    ),
)


def _fail(msg: str) -> dict:
    return {"error": msg}


def _caller(db):
    uid = auth.caller_id()
    return db.query(User).get(uid) if uid else None


def _board(db, user, ref: str):
    """Resolve a board by id or name, for this caller only.

    Returns None when the caller cannot see it — the caller cannot tell apart
    "does not exist" from "not yours", which is the point.
    """
    boards = workspaces_of(db, user)
    ref = (ref or "").strip()
    if not ref:
        return boards[0] if len(boards) == 1 else None
    for ws in boards:
        if str(ws.id) == ref or ws.name.lower() == ref.lower():
            return ws
    return None


def _no_board(db, user, ref: str) -> dict:
    """Why there is no board here — and there are two different reasons for it.

    Naming a board that is not yours has to look exactly like naming one that
    does not exist, or the surface becomes a way to probe for what it hides. But
    saying the same thing to somebody who can see several boards and named none
    is not discretion, it is unhelpfulness: **an editor always has two**, their
    own and the one they were invited to, so the empty default is ambiguous
    rather than missing. Listing what they can already see gives nothing away.
    """
    boards = workspaces_of(db, user)
    if not (ref or "").strip() and len(boards) > 1:
        return _fail("you can reach more than one board, so say which: "
                     + ", ".join("%s (id %d)" % (ws.name, ws.id) for ws in boards))
    return _fail("board not found")


def _open(db, ref: str, minimum: str = "editor"):
    """(workspace, user, None) on success, (None, None, error) on refusal.

    The third slot is an error and nothing else. It held the resolved role for
    about ten minutes, which meant every caller's `if err: return err` fired on
    success and answered "owner" to whoever asked — invisible to the read tools,
    which never call this, and caught by the first write test.

    The two refusals are not the same refusal, and the difference is deliberate.
    A board that is not yours does not exist — anything else lets the model probe
    for what it cannot see. A board that IS yours, with an action reserved to the
    owner, says exactly that: nothing is concealed by the answer, and a model
    that is told the rule can pass it on instead of retrying.
    """
    user = _caller(db)
    if user is None:
        return None, None, _fail("unknown caller")
    ws = _board(db, user, ref)
    if ws is None:
        return None, None, _no_board(db, user, ref)
    role = role_for(db, ws.id, user)
    if not has_role(role, minimum):
        return None, None, _fail("only the owner of this board can do that")
    return ws, user, None


def _task(db, ws, task_id: int, deleted: bool = False):
    t = (db.query(Task)
           .filter(Task.id == task_id, Task.workspace_id == ws.id).first())
    if t is None or (t.deleted_at is not None) != deleted:
        return None
    return t


def _brief(db, t: Task) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status,
        "for": t.person.display_name if t.person else None,
        "tags": [tag.display_name for tag in t.tag_list],
        "effort": t.effort,
        "recurring": bool(t.recurring),
        "due": t.due_date.isoformat() if t.due_date else None,
        "overdue": bool(t.due_date and t.due_date < date.today()),
        "last_done": t.last_done_at.date().isoformat() if t.last_done_at else None,
        "notes": db.query(Note).filter(Note.task_id == t.id,
                                       Note.deleted_at.is_(None)).count(),
        "links": [{"id": l.id, "url": l.url, "label": l.label or None}
                  for l in t.links],
    }


@mcp.tool()
def list_boards() -> dict:
    """The boards this key can reach, and the role on each."""
    db = SessionLocal()
    try:
        user = _caller(db)
        if user is None:
            return _fail("unknown caller")
        return {"boards": [
            {"id": ws.id, "name": ws.name,
             "role": role_for(db, ws.id, user),
             "owner": ws.owner.label if ws.owner else None,
             "on_the_list": active_tasks(db, ws).count()}
            for ws in workspaces_of(db, user)]}
    finally:
        db.close()


@mcp.tool()
def list_tasks(board: str = "", status: str = "active", tag: str = "",
               for_person: str = "", include_archived: bool = False) -> dict:
    """What is on a board, in the order the owner put it.

    `status` is one of active, proposed, done, rejected. The order of the result
    IS the priority the owner declared by dragging: do not re-sort it before
    reporting it.
    """
    db = SessionLocal()
    try:
        user = _caller(db)
        if user is None:
            return _fail("unknown caller")
        ws = _board(db, user, board)
        if ws is None:
            return _no_board(db, user, board)
        if status not in STATUS_LABELS:
            return _fail(f"status must be one of {', '.join(STATUS_LABELS)}")

        q = db.query(Task).filter(Task.workspace_id == ws.id, Task.status == status)
        if not include_archived:
            q = q.filter(Task.deleted_at.is_(None))
        rows = q.order_by(Task.position, Task.id).all()
        if tag:
            tl = tag.strip().lower()
            rows = [t for t in rows if tl in [x.name for x in t.tag_list]]
        if for_person:
            fp = for_person.strip().lower()
            rows = [t for t in rows
                    if t.person and t.person.norm_name == fp]
        return {"board": ws.name, "status": status,
                "tasks": [_brief(db, t) for t in rows]}
    finally:
        db.close()


@mcp.tool()
def get_task(task_id: int, board: str = "") -> dict:
    """One task with its notes and its recent history."""
    db = SessionLocal()
    try:
        user = _caller(db)
        if user is None:
            return _fail("unknown caller")
        ws = _board(db, user, board)
        if ws is None:
            return _no_board(db, user, board)
        t = db.query(Task).filter(Task.id == task_id,
                                  Task.workspace_id == ws.id).first()
        if t is None:
            return _fail("task not found")
        notes = (db.query(Note).filter(Note.task_id == t.id, Note.deleted_at.is_(None))
                   .order_by(Note.created_at).all())
        out = _brief(db, t)
        out["description"] = t.description
        out["decision_reason"] = t.decision_reason or None
        out["notes"] = [{"author": n.author.label if n.author else None,
                         "at": n.created_at.isoformat(timespec="minutes"),
                         "body": n.body} for n in notes]
        return out
    finally:
        db.close()


@mcp.tool()
def upcoming(days: int = 14, board: str = "") -> dict:
    """What falls due within N days, overdue things first."""
    db = SessionLocal()
    try:
        user = _caller(db)
        if user is None:
            return _fail("unknown caller")
        ws = _board(db, user, board)
        if ws is None:
            return _no_board(db, user, board)
        horizon = date.today() + timedelta(days=max(0, days))
        rows = [t for t in active_tasks(db, ws).all()
                if t.due_date and t.due_date <= horizon]
        rows.sort(key=lambda t: t.due_date)
        return {"board": ws.name, "horizon": horizon.isoformat(),
                "tasks": [_brief(db, t) for t in rows]}
    finally:
        db.close()


@mcp.tool()
def list_proposals(board: str = "") -> dict:
    """What is waiting for the owner to accept or decline, and what was declined."""
    db = SessionLocal()
    try:
        user = _caller(db)
        if user is None:
            return _fail("unknown caller")
        ws = _board(db, user, board)
        if ws is None:
            return _no_board(db, user, board)
        q = db.query(Task).filter(Task.workspace_id == ws.id, Task.deleted_at.is_(None))
        pending = q.filter(Task.status == "proposed").order_by(Task.created_at).all()
        declined = (q.filter(Task.status == "rejected")
                     .order_by(Task.decided_at.desc()).limit(20).all())
        return {
            "board": ws.name,
            "waiting": [{**_brief(db, t),
                         "proposed_by": t.creator.label if t.creator else None,
                         "waiting_days": (utcnow() - t.created_at).days}
                        for t in pending],
            "declined": [{**_brief(db, t), "reason": t.decision_reason}
                         for t in declined],
        }
    finally:
        db.close()


@mcp.tool()
def workload(date_from: str = "", date_to: str = "", board: str = "",
             group_by: str = "person") -> dict:
    """How the period was spent, by person or by tag.

    Dates are ISO (YYYY-MM-DD); the default period is the current quarter to
    today. `group_by` is "person" or "tag".

    **These are counts, not hours.** `touches` counts completions in the period,
    recurring tasks included — a recurring task done eleven times produces eleven
    of them, which is why it beats counting tasks. `weighted` multiplies each by
    its effort (S/M/L = 1/3/8), which separates the chapter from the website
    update but is still not time. Report both, and say which is which.
    """
    db = SessionLocal()
    try:
        user = _caller(db)
        if user is None:
            return _fail("unknown caller")
        ws = _board(db, user, board)
        if ws is None:
            return _no_board(db, user, board)
        if group_by not in ("person", "tag"):
            return _fail('group_by must be "person" or "tag"')

        today = date.today()
        q_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        try:
            start = datetime.fromisoformat(date_from) if date_from else \
                datetime.combine(q_start, datetime.min.time())
            end = (datetime.fromisoformat(date_to) + timedelta(days=1)) if date_to else \
                datetime.combine(today + timedelta(days=1), datetime.min.time())
        except ValueError:
            return _fail("dates must be ISO, YYYY-MM-DD")

        data = report(db, ws, start, end)
        return {
            "board": ws.name,
            "period": {"from": start.date().isoformat(),
                       "to": (end - timedelta(days=1)).date().isoformat()},
            "weights": EFFORT_WEIGHTS,
            "caveat": ("counts and weighted counts, never hours — an S and an L "
                       "differ by declared size, not by measured time"),
            "rows": data["people" if group_by == "person" else "tags"],
        }
    finally:
        db.close()


@mcp.tool()
def vocabularies(board: str = "") -> dict:
    """The tags and the people this board already knows."""
    db = SessionLocal()
    try:
        user = _caller(db)
        if user is None:
            return _fail("unknown caller")
        ws = _board(db, user, board)
        if ws is None:
            return _no_board(db, user, board)
        return {
            "board": ws.name,
            "tags": [t.display_name for t in tags_of(db, ws)],
            "people": [{"name": p.display_name, "is_me": bool(p.is_owner_self)}
                       for p in people_of(db, ws)],
            "efforts": EFFORT_WEIGHTS,
        }
    finally:
        db.close()


# ── writing ───────────────────────────────────────────────────────────────────
#
# Everything below goes through the same helpers the web routes use — mark_done,
# reopen, set_tags, get_or_create_person, log_event — rather than reimplementing
# the rules beside them. Two implementations of "what does completing a recurring
# task mean" is one too many, and the second one is always the one that drifts.
#
# Every write also carries via="mcp" into the event log, so the history can tell
# a change made in conversation from one made on the page.


@mcp.tool()
def add_task(title: str, board: str = "", for_person: str = "", tags: str = "",
             effort: str = "M", due: str = "", recurring: bool = False,
             description: str = "", link_url: str = "", link_label: str = "",
             allow_duplicate: bool = False) -> dict:
    """Put something on a board — or propose it, depending on who you are.

    The owner's tasks land on the list; an editor's land as a proposal for the
    owner to accept or decline. Same rule as the interface, and the answer says
    which of the two happened.

    `for_person` is who the task is FOR — anyone, with or without an account; an
    unknown name creates that person on this board. Left empty it defaults to the
    board's owner, under their own name, or to the proposer when an editor
    proposes.

    `effort` is S, M or L (declared size, never hours). `due` is ISO YYYY-MM-DD.
    `recurring` means it comes back every so often: completing it will keep it on
    the list and record when it was last done, instead of archiving it.

    A title that already exists on the board is refused unless `allow_duplicate`
    is set — a retried call should not quietly leave two identical rows.
    """
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "editor")
        if err:
            return err
        title = (title or "").strip()
        if not title:
            return _fail("a task needs a title")
        effort = (effort or DEFAULT_EFFORT).upper()
        if effort not in EFFORTS:
            return _fail("effort must be one of " + ", ".join(EFFORTS))
        due_date = None
        if due:
            try:
                due_date = date.fromisoformat(due)
            except ValueError:
                return _fail("due must be ISO, YYYY-MM-DD")

        if not allow_duplicate:
            clash = (db.query(Task)
                       .filter(Task.workspace_id == ws.id,
                               Task.deleted_at.is_(None),
                               Task.status.in_(("active", "proposed")),
                               Task.title == title).first())
            if clash is not None:
                return _fail(
                    "task %d on this board already has that exact title (%s). Pass "
                    "allow_duplicate to add a second one anyway."
                    % (clash.id, clash.status))

        is_owner = role_for(db, ws.id, user) == "owner"
        t = Task(workspace_id=ws.id, title=title,
                 description=(description or "").strip(),
                 status="active" if is_owner else "proposed",
                 recurring=bool(recurring), effort=effort, due_date=due_date,
                 link_url=(link_url or "").strip(),
                 link_label=(link_label or "").strip(),
                 created_by=user.id,
                 position=next_position(db, ws) if is_owner else 0)
        if for_person.strip():
            p = get_or_create_person(db, ws, for_person)
            t.for_person_id = p.id if p else None
        if t.for_person_id is None:
            t.for_person_id = (self_person(db, ws).id if is_owner
                               else person_for_proposal(db, ws, user).id)
        db.add(t)
        db.flush()
        if link_url.strip():
            add_link(db, t, link_url, link_label)
        set_tags(db, ws, t, tags)
        log_event(db, ws.id, "created" if is_owner else "proposed",
                  actor=user, task=t, title=t.title, via="mcp")

        if not is_owner:
            owner = db.query(User).get(ws.owner_user_id)
            subject, body = mailer.proposal_received(user.label, t.title, ws.id)
            mailer.notify(db, owner, "proposal_received", subject, body)
        db.commit()
        return {"landed": "on the list" if is_owner else "in the owner's proposals",
                "task": _brief(db, t)}
    finally:
        db.close()


@mcp.tool()
def update_task(task_id: int, board: str = "", title: str = "",
                for_person: str = "", tags: str = "", effort: str = "",
                due: str = "", description: str = "",
                recurring: str = "", clear: str = "") -> dict:
    """Change a task. Owner only.

    Empty means "leave it alone", so a call only touches what it names. To empty
    a field, list it in `clear` instead — a comma-separated set drawn from
    due, description, tags, for. **Links are not here**: a task can have several,
    so they have their own tools.

    `recurring` takes "yes" or "no". `tags` REPLACES the whole set rather than
    adding to it: read the task first if you mean to append.
    """
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "owner")
        if err:
            return err
        t = _task(db, ws, task_id)
        if t is None:
            return _fail("task not found")

        wipe = {c.strip().lower() for c in (clear or "").replace(";", ",").split(",")}
        before_person = t.for_person_id

        if title.strip():
            t.title = title.strip()
        if description.strip():
            t.description = description.strip()
        elif "description" in wipe:
            t.description = ""
        if effort.strip():
            e = effort.strip().upper()
            if e not in EFFORTS:
                return _fail("effort must be one of " + ", ".join(EFFORTS))
            t.effort = e
        if recurring.strip():
            r = recurring.strip().lower()
            if r not in ("yes", "no", "true", "false"):
                return _fail('recurring takes "yes" or "no"')
            t.recurring = r in ("yes", "true")
        if due.strip():
            try:
                t.due_date = date.fromisoformat(due.strip())
            except ValueError:
                return _fail("due must be ISO, YYYY-MM-DD")
        elif "due" in wipe:
            t.due_date = None
        if for_person.strip():
            p = get_or_create_person(db, ws, for_person)
            if p is not None:
                t.for_person_id = p.id
        elif "for" in wipe:
            t.for_person_id = self_person(db, ws).id
        if tags.strip():
            set_tags(db, ws, t, tags)
        elif "tags" in wipe:
            set_tags(db, ws, t, "")

        log_event(db, ws.id, "edited", actor=user, task=t, via="mcp")
        if before_person != t.for_person_id:
            log_event(db, ws.id, "for_changed", actor=user, task=t,
                      before=before_person, after=t.for_person_id, via="mcp")
        db.commit()
        return {"updated": True, "task": _brief(db, t)}
    finally:
        db.close()


@mcp.tool()
def complete_task(task_id: int, board: str = "") -> dict:
    """Record that a task has been done. Either role may.

    On a one-off task this archives it. On a recurring one it stays exactly where
    it is, records the date and drops its due date — so the answer says which of
    the two happened instead of leaving you to assume.
    """
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "editor")
        if err:
            return err
        t = _task(db, ws, task_id)
        if t is None:
            return _fail("task not found")
        if t.status != "active":
            return _fail("that task is %s, and only something on the list can be "
                         "completed" % t.status)
        was_recurring = t.recurring
        mark_done(db, ws, t, actor=user)
        db.commit()
        return {"outcome": ("recorded, and it stays on the list" if was_recurring
                            else "archived"),
                "task": _brief(db, t)}
    finally:
        db.close()


@mcp.tool()
def reopen_task(task_id: int, board: str = "") -> dict:
    """Put an archived task back on the list, at the bottom. Either role may."""
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "editor")
        if err:
            return err
        t = _task(db, ws, task_id)
        if t is None:
            return _fail("task not found")
        if t.status == "active":
            return _fail("that task is already on the list")
        reopen(db, ws, t, actor=user)
        db.commit()
        return {"reopened": True, "task": _brief(db, t)}
    finally:
        db.close()


@mcp.tool()
def add_note(task_id: int, body: str, board: str = "") -> dict:
    """Write a note on a task. Either role may.

    Notes cannot be edited afterwards — they are how two people talk to each
    other here, and an editable one is a conversation nobody can rely on. Write
    something you are willing to leave standing.
    """
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "editor")
        if err:
            return err
        body = (body or "").strip()
        if not body:
            return _fail("an empty note is not a note")
        t = _task(db, ws, task_id)
        if t is None:
            return _fail("task not found")
        db.add(Note(task_id=t.id, author_user_id=user.id, body=body))
        log_event(db, ws.id, "note_added", actor=user, task=t, via="mcp")
        for m in db.query(Membership).filter(Membership.workspace_id == ws.id).all():
            if m.user_id != user.id:
                mailer.notify(db, m.user, "note_added", "Note on: " + t.title,
                              '%s wrote on "%s":\n\n%s\n\n  %s/app/%d\n'
                              % (user.label, t.title, body, mailer.base_url(), ws.id))
        db.commit()
        return {"added": True, "task_id": t.id, "author": user.label}
    finally:
        db.close()


@mcp.tool()
def accept_proposal(task_id: int, board: str = "") -> dict:
    """Accept a proposal onto the list, at the bottom. Owner only."""
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "owner")
        if err:
            return err
        t = _task(db, ws, task_id)
        if t is None:
            return _fail("task not found")
        if t.status != "proposed":
            return _fail("that is not an open proposal")
        t.status = "active"
        t.position = next_position(db, ws)
        t.decided_by, t.decided_at = user.id, utcnow()
        log_event(db, ws.id, "accepted", actor=user, task=t, via="mcp")
        proposer = db.query(User).get(t.created_by) if t.created_by else None
        if proposer is not None and proposer.id != user.id:
            subject, body = mailer.proposal_accepted(user.label, t.title, ws.id)
            mailer.notify(db, proposer, "proposal_decided", subject, body)
        db.commit()
        return {"accepted": True, "task": _brief(db, t)}
    finally:
        db.close()


@mcp.tool()
def decline_proposal(task_id: int, reason: str, board: str = "") -> dict:
    """Decline a proposal. Owner only, and the reason is required.

    Declined proposals are kept, with the reason: in a two-person arrangement
    "you proposed this in May and I said no because…" is working memory, and a
    refusal with no reason comes back identical in two months. Do not invent the
    reason — ask the person for it.
    """
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "owner")
        if err:
            return err
        reason = (reason or "").strip()
        if not reason:
            return _fail("declining needs a reason, and it is kept with the proposal")
        t = _task(db, ws, task_id)
        if t is None:
            return _fail("task not found")
        if t.status != "proposed":
            return _fail("that is not an open proposal")
        t.status = "rejected"
        t.decision_reason = reason
        t.decided_by, t.decided_at = user.id, utcnow()
        log_event(db, ws.id, "rejected", actor=user, task=t, reason=reason, via="mcp")
        proposer = db.query(User).get(t.created_by) if t.created_by else None
        if proposer is not None and proposer.id != user.id:
            subject, body = mailer.proposal_declined(user.label, t.title, reason, ws.id)
            mailer.notify(db, proposer, "proposal_decided", subject, body)
        db.commit()
        return {"declined": True, "reason": reason, "task": _brief(db, t)}
    finally:
        db.close()


@mcp.tool()
def move_task(task_id: int, board: str = "", after_task_id: int = 0,
              to_top: bool = False) -> dict:
    """Move a task within the list. Either role may.

    `to_top` puts it first; `after_task_id` puts it straight after that task;
    neither puts it last.

    The order of this list is a priority its owner declared by hand, so moving
    something rewrites somebody else's stated priorities: confirm first. The move
    is recorded either way.
    """
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "editor")
        if err:
            return err
        rows = active_tasks(db, ws).all()
        ids = [t.id for t in rows]
        if task_id not in ids:
            return _fail("task not found on the list")
        if after_task_id and after_task_id not in ids:
            return _fail("after_task_id is not on this list")
        if after_task_id == task_id:
            return _fail("a task cannot go after itself")

        ids.remove(task_id)
        if to_top:
            ids.insert(0, task_id)
        elif after_task_id:
            ids.insert(ids.index(after_task_id) + 1, task_id)
        else:
            ids.append(task_id)

        by_id = {t.id: t for t in rows}
        for i, tid in enumerate(ids, start=1):
            by_id[tid].position = i
        ws.order_version += 1
        log_event(db, ws.id, "reordered", actor=user, count=len(ids), via="mcp")
        db.commit()
        return {"moved": True,
                "order": [{"id": tid, "title": by_id[tid].title} for tid in ids]}
    finally:
        db.close()


@mcp.tool()
def delete_task(task_id: int, board: str = "") -> dict:
    """Take a task off the board. Owner only.

    Deletion is soft, always: a task carries its notes, which are a conversation
    between two people, and removing the row would remove somebody else's
    history. It goes to the archive, and `restore_task` brings it back.
    """
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "owner")
        if err:
            return err
        t = _task(db, ws, task_id)
        if t is None:
            return _fail("task not found")
        t.deleted_at = utcnow()
        log_event(db, ws.id, "deleted", actor=user, task=t, title=t.title, via="mcp")
        db.commit()
        return {"deleted": True, "recoverable": True, "task_id": t.id}
    finally:
        db.close()


@mcp.tool()
def restore_task(task_id: int, board: str = "") -> dict:
    """Bring a deleted task back from the archive. Owner only."""
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "owner")
        if err:
            return err
        t = _task(db, ws, task_id, deleted=True)
        if t is None:
            return _fail("no deleted task with that id")
        t.deleted_at = None
        if t.status == "active":
            t.position = next_position(db, ws)
        log_event(db, ws.id, "restored", actor=user, task=t, via="mcp")
        db.commit()
        return {"restored": True, "task": _brief(db, t)}
    finally:
        db.close()


@mcp.tool()
def add_task_link(task_id: int, url: str, label: str = "", board: str = "") -> dict:
    """Point a task at something. Owner only, and a task may have several.

    `label` is what the link is called on the row — leave it out and a short form
    of the address is shown instead, because a bare 90-character URL in a pill
    wrecks the layout. A missing scheme is filled in as https, since a bare
    domain is what people paste and without one the browser reads it as a
    relative path and the link goes nowhere.
    """
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "owner")
        if err:
            return err
        t = _task(db, ws, task_id)
        if t is None:
            return _fail("task not found")
        link = add_link(db, t, url, label)
        if link is None:
            return _fail("a link needs an address")
        log_event(db, ws.id, "edited", actor=user, task=t, added_link=link.url,
                  via="mcp")
        db.commit()
        return {"added": True, "task": _brief(db, t)}
    finally:
        db.close()


@mcp.tool()
def remove_task_link(link_id: int, board: str = "") -> dict:
    """Take one link off a task. Owner only."""
    db = SessionLocal()
    try:
        ws, user, err = _open(db, board, "owner")
        if err:
            return err
        link = (db.query(Link).join(Task, Task.id == Link.task_id)
                  .filter(Link.id == link_id, Task.workspace_id == ws.id).first())
        if link is None:
            return _fail("link not found")
        task = link.task_id
        db.delete(link)
        db.commit()
        return {"removed": True, "task_id": task}
    finally:
        db.close()
