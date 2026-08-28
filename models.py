"""
Database models for TheList.

ORM: SQLAlchemy with SQLite (./data/thelist.db, persisted via a Docker volume).

Access model (SPEC.md §3.3): a Membership row IS the access. There is no
role='none' — absence of a row means the workspace does not exist for that user,
and routes answer 404 rather than 403 so the existence of a workspace is never
leaked.

Migration strategy (borant house pattern, same as PaperTrail): init_db() runs
ALTER TABLE for each added column on every startup; SQLite raises on duplicates,
which is caught and ignored. Additive only.

Two things in here are load-bearing and easy to undo by accident:

  * `Event` is the backbone (SPEC.md §3.8). Every transition stamps itself, and
    the report is a read over that table, never a set of counters somebody has
    to remember to increment.

  * `Event` carries a **snapshot** of who the task was for, how big it was and
    which tags it had. Reading those off the live Task row would let a rename
    today rewrite last quarter's report. An event is a dated fact, and dated
    facts do not get updated.
"""
import json
import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine, func, text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/thelist.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC, consistent with the rest of the borant tools."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── vocabularies ──────────────────────────────────────────────────────────────

# Four states, two of them terminal. This is deliberately not a kanban: with
# four states and two ends, columns would be four containers where three are
# empty (SPEC.md §13).
STATUSES = ["proposed", "active", "done", "rejected"]

STATUS_LABELS = {
    "proposed": "Proposed",
    "active":   "On the list",
    "done":     "Done",
    "rejected": "Declined",
}

ROLES = ["editor", "owner"]          # ordered: index is the rank
ROLE_LABELS = {"owner": "Owner", "editor": "Editor"}

# Effort (SPEC.md §5.7). Approved 27 Aug 2026 in answer to finding 13.
#
# The weights are an ordinal scale with the gaps widening upward, because the
# real ratio between an S and an L is almost never linear. They are CHOSEN, not
# measured, they are printed next to every total, and changing them rewrites the
# totals of the whole history — so if they ever change, they change once and the
# date gets written down.
EFFORTS = ["S", "M", "L"]
EFFORT_WEIGHTS = {"S": 1, "M": 3, "L": 8}
EFFORT_LABELS = {"S": "Small", "M": "Medium", "L": "Large"}
DEFAULT_EFFORT = "M"

EVENT_TYPES = [
    "created", "proposed", "accepted", "rejected", "edited", "reordered",
    "done", "reopened", "note_added", "deleted", "restored",
    "member_added", "member_removed", "for_changed", "person_renamed",
]

# Mail this app can send, and whether it is on unless somebody turns it off
# (SPEC.md §7). Notes are off because with two people a mail per note means
# notifications switched off within a week, including the ones that mattered.
NOTIFICATIONS = {
    "proposal_received": True,
    "proposal_decided":  True,
    "assigned":          True,
    "note_added":        False,
    "due_soon":          False,
}
NOTIFICATION_LABELS = {
    "proposal_received": "Someone proposes a task for my list",
    "proposal_decided":  "My proposal is accepted or declined",
    "assigned":          "A task is put in my name",
    "note_added":        "A note is added to a task I can see",
    "due_soon":          "Daily digest of what is due within a week",
}


# ── tables ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False, default="")
    hashed_password = Column(String, nullable=False)
    # The key is the subject, never the email: an address changes when somebody
    # changes institution, and whoever keyed on it rewrites a migration.
    borant_sub = Column(String, unique=True, nullable=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)

    @property
    def label(self) -> str:
        return self.name or self.email


class Workspace(Base):
    __tablename__ = "workspaces"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Optimistic lock on reordering (SPEC.md §5.2). Two people dragging the same
    # list is not a theoretical race when the whole point is two people.
    order_version = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    owner = relationship("User")


class Membership(Base):
    __tablename__ = "memberships"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String, nullable=False, default="editor")
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    user = relationship("User", foreign_keys=[user_id])
    workspace = relationship("Workspace")


class Person(Base):
    """Who a task is for (SPEC.md §3.5).

    A table and not a FK to `users`, because whoever *asks* for the work is
    usually outside the system and always will be. Keying this on accounts would
    have meant inventing fake users to count their requests — dirtying the
    identity table to draw a bar chart.

    `user_id` is the optional bridge: when the person does have an account, the
    link is what lets "tasks Nikola asked me for" and "tasks Nikola proposed"
    be recognised as the same human.
    """
    __tablename__ = "people"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    display_name = Column(String, nullable=False)
    # Normalised for uniqueness; `display_name` keeps the form first written.
    norm_name = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # The owner is a row with a flag, not a special value in the code: the
    # mine-vs-theirs share is then the same query as every other cut. The label
    # is their own name — see self_person() for why it is not "Me".
    is_owner_self = Column(Boolean, default=False, nullable=False)
    archived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("workspace_id", "norm_name"),)

    user = relationship("User")


