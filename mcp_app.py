"""
The model-facing surface of TheList. **Read-only in Fase 1.**

The question this exists for is the one that arrives in conversation rather than
in front of a page: what is due, what is waiting for me to decide, and how much
of this quarter went to somebody else's work.

Access. Every call runs as the human who owns the API key, and every board
lookup goes through the same access function the web app uses. The surface has
exactly the reach of its owner, no more. A board the caller is not a member of
answers "not found" and never "forbidden", or the model could enumerate what it
cannot read.

Errors are returned as {"error": ...} rather than raised: a tool that throws
gives the model a stack trace to hallucinate around, while a message it can read
lets it correct course.

Writing is deliberately not here. `add_note` would be the natural one, and it is
postponed: the first version gets looked at before it gets written to.
"""
from datetime import date, datetime, timedelta

from mcp.server.mcpserver import MCPServer

import auth
from models import (
    EFFORT_WEIGHTS, STATUS_LABELS, Note, SessionLocal, Task, User, Workspace,
    active_tasks, people_of, report, role_for, tags_of, utcnow, workspaces_of,
)

mcp = MCPServer(
    name="thelist",
    instructions=(
        "A list of macro-tasks, one board per person: what is on it, what is due, "
        "what is waiting for a decision, and who the work is for. Read-only — every "
        "change is made by a human in the web app. Start with list_boards. "
        "Note that `workload` counts tasks and completions, NOT time: report those "
        "numbers as frequency and volume, never as hours."
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
        "link": t.link_url or None,
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
            return _fail("board not found")
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
            return _fail("board not found")
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
            return _fail("board not found")
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
            return _fail("board not found")
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
            return _fail("board not found")
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
            return _fail("board not found")
        return {
            "board": ws.name,
            "tags": [t.display_name for t in tags_of(db, ws)],
            "people": [{"name": p.display_name, "is_me": bool(p.is_owner_self)}
                       for p in people_of(db, ws)],
            "efforts": EFFORT_WEIGHTS,
        }
    finally:
        db.close()