class Tag(Base):
    __tablename__ = "tags"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String, nullable=False)          # normalised, lowercase
    display_name = Column(String, nullable=False)  # as first written
    created_at = Column(DateTime, default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)


class TaskTag(Base):
    __tablename__ = "task_tags"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    tag_id = Column(Integer, ForeignKey("tags.id"), nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "tag_id"),)

    tag = relationship("Tag")


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, default="", nullable=False)
    status = Column(String, default="active", nullable=False)

    # A flag, not a cadence, and not a generator of child rows: it changes what
    # completion MEANS (SPEC.md §5.3).
    recurring = Column(Boolean, default=False, nullable=False)
    effort = Column(String, default=DEFAULT_EFFORT, nullable=False)
    due_date = Column(Date, nullable=True)

    position = Column(Integer, default=0, nullable=False)
    for_person_id = Column(Integer, ForeignKey("people.id"), nullable=True)
    # Not exposed in v1: one visible person column at a time, or the two get
    # confused (SPEC.md §3.5).
    assignee_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Legacy: the single-link columns, kept because migrations here are additive
    # only and dropping a column in SQLite means rebuilding the table. They are
    # emptied by migrate_links() at startup and nothing reads them any more.
    link_url = Column(String, default="", nullable=False)
    link_label = Column(String, default="", nullable=False)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_reason = Column(Text, default="", nullable=False)

    last_done_at = Column(DateTime, nullable=True)   # recurring
    done_at = Column(DateTime, nullable=True)        # one-off
    deleted_at = Column(DateTime, nullable=True)

    person = relationship("Person", foreign_keys=[for_person_id])
    creator = relationship("User", foreign_keys=[created_by])
    tags = relationship("TaskTag", cascade="all, delete-orphan")
    links = relationship("Link", cascade="all, delete-orphan",
                         order_by="Link.position, Link.id")
    contacts = relationship("Contact", cascade="all, delete-orphan",
                            order_by="Contact.position, Contact.id")

    @property
    def tag_list(self):
        return sorted((tt.tag for tt in self.tags), key=lambda t: t.name)


class Link(Base):
    """A task can point at more than one thing, so links are rows.

    They started as two columns on `tasks` — one url, one label — which is the
    shape you pick when you imagine a task pointing at *the* paper. Real ones
    point at the paper, the shared folder and the thread where it was discussed.
    """
    __tablename__ = "links"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    url = Column(String, nullable=False)
    label = Column(String, default="", nullable=False)
    position = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    @property
    def text(self) -> str:
        """What to show: the label, or something short from the URL rather than
        the whole thing — a bare 90-character URL in a pill wrecks the row."""
        # `or ""`: a column default is applied at flush, so a Link that has just
        # been constructed still has None here — and that is exactly the object a
        # template renders when showing what was just added.
        if (self.label or "").strip():
            return self.label.strip()
        bare = (self.url or "").split("://", 1)[-1]
        return bare[:32] + ("…" if len(bare) > 32 else "")


class Contact(Base):
    """Somebody to talk to about this task, and why.

    Not a `Person`: those answer *for whom does this row exist* and are counted
    in the report. These answer *who do I have to write to* — the secretary who
    holds the room booking, the co-author who owes a paragraph. Putting them in
    the same table would have made the report count people who never asked for
    anything.
    """
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    name = Column(String, default="", nullable=False)
    email = Column(String, default="", nullable=False)
    reason = Column(String, default="", nullable=False)
    position = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    @property
    def label(self) -> str:
        return (self.name or "").strip() or (self.email or "").strip() or "someone"


class Note(Base):
    """Append-only, and not out of laziness (SPEC.md §3.7).

    Notes are the asynchronous channel between two people; a note that can be
    edited afterwards is a conversation nobody can rely on. The 15-minute window
    covers the typo and nothing else — after that, the correction is another
    note.
    """
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    author_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    deleted_at = Column(DateTime, nullable=True)

    author = relationship("User")

    EDIT_WINDOW_MINUTES = 15

    def deletable_by(self, user) -> bool:
        if self.deleted_at or not user or self.author_user_id != user.id:
            return False
        age = (utcnow() - self.created_at).total_seconds()
        return age <= self.EDIT_WINDOW_MINUTES * 60


class Event(Base):
    """The backbone (SPEC.md §3.8).

    `snap_*` are the reason the report can be trusted six months later: they
    record who the task was for, how big it was and which tags it had **at the
    moment the thing happened**. Reading that off the live Task row would let a
    rename today rewrite last quarter.
    """
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    type = Column(String, nullable=False, index=True)
    payload = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    snap_person_id = Column(Integer, ForeignKey("people.id"), nullable=True)
    snap_effort = Column(String, nullable=True)
    snap_tags = Column(String, default="", nullable=False)  # comma-separated, normalised

    actor = relationship("User")
    task = relationship("Task")

    @property
    def data(self) -> dict:
        try:
            return json.loads(self.payload or "{}")
        except ValueError:
            return {}

    @property
    def snap_tag_list(self) -> list:
        return [t for t in (self.snap_tags or "").split(",") if t]


class NotificationPref(Base):
    __tablename__ = "notification_prefs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_type = Column(String, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    __table_args__ = (UniqueConstraint("user_id", "event_type"),)


class Invitation(Base):
    __tablename__ = "invitations"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    email = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False, default="editor")
    token = Column(String, unique=True, nullable=False)
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    accepted_at = Column(DateTime, nullable=True)

    workspace = relationship("Workspace")


class Setting(Base):
    """Key/value for what is configured from the interface instead of the
    environment — today the SMTP relay. The password is stored Fernet-encrypted
    and is write-only in the form: an admin sets the relay up once and everybody
    else sends through it without ever being able to read it."""
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class ApiKey(Base):
    """An MCP key carries an identity, not a capability (SPEC.md §9).

    Every call runs as its owner and goes through the same access function as
    the web, so it reaches exactly what that person reaches.
    """
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    key = Column(String, unique=True, nullable=False, index=True)
    label = Column(String, default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User")


# ── session ───────────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── helpers: access ───────────────────────────────────────────────────────────

def role_for(db, workspace_id: int, user) -> str | None:
    """The role, or None. None means the workspace does not exist for them."""
    if user is None:
        return None
    m = (db.query(Membership)
           .filter(Membership.workspace_id == workspace_id,
                   Membership.user_id == user.id).first())
    return m.role if m else None


def has_role(role: str | None, minimum: str) -> bool:
    if role is None:
        return False
    return ROLES.index(role) >= ROLES.index(minimum)


def workspaces_of(db, user) -> list:
    if user is None:
        return []
    return (db.query(Workspace)
              .join(Membership, Membership.workspace_id == Workspace.id)
              .filter(Membership.user_id == user.id)
              .order_by(Workspace.id).all())


def bootstrap_admin(db, user) -> bool:
    """The first account to exist becomes the administrator.

    Somebody has to be able to configure the relay, and behind the gate the first
    arrival is whoever was granted access first — in practice the person who set
    the thing up. The alternative, shipping with a hardcoded admin or a flag in
    the environment, means a credential in a compose file for a role that only
    edits an SMTP host.

    It is a bootstrap and not a rule: after the first, everybody arrives plain,
    and admin is given deliberately from /admin or from `admin.py`. Worth knowing
    before you hand out grants — **whoever walks in first gets it**, so walk in
    first.
    """
    if db.query(User).filter(User.is_admin == True).first() is not None:  # noqa: E712
        return False
    user.is_admin = True
    return True


def ensure_one_admin(db) -> str | None:
    """If nobody can administer, give it to the oldest active account.

    Runs at startup, and exists because `bootstrap_admin` alone got it wrong the
    first time it met a database that already had people in it. On 27 Aug 2026
    the admin level shipped to an app with two accounts already created: the rule
    "first account with no admin around" then handed the level to **the next
    person to arrive** rather than to the one who had been there since the
    morning. The intention was always "whoever was here first"; with existing
    rows, only this reading of it is true.

    Returns the email it promoted, or None if somebody could already do the job.
    """
    if db.query(User).filter(User.is_admin == True,  # noqa: E712
                             User.is_active == True).first() is not None:  # noqa: E712
        return None
    first = (db.query(User).filter(User.is_active == True)  # noqa: E712
               .order_by(User.created_at, User.id).first())
    if first is None:
        return None
    first.is_admin = True
    return first.email


def ensure_workspace(db, user) -> Workspace:
    """Every user owns exactly one board, created on first sight.

    There is no route to create a second one: the "one workspace per user" rule
    lives in the code and not in the schema, so the day it changes it costs a
    button and not a migration (SPEC.md §3.2).
    """
    ws = db.query(Workspace).filter(Workspace.owner_user_id == user.id).first()
    if ws is None:
        ws = Workspace(name=f"{user.label}'s list", owner_user_id=user.id)
        db.add(ws)
        db.flush()
        db.add(Membership(workspace_id=ws.id, user_id=user.id, role="owner"))
        db.flush()
    # The owner-as-person exists from the first moment, because it is the default
    # of every new task, and a default created lazily is a default that will one
    # day be missing.
    self_person(db, ws)
    return ws


# ── helpers: people ───────────────────────────────────────────────────────────

def normalise(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def self_person(db, ws: Workspace) -> Person:
    """The owner, as a person a task can be for — under their own name.

    It used to be called "Me", which reads fine on your own board and is
    misleading everywhere else: an editor looking at somebody else's list saw
    tasks "for Me" meaning somebody who is not them, and the report had a row
    called Me next to rows called Markus and Nikola. A name is a name.

    The label follows the owner's, here rather than at signup, so an account
    renamed at the gate does not leave a stale name behind — and so boards
    created before this change repair themselves the first time they are opened.
    """
    owner = db.query(User).get(ws.owner_user_id)
    wanted = (owner.label if owner is not None else "").strip() or "Owner"
    p = (db.query(Person)
           .filter(Person.workspace_id == ws.id,
                   Person.is_owner_self == True).first())  # noqa: E712
    if p is None:
        p = Person(workspace_id=ws.id, display_name=wanted,
                   norm_name=normalise(wanted), user_id=ws.owner_user_id,
                   is_owner_self=True)
        db.add(p)
        db.flush()
        return p

    if p.norm_name != normalise(wanted):
        # Unless somebody else on this board already goes by that name. Merging
        # the two would silently reattribute their tasks, which is a decision for
        # a human and not a side effect of opening a page: leave the label alone
        # and let it be renamed deliberately.
        clash = (db.query(Person)
                   .filter(Person.workspace_id == ws.id,
                           Person.norm_name == normalise(wanted),
                           Person.id != p.id).first())
        if clash is None:
            p.display_name, p.norm_name = wanted, normalise(wanted)
    return p


def get_or_create_person(db, ws: Workspace, name: str, user=None) -> Person | None:
    """Find by normalised name, or make one. Empty name -> None."""
    norm = normalise(name)
    if not norm:
        return None
    p = (db.query(Person)
           .filter(Person.workspace_id == ws.id, Person.norm_name == norm).first())
    if p is None:
        p = Person(workspace_id=ws.id, display_name=name.strip(), norm_name=norm,
                   user_id=user.id if user else None)
        db.add(p)
        db.flush()
    elif p.archived_at is not None:
        # Naming an archived person again is how you bring them back: the
        # alternative is a duplicate row with a number stuck on the end.
        p.archived_at = None
    if user is not None and p.user_id is None:
        p.user_id = user.id
    return p


def person_for_proposal(db, ws: Workspace, proposer) -> Person:
    """A task proposed by an editor is for that editor unless told otherwise.

    This is the one case where the right default is deducible, so it gets
    deduced (SPEC.md §11, finding 14).
    """
    if proposer is not None:
        p = (db.query(Person)
               .filter(Person.workspace_id == ws.id,
                       Person.user_id == proposer.id,
                       Person.is_owner_self == False).first())  # noqa: E712
        if p is not None:
            return p
        if proposer.id != ws.owner_user_id:
            return get_or_create_person(db, ws, proposer.label, user=proposer)
    return self_person(db, ws)


def people_of(db, ws: Workspace, include_archived: bool = False) -> list:
    q = db.query(Person).filter(Person.workspace_id == ws.id)
    if not include_archived:
        q = q.filter(Person.archived_at.is_(None))
    return q.order_by(Person.is_owner_self.desc(), Person.norm_name).all()


# ── helpers: tags ─────────────────────────────────────────────────────────────

def get_or_create_tag(db, ws: Workspace, raw: str) -> Tag | None:
    norm = normalise(raw)
    if not norm:
        return None
    t = db.query(Tag).filter(Tag.workspace_id == ws.id, Tag.name == norm).first()
    if t is None:
        t = Tag(workspace_id=ws.id, name=norm, display_name=raw.strip())
        db.add(t)
        db.flush()
    return t


def set_tags(db, ws: Workspace, task: Task, raw: str) -> None:
    """Replace a task's tags from a comma-separated string."""
    wanted = []
    for chunk in (raw or "").replace(";", ",").split(","):
        t = get_or_create_tag(db, ws, chunk)
        if t is not None and t.id not in [x.id for x in wanted]:
            wanted.append(t)
    # Through the relationship and not through db.add(): the collection has
    # already been read by this point, so a bare INSERT leaves `task.tags` stale
    # in memory — and the very next thing that happens is log_event() taking a
    # snapshot of the tags. That is how the `created` event was born with no
    # tags on it while `done`, rendered a request later, had them all.
    have = {tt.tag_id: tt for tt in task.tags}
    keep = {t.id for t in wanted}
    for tag in wanted:
        if tag.id not in have:
            task.tags.append(TaskTag(tag=tag))
    for tag_id, tt in list(have.items()):
        if tag_id not in keep:
            task.tags.remove(tt)
    db.flush()


def tags_of(db, ws: Workspace) -> list:
    return db.query(Tag).filter(Tag.workspace_id == ws.id).order_by(Tag.name).all()


def tag_names(task: Task) -> str:
    return ",".join(t.name for t in task.tag_list)


# ── helpers: events ───────────────────────────────────────────────────────────

def log_event(db, ws_id: int, type_: str, actor=None, task: Task = None,
              **payload) -> Event:
    """Stamp a fact, with the snapshot that makes it readable later."""
    ev = Event(
        workspace_id=ws_id,
        task_id=task.id if task is not None else None,
        actor_user_id=actor.id if actor is not None else None,
        type=type_,
        payload=json.dumps(payload, default=str) if payload else "",
        snap_person_id=task.for_person_id if task is not None else None,
        snap_effort=task.effort if task is not None else None,
        snap_tags=tag_names(task) if task is not None else "",
    )
    db.add(ev)
    db.flush()
    return ev


# ── helpers: the list ─────────────────────────────────────────────────────────

def active_tasks(db, ws: Workspace):
    return (db.query(Task)
              .filter(Task.workspace_id == ws.id, Task.status == "active",
                      Task.deleted_at.is_(None))
              .order_by(Task.position, Task.id))


def next_position(db, ws: Workspace) -> int:
    top = (db.query(func.max(Task.position))
             .filter(Task.workspace_id == ws.id, Task.status == "active")
             .scalar())
    return (top or 0) + 1


def apply_order(db, ws: Workspace, ordered_ids: list, expected_version: int,
                actor=None) -> str:
    """Rewrite the whole order in one transaction, or refuse.

    The client sends the complete array plus the `order_version` it read. With a
    few dozen rows the whole array costs nothing and has none of the drift of
    fractional positions; the version settles the race that genuinely exists
    when the whole point of the tool is two people. Losing one drag is
    acceptable — applying two overlapping ones in an arbitrary order is not.
    """
    if expected_version != ws.order_version:
        return "stale_order"
    rows = {t.id: t for t in active_tasks(db, ws).all()}
    if set(ordered_ids) != set(rows.keys()):
        # The list changed under the drag (something was added, completed or
        # deleted elsewhere). Same answer as a stale version: reload and redo.
        return "stale_order"
    for i, tid in enumerate(ordered_ids, start=1):
        rows[tid].position = i
    ws.order_version += 1
    log_event(db, ws.id, "reordered", actor=actor, count=len(ordered_ids))
    return "ok"


def mark_done(db, ws: Workspace, task: Task, actor=None) -> None:
    """What completion means, and it is the whole point of `recurring`.

    One-off: archived, out of the list, position released.
    Recurring: stays exactly where it is, records when it was last done, and
    drops its due date — a past date on a row that stays alive is permanent
    noise (SPEC.md §5.3).
    """
    now = utcnow()
    log_event(db, ws.id, "done", actor=actor, task=task,
              recurring=task.recurring)
    task.last_done_at = now
    if task.recurring:
        task.due_date = None
    else:
        task.status = "done"
        task.done_at = now
        task.position = 0


def reopen(db, ws: Workspace, task: Task, actor=None) -> None:
    task.status = "active"
    task.done_at = None
    task.position = next_position(db, ws)
    log_event(db, ws.id, "reopened", actor=actor, task=task)


# ── the report (SPEC.md §5.6) ─────────────────────────────────────────────────

def report(db, ws: Workspace, start: datetime, end: datetime) -> dict:
    """Counts, per person and per tag, plus the weighted column beside them.

    Read from the event log and aggregated in Python on purpose: at this scale
    the period holds tens of events, not millions, and the alternative is JSON
    extraction in SQL for no gain.

    The two columns — raw and weighted — are the honesty check. While nobody
    touches the effort menu the weighted column is the raw one times three, and
    that is VISIBLE. A weighted column on its own would have hidden exactly the
    case where the weight was never used.
    """
    evs = (db.query(Event)
             .filter(Event.workspace_id == ws.id,
                     Event.created_at >= start, Event.created_at < end,
                     Event.type.in_(("created", "done")))
             .all())
    people = {p.id: p for p in people_of(db, ws, include_archived=True)}
    tags = {t.name: t for t in tags_of(db, ws)}

    def blank():
        return {"opened": 0, "closed": 0, "touches": 0, "weighted": 0, "listed": 0}

    by_person, by_tag = {}, {}
    for ev in evs:
        w = EFFORT_WEIGHTS.get(ev.snap_effort or DEFAULT_EFFORT, EFFORT_WEIGHTS[DEFAULT_EFFORT])
        buckets = [by_person.setdefault(ev.snap_person_id, blank())]
        for name in ev.snap_tag_list:
            buckets.append(by_tag.setdefault(name, blank()))
        for b in buckets:
            if ev.type == "created":
                b["opened"] += 1
            else:
                b["touches"] += 1
                b["weighted"] += w
                if not ev.data.get("recurring"):
                    b["closed"] += 1

    for t in active_tasks(db, ws).all():
        by_person.setdefault(t.for_person_id, blank())["listed"] += 1
        for tag in t.tag_list:
            by_tag.setdefault(tag.name, blank())["listed"] += 1

    def rows(buckets, resolve):
        total = sum(b["weighted"] for b in buckets.values()) or 0
        out = []
        for key, b in buckets.items():
            label, is_self = resolve(key)
            out.append({**b, "label": label, "is_self": is_self,
                        "share": round(100 * b["weighted"] / total, 1) if total else 0.0})
        out.sort(key=lambda r: (-r["weighted"], -r["listed"], r["label"].lower()))
        return out

    def person_label(pid):
        p = people.get(pid)
        if p is None:
            return ("Unassigned", False)
        return (p.display_name, bool(p.is_owner_self))

    return {
        "people": rows(by_person, person_label),
        "tags": rows(by_tag, lambda n: (tags[n].display_name if n in tags else n, False)),
        "weights": EFFORT_WEIGHTS,
        "start": start, "end": end,
    }


# ── init / migrations ─────────────────────────────────────────────────────────

# Additive columns added after the first deploy go here, in the house form:
# every startup tries them, SQLite refuses the duplicates, we ignore the refusal.
_MIGRATIONS = [
    # ("tasks", "some_new_column TEXT DEFAULT ''"),
]


def migrate_links(db) -> int:
    """Move the old single link onto the links table. Idempotent.

    Runs at startup rather than as a one-off script, because a one-off script is
    a thing somebody has to remember to run on a box they are not looking at.
    """
    moved = 0
    rows = (db.query(Task)
              .filter(Task.link_url != "", Task.link_url.isnot(None)).all())
    for t in rows:
        if not any(l.url == t.link_url for l in t.links):
            t.links.append(Link(url=t.link_url, label=t.link_label or "", position=0))
            moved += 1
        t.link_url, t.link_label = "", ""
    return moved


def add_contact(db, task: Task, name: str, email: str = "",
                reason: str = "") -> Contact | None:
    """A contact needs a name or an address; the reason is what makes it useful
    six weeks later, and it is optional because forcing it would get it filled
    with a full stop."""
    name, email = (name or "").strip(), (email or "").strip()
    if not name and not email:
        return None
    top = max([c.position for c in task.contacts], default=0)
    c = Contact(name=name, email=email, reason=(reason or "").strip(),
                position=top + 1)
    task.contacts.append(c)
    return c


def add_link(db, task: Task, url: str, label: str = "") -> Link | None:
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        # A bare domain is what people paste; without a scheme the browser reads
        # it as a relative path and the link silently goes nowhere.
        url = "https://" + url
    top = max([l.position for l in task.links], default=0)
    link = Link(url=url, label=(label or "").strip(), position=top + 1)
    task.links.append(link)
    return link


def init_db() -> None:
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        for table, column in _MIGRATIONS:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column}"))
                conn.commit()
            except Exception:
                conn.rollback()


def new_api_key() -> str:
    return secrets.token_urlsafe(32)


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)
