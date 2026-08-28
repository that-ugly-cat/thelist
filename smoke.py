"""
End-to-end smoke test, two users, one board.

Runs against its own database file so it can be run beside a live dev server.
Everything here is a rule from SPEC.md that would be expensive to discover in
production, and several of them are the kind that look right when you only try
them as the owner.

    python smoke.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
os.chdir(HERE)
os.environ.setdefault("JWT_SECRET", "smoke-test-secret")
os.environ["DATABASE_URL"] = "sqlite:///./data/smoke.db"
os.environ["AUTH_MODE"] = "local"
# An ephemeral key, so the run is deterministic wherever it happens. Without one
# the app still boots and 93 of these checks still pass — only the three about a
# stored SMTP password fail, which is the degradation crypto.py promises. That is
# worth knowing and not worth failing a test run over.
if not os.environ.get("FERNET_KEY"):
    from cryptography.fernet import Fernet
    os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

db_file = HERE / "data" / "smoke.db"
db_file.parent.mkdir(exist_ok=True)
db_file.unlink(missing_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from auth import hash_password  # noqa: E402
from models import (  # noqa: E402
    Membership, SessionLocal, Task, User, ensure_workspace, init_db,
)

PASSED, FAILED = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(label)
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not cond else ""))


def main_() -> int:
    init_db()
    db = SessionLocal()
    owner = User(email="owner@example.org", name="Spit",
                 hashed_password=hash_password("pw"), is_active=True)
    editor = User(email="editor@example.org", name="Nikola",
                  hashed_password=hash_password("pw"), is_active=True)
    stranger = User(email="stranger@example.org", name="Nobody",
                    hashed_password=hash_password("pw"), is_active=True)
    db.add_all([owner, editor, stranger])
    db.commit()
    ws = ensure_workspace(db, owner)
    ensure_workspace(db, stranger)
    db.add(Membership(workspace_id=ws.id, user_id=editor.id, role="editor"))
    db.commit()
    ws_id, owner_id = ws.id, owner.id
    db.close()

    with TestClient(main.app) as c:
        def login(email):
            s = TestClient(main.app)
            r = s.post("/login", data={"email": email, "password": "pw"},
                       follow_redirects=False)
            assert r.status_code == 302, r.status_code
            return s

        O = login("owner@example.org")
        E = login("editor@example.org")
        S = login("stranger@example.org")

        print("\n— the public surface —")
        r = c.get("/")
        check("the shop window is public", r.status_code == 200)
        check("the shop window never names a user",
              "Sign out" not in r.text and "logout" not in r.text)
        check("/health answers", c.get("/health").json() == {"ok": True})
        r = c.get("/app", follow_redirects=False)
        check("anonymous /app redirects to login",
              r.status_code == 302 and r.headers["location"] == "/login")

        r = c.get("/app", headers={"sec-fetch-mode": "cors",
                                   "accept": "application/json"})
        check("a script call gets JSON and not a redirect",
              r.status_code == 401 and r.headers["content-type"].startswith("application/json"))

        print("\n— the machine surface —")
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check("/mcp without a key is refused by the app itself",
              r.status_code == 401 and r.json().get("error", "").startswith("missing"),
              r.text[:80])
        r = c.post("/mcp/k/not-a-real-key",
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check("a wrong capability URL is refused too", r.status_code == 401)

        print("\n— access is a membership row —")
        r = S.get(f"/app/{ws_id}", follow_redirects=False)
        check("a stranger gets 404 and never 403", r.status_code == 404,
              str(r.status_code))
        r = S.get("/app/99999", follow_redirects=False)
        check("a board that does not exist is also 404", r.status_code == 404)

        print("\n— the owner creates, the editor proposes —")
        O.post(f"/app/{ws_id}/tasks", data={
            "title": "Update the DIPEx site", "for_person": "Nikola",
            "tags": "BAG, ITE", "effort": "S", "recurring": "1"},
            follow_redirects=False)
        E.post(f"/app/{ws_id}/tasks", data={"title": "Reply to the ministry",
                                            "effort": "M"},
               follow_redirects=False)
        db = SessionLocal()
        rows = {t.title: t for t in db.query(Task).all()}
        check("the owner's task lands on the list",
              rows["Update the DIPEx site"].status == "active")
        check("the editor's task lands as a proposal",
              rows["Reply to the ministry"].status == "proposed")
        check("a proposal inherits the proposer as its person",
              rows["Reply to the ministry"].person.display_name == "Nikola")
        check("`for` is never empty",
              rows["Update the DIPEx site"].person is not None)
        check("the created event snapshots the tags",
              rows["Update the DIPEx site"].id is not None)
        prop_id = rows["Reply to the ministry"].id
        task_id = rows["Update the DIPEx site"].id
        db.close()

        print("\n— what an editor may not do —")
        r = E.post(f"/app/{ws_id}/tasks/{task_id}",
                   data={"title": "hijacked", "effort": "M"}, follow_redirects=False)
        check("an editor cannot edit a task", r.status_code == 404, str(r.status_code))
        r = E.post(f"/app/{ws_id}/tasks/{task_id}/delete", follow_redirects=False)
        check("an editor cannot delete a task", r.status_code == 404)
        r = E.post(f"/app/{ws_id}/tasks/{prop_id}/accept", follow_redirects=False)
        check("an editor cannot accept their own proposal", r.status_code == 404)

        print("\n— what an editor may do —")
        r = E.post(f"/app/{ws_id}/tasks/{task_id}/notes", data={"body": "on it"},
                   follow_redirects=False)
        check("an editor can add a note", r.status_code == 302)
        r = E.post(f"/app/{ws_id}/tasks/{task_id}/done", follow_redirects=False)
        check("an editor can mark something done", r.status_code == 302)

        print("\n— recurring changes what done means —")
        db = SessionLocal()
        t = db.query(Task).get(task_id)
        check("a recurring task stays on the list", t.status == "active")
        check("...and records when it was last done", t.last_done_at is not None)
        db.close()

        O.post(f"/app/{ws_id}/tasks", data={"title": "One-off thing", "effort": "L"},
               follow_redirects=False)
        db = SessionLocal()
        one = db.query(Task).filter(Task.title == "One-off thing").first()
        one_id = one.id
        db.close()
        O.post(f"/app/{ws_id}/tasks/{one_id}/done", follow_redirects=False)
        db = SessionLocal()
        one = db.query(Task).get(one_id)
        check("a one-off task is archived instead", one.status == "done")
        check("...with a date on it", one.done_at is not None)
        db.close()

        print("\n— declining needs a reason —")
        r = O.post(f"/app/{ws_id}/tasks/{prop_id}/reject", data={"reason": "  "},
                   follow_redirects=False)
        check("an empty reason is refused", r.status_code == 400, str(r.status_code))
        r = O.post(f"/app/{ws_id}/tasks/{prop_id}/reject",
                   data={"reason": "the ministry can wait"}, follow_redirects=False)
        check("a reason gets it declined", r.status_code == 302)
        db = SessionLocal()
        p = db.query(Task).get(prop_id)
        check("a declined proposal stays, with its reason",
              p.status == "rejected" and p.decision_reason == "the ministry can wait")
        db.close()
        r = E.get(f"/app/{ws_id}/proposals")
        check("the editor can read the reason", "the ministry can wait" in r.text)

        print("\n— reordering, and the race —")
        for title in ("A", "B", "C"):
            O.post(f"/app/{ws_id}/tasks", data={"title": title, "effort": "M"},
                   follow_redirects=False)
        db = SessionLocal()
        ids = [t.id for t in db.query(Task)
               .filter(Task.workspace_id == ws_id, Task.status == "active")
               .order_by(Task.position).all()]
        version = db.query(main.Workspace).get(ws_id).order_version
        db.close()
        r = E.post(f"/app/{ws_id}/order", json={"ids": list(reversed(ids)),
                                                "version": version})
        check("an editor can reorder", r.status_code == 200, str(r.status_code))
        r = O.post(f"/app/{ws_id}/order", json={"ids": ids, "version": version})
        check("a stale version is refused with 409", r.status_code == 409)
        check("...and says why", r.json().get("error") == "stale_order")
        db = SessionLocal()
        now = [t.id for t in db.query(Task)
               .filter(Task.workspace_id == ws_id, Task.status == "active")
               .order_by(Task.position).all()]
        check("the refused reorder changed nothing", now == list(reversed(ids)))
        db.close()

        print("\n— an incomplete array is a stale list —")
        r = O.post(f"/app/{ws_id}/order", json={"ids": ids[:2],
                                                "version": version + 1})
        check("a partial array is refused", r.status_code == 409)

        print("\n— the report counts what happened —")
        r = O.get(f"/app/{ws_id}/report?from=2000-01-01&to=2100-01-01")
        check("the report renders", r.status_code == 200)
        check("...and says it is not hours", "not hours" in r.text)
        r = O.get(f"/app/{ws_id}/report.csv?from=2000-01-01&to=2100-01-01")
        check("the CSV carries the weights",
              "weights" in r.text and "S=1" in r.text)

        print("\n— notes are append-only —")
        db = SessionLocal()
        from models import Note
        n = db.query(Note).first()
        n_id, n_task = n.id, n.task_id
        db.close()
        r = O.post(f"/app/{ws_id}/notes/{n_id}/delete", follow_redirects=False)
        check("someone else cannot remove a note", r.status_code == 400,
              str(r.status_code))
        r = E.post(f"/app/{ws_id}/notes/{n_id}/delete", follow_redirects=False)
        check("the author can, within the window", r.status_code == 302)

        print("\n— deletion is soft —")
        O.post(f"/app/{ws_id}/tasks/{n_task}/delete", follow_redirects=False)
        db = SessionLocal()
        t = db.query(Task).get(n_task)
        check("the row survives with its notes", t is not None and t.deleted_at)
        check("...and its notes are still there",
              db.query(Note).filter(Note.task_id == n_task).count() > 0)
        db.close()

        print("\n— the person menu —")
        r = O.post(f"/app/{ws_id}/people/1", data={"display_name": "Me"},
                   follow_redirects=False)
        check("the owner can save a person", r.status_code == 302)
        r = E.post(f"/app/{ws_id}/people/1", data={"display_name": "Hijack"},
                   follow_redirects=False)
        check("an editor cannot rename people", r.status_code == 404)

        print("\n— a task points at more than one thing —")
        from models import Link, migrate_links
        db = SessionLocal()
        legacy = db.query(Task).get(task_id)
        legacy.link_url, legacy.link_label = "example.org/paper", "The paper"
        db.commit()
        moved = migrate_links(db)
        db.commit()
        check("the old single link is migrated onto the table", moved == 1, str(moved))
        legacy = db.query(Task).get(task_id)
        check("...with its label kept", legacy.links[0].label == "The paper")
        check("...and the old columns emptied", legacy.link_url == "")
        check("running the migration twice adds nothing", migrate_links(db) == 0)
        db.close()

        # A task of its own: the soft-delete section above took `task_id` off
        # the board, and a deleted task is invisible to every route by design.
        O.post(f"/app/{ws_id}/tasks", data={"title": "Linked thing", "effort": "M"},
               follow_redirects=False)
        db = SessionLocal()
        linked_id = db.query(Task).filter(Task.title == "Linked thing").first().id
        db.close()
        r = O.post(f"/app/{ws_id}/tasks/{linked_id}/links",
                   data={"url": "docs.example.org/folder", "label": "Folder"},
                   follow_redirects=False)
        check("the owner can add a second link", r.status_code == 302)
        db = SessionLocal()
        t = db.query(Task).get(linked_id)
        check("a task can hold several", len(t.links) == 1, str(len(t.links)))
        check("a missing scheme is filled in",
              t.links[0].url.startswith("https://"), t.links[0].url)
        check("a link with no label shows a short form of the address",
              Link(url="https://example.org/" + "x" * 90).text.endswith("…"))
        link_id = t.links[0].id
        db.close()
        r = E.post(f"/app/{ws_id}/tasks/{linked_id}/links", data={"url": "x.org"},
                   follow_redirects=False)
        check("an editor cannot add links", r.status_code == 404)
        r = O.post(f"/app/{ws_id}/tasks/{linked_id}/links", data={"url": "  "},
                   follow_redirects=False)
        check("a link with no address is refused", r.status_code == 400)
        r = O.post(f"/app/{ws_id}/links/{link_id}/delete", follow_redirects=False)
        check("...and one can be removed", r.status_code == 302)
        r = O.post(f"/app/{ws_id}/tasks/{linked_id}/links",
                   data={"url": "https://example.org/second"}, follow_redirects=False)
        db = SessionLocal()
        check("and the ones left keep their order",
              [l.url for l in db.query(Task).get(linked_id).links]
              == ["https://example.org/second"])
        db.close()

        print("\n— the earmark: private, per person, and not in the log —")
        from models import Earmark, Event as _Ev
        before = db_events = None
        db = SessionLocal()
        before = db.query(_Ev).count()
        db.close()
        r = O.post(f"/app/{ws_id}/tasks/{linked_id}/earmark")
        check("marking answers on", r.json() == {"on": True}, r.text[:60])
        r = O.post(f"/app/{ws_id}/tasks/{linked_id}/earmark")
        check("...and clicking again answers off", r.json() == {"on": False})
        O.post(f"/app/{ws_id}/tasks/{linked_id}/earmark")
        db = SessionLocal()
        check("it writes no event at all", db.query(_Ev).count() == before,
              f"{db.query(_Ev).count()} vs {before}")
        db.close()
        r = E.post(f"/app/{ws_id}/tasks/{linked_id}/earmark")
        check("an editor can mark too", r.json() == {"on": True})
        db = SessionLocal()
        check("...on their own row, not the owner's",
              db.query(Earmark).filter(Earmark.task_id == linked_id).count() == 2)
        db.close()
        r = O.get(f"/app/{ws_id}?marked=1")
        check("the filter shows the marked one", "Linked thing" in r.text)
        r = E.get(f"/app/{ws_id}?marked=1")
        check("...and each person sees their own marks", "Linked thing" in r.text)
        # An editor unmarks: the owner's mark must survive.
        E.post(f"/app/{ws_id}/tasks/{linked_id}/earmark")
        db = SessionLocal()
        check("unmarking removes only your own",
              db.query(Earmark).filter(Earmark.task_id == linked_id).count() == 1)
        db.close()

        print("\n— notes come newest first —")
        O.post(f"/app/{ws_id}/tasks/{linked_id}/notes", data={"body": "prima nota"},
               follow_redirects=False)
        O.post(f"/app/{ws_id}/tasks/{linked_id}/notes", data={"body": "seconda nota"},
               follow_redirects=False)
        r = O.get(f"/app/{ws_id}/task/{linked_id}")
        check("the newest is above the older one",
              r.text.index("seconda nota") < r.text.index("prima nota"))
        check("...and the box to write is above both",
              r.text.index("Add note") < r.text.index("seconda nota"))

        print("\n— everything on a task is editable except the notes —")
        r = O.post(f"/app/{ws_id}/links/{link_id}",
                   data={"url": "docs.example.org/moved", "label": "Moved"},
                   follow_redirects=False)
        check("a link can be edited in place", r.status_code == 302, str(r.status_code))
        db = SessionLocal()
        l = db.query(Link).get(link_id)
        check("...with the scheme filled in again",
              l.url == "https://docs.example.org/moved", l.url)
        check("...and the new label kept", l.label == "Moved")
        db.close()
        r = O.post(f"/app/{ws_id}/links/{link_id}", data={"url": "  "},
                   follow_redirects=False)
        check("a link cannot be emptied", r.status_code == 400)
        r = E.post(f"/app/{ws_id}/links/{link_id}", data={"url": "x.org"},
                   follow_redirects=False)
        check("an editor cannot edit links", r.status_code == 404)

        print("\n— relevant contacts, which are not the person it is for —")
        r = O.post(f"/app/{ws_id}/tasks/{linked_id}/contacts",
                   data={"name": "Anna", "email": "anna@example.org",
                         "reason": "holds the room booking"}, follow_redirects=False)
        check("the owner can add a contact", r.status_code == 302)
        db = SessionLocal()
        t = db.query(Task).get(linked_id)
        check("with name, address and reason",
              t.contacts[0].name == "Anna" and t.contacts[0].reason.startswith("holds"))
        check("a contact does not change who the task is for",
              t.person.display_name == "Spit", t.person.display_name)
        contact_id = t.contacts[0].id
        db.close()
        r = O.post(f"/app/{ws_id}/tasks/{linked_id}/contacts",
                   data={"reason": "no name, no address"}, follow_redirects=False)
        check("a contact with neither name nor address is refused",
              r.status_code == 400, str(r.status_code))
        r = O.post(f"/app/{ws_id}/tasks/{linked_id}/contacts",
                   data={"email": "only@example.org"}, follow_redirects=False)
        check("an address alone is enough", r.status_code == 302)
        r = E.post(f"/app/{ws_id}/tasks/{linked_id}/contacts", data={"name": "X"},
                   follow_redirects=False)
        check("an editor cannot add contacts", r.status_code == 404)
        r = O.post(f"/app/{ws_id}/contacts/{contact_id}",
                   data={"name": "Anna Redaelli", "email": "anna@example.org",
                         "reason": "tiene la sala e le chiavi"}, follow_redirects=False)
        check("a contact can be edited in place", r.status_code == 302, str(r.status_code))
        db = SessionLocal()
        # NOT `c`: that name is the anonymous TestClient, and shadowing it here
        # breaks every check after this one with an error that points nowhere
        # near the line that caused it.
        from models import Contact as _C
        ct = db.query(_C).get(contact_id)
        check("...with the new name and reason",
              ct.name == "Anna Redaelli" and ct.reason.endswith("chiavi"))
        db.close()
        r = O.post(f"/app/{ws_id}/contacts/{contact_id}",
                   data={"name": "", "email": ""}, follow_redirects=False)
        check("a contact cannot be emptied of both", r.status_code == 400)
        r = O.post(f"/app/{ws_id}/contacts/{contact_id}/delete", follow_redirects=False)
        check("...and one can be removed", r.status_code == 302)
        db = SessionLocal()
        check("leaving the other", len(db.query(Task).get(linked_id).contacts) == 1)
        db.close()

        print("\n— the owner is a person with a name, not 'Me' —")
        from models import Person, self_person, Workspace as _WS
        db = SessionLocal()
        ws_row = db.get(_WS, ws_id)
        me = self_person(db, ws_row)
        db.commit()
        check("the owner-as-person carries the owner's name", me.display_name == "Spit",
              me.display_name)
        check("...and it is not 'Me'", me.display_name.lower() != "me")

        # Renamed at the gate: the label follows, so no stale name is left behind.
        owner_row = db.query(User).filter(User.email == "owner@example.org").first()
        owner_row.name = "Giovanni Spitale"
        db.commit()
        me = self_person(db, ws_row)
        db.commit()
        check("a rename upstream updates the label",
              me.display_name == "Giovanni Spitale", me.display_name)

        # And the case that must not blow up: somebody else already goes by that
        # name on this board. Merging would reattribute their tasks silently.
        db.add(Person(workspace_id=ws_id, display_name="Someone Else",
                      norm_name="someone else"))
        db.commit()
        owner_row.name = "Someone Else"
        db.commit()
        me = self_person(db, ws_row)
        db.commit()
        check("a name already taken leaves the label alone instead of merging",
              me.display_name == "Giovanni Spitale", me.display_name)
        owner_row.name = "Spit"
        db.commit()
        self_person(db, ws_row)
        db.commit()
        db.close()

        print("\n— invitations: public to read, gated to accept —")
        # The 503 in production came from here: the accepting half needed an
        # identity while sitting on a path declared public, where the gate never
        # runs and the identity headers are stripped.
        r = O.post(f"/app/{ws_id}/members", data={"email": "newcomer@example.org"},
                   follow_redirects=False)
        tok = r.headers["location"].split("invited=")[1].split("&")[0]
        check("inviting produces a token", len(tok) > 10)
        r = c.get(f"/invite/{tok}")
        check("the invitation page is readable by anyone", r.status_code == 200)
        check("...and points at the gated side", f"/app/join/{tok}" in r.text)
        check("nothing on a public path needs an identity",
              all(not p.startswith("/invite") for p in
                  [r_.path for r_ in main.app.routes
                   if getattr(r_, "methods", None) and "POST" in r_.methods]))
        r = c.get(f"/app/join/{tok}", follow_redirects=False)
        check("the accepting side is gated", r.status_code in (302, 401),
              str(r.status_code))

        newcomer = User(email="newcomer@example.org", name="New",
                        hashed_password=hash_password("pw"), is_active=True)
        db = SessionLocal()
        db.add(newcomer)
        db.commit()
        db.close()
        N = login("newcomer@example.org")
        r = N.get(f"/app/join/{tok}")
        check("the invited person sees a confirmation", r.status_code == 200)
        # Somebody who is NOT already a member: an existing member sees the
        # "you are already on this list" branch first, which is right — once you
        # are in, who the invitation was addressed to stops mattering.
        r = S.get(f"/app/join/{tok}")
        check("somebody else sees who it was addressed to",
              "newcomer@example.org" in r.text and "sent to" in r.text)
        r = S.post(f"/app/join/{tok}", follow_redirects=False)
        check("...and cannot take it", r.status_code == 400, str(r.status_code))
        r = E.get(f"/app/join/{tok}")
        check("an existing member is told they are already in",
              "already on this list" in r.text)
        r = N.post(f"/app/join/{tok}", follow_redirects=False)
        check("the right person can", r.status_code == 302)
        check("...and lands on the board",
              r.headers["location"] == f"/app/{ws_id}")
        r = N.post(f"/app/join/{tok}", follow_redirects=False)
        check("an accepted invitation is spent", r.status_code == 404)

        print("\n— somebody can always administer —")
        # The case this got wrong in production: an admin level arriving in a
        # database that already has accounts. "First account with no admin
        # around" hands the level to the NEXT arrival; only "the oldest account"
        # means what was intended.
        from models import ensure_one_admin
        db = SessionLocal()
        for u in db.query(User).all():
            u.is_admin = False
        db.commit()
        promoted = ensure_one_admin(db)
        db.commit()
        check("with no admin at all, the oldest account gets it",
              promoted == "owner@example.org", str(promoted))
        check("...and running it again changes nothing",
              ensure_one_admin(db) is None)
        db.close()

        print("\n— administration is a different axis, not a level above —")
        r = E.get("/admin", follow_redirects=False)
        check("a member cannot even see /admin", r.status_code == 404, str(r.status_code))
        r = O.get("/admin")
        check("an admin can", r.status_code == 200)
        check("...and the page says what it does not grant",
              "gives access to nobody" in r.text)
        # The line that matters: admin is not membership.
        r = O.get(f"/app/{ws_id}")
        stranger_ws = None
        db = SessionLocal()
        from models import Workspace as _W
        stranger_ws = (db.query(_W)
                         .filter(_W.owner_user_id ==
                                 db.query(User).filter(
                                     User.email == "stranger@example.org").first().id)
                         .first().id)
        db.close()
        # Make the stranger an admin and check they still cannot read the owner's board.
        db = SessionLocal()
        s_user = db.query(User).filter(User.email == "stranger@example.org").first()
        s_user.is_admin = True
        db.commit()
        db.close()
        r = S.get(f"/app/{ws_id}", follow_redirects=False)
        check("an admin still gets 404 on a board they are not a member of",
              r.status_code == 404, str(r.status_code))
        r = S.get("/admin")
        check("...while reaching /admin fine", r.status_code == 200)
        db = SessionLocal()
        s_user = db.query(User).filter(User.email == "stranger@example.org").first()
        s_user.is_admin = False
        db.commit()
        db.close()

        print("\n— the relay: written by one, used by all, read by none —")
        import settings as S_
        r = O.post("/admin/smtp", data={
            "smtp_enabled": "1", "smtp_host": "smtp.example.org", "smtp_port": "587",
            "smtp_security": "starttls", "smtp_username": "postmaster",
            "smtp_password": "hunter2", "smtp_from_email": "list@example.org",
            "smtp_from_name": "TheList"}, follow_redirects=False)
        check("an admin can save the relay", r.status_code == 302)
        db = SessionLocal()
        check("the password is stored encrypted, not in clear",
              "hunter2" not in S_.get(db, "smtp_password_enc")
              and S_.get(db, "smtp_password_enc") != "")
        check("...and the mailer can still read it",
              S_.smtp_config(db)["password"] == "hunter2")
        db.close()
        r = O.get("/admin")
        check("the form never sends the password back", "hunter2" not in r.text)
        r = E.get(f"/app/{ws_id}/settings")
        check("a member sees that mail is on, not what it is on",
              r.status_code == 200 and "hunter2" not in r.text
              and "smtp.example.org" not in r.text)

        r = O.post("/admin/smtp", data={
            "smtp_enabled": "1", "smtp_host": "smtp.example.org", "smtp_port": "587",
            "smtp_security": "starttls", "smtp_username": "postmaster",
            "smtp_password": "", "smtp_from_email": "list@example.org",
            "smtp_from_name": "TheList"}, follow_redirects=False)
        db = SessionLocal()
        check("a blank password box keeps the stored one",
              S_.smtp_config(db)["password"] == "hunter2")
        db.close()
        r = O.post("/admin/smtp/clear-password", follow_redirects=False)
        db = SessionLocal()
        check("clearing is its own button", S_.smtp_config(db)["password"] == "")
        db.close()

        r = O.post("/admin/smtp", data={"smtp_host": "x", "smtp_security": "carrier-pigeon"},
                   follow_redirects=False)
        check("an unknown transport security is refused", r.status_code == 400)

        print("\n— nobody can lock the app out of itself —")
        db = SessionLocal()
        admin_id = db.query(User).filter(User.email == "owner@example.org").first().id
        db.close()
        r = O.post(f"/admin/users/{admin_id}", data={"action": "demote"},
                   follow_redirects=False)
        check("the last admin cannot demote themselves", r.status_code == 400,
              str(r.status_code))
        r = O.post(f"/admin/users/{admin_id}", data={"action": "deactivate"},
                   follow_redirects=False)
        check("...nor deactivate themselves", r.status_code == 400)
        db = SessionLocal()
        eid = db.query(User).filter(User.email == "editor@example.org").first().id
        db.close()
        r = O.post(f"/admin/users/{eid}", data={"action": "promote"},
                   follow_redirects=False)
        check("promoting somebody else works", r.status_code == 302)
        r = O.post(f"/admin/users/{admin_id}", data={"action": "demote"},
                   follow_redirects=False)
        check("...and then stepping down is allowed", r.status_code == 302)
        db = SessionLocal()
        db.query(User).filter(User.email == "owner@example.org").first().is_admin = True
        db.query(User).filter(User.email == "editor@example.org").first().is_admin = False
        db.commit()
        db.close()

    # ── the MCP surface, called as its owner ──────────────────────────────────
    #
    # Called in-process with the caller set by hand, which is exactly what the
    # key gate does at the edge. The point of these checks is that the write
    # tools cannot reach anywhere the interface would not: same roles, same
    # rules, same soft delete.
    import auth
    import mcp_app as M

    db = SessionLocal()
    owner = db.query(User).filter(User.email == "owner@example.org").first()
    editor = db.query(User).filter(User.email == "editor@example.org").first()
    stranger = db.query(User).filter(User.email == "stranger@example.org").first()
    db.close()

    def as_(u):
        auth.set_caller(u)

    print("\n— MCP: what a board is —")
    as_(owner)
    boards = M.list_boards()["boards"]
    check("the owner sees one board", len(boards) == 1 and boards[0]["role"] == "owner")
    as_(stranger)
    check("a stranger cannot reach it by id",
          M.list_tasks(board=str(ws_id)).get("error") == "board not found")
    check("...nor by name",
          M.add_task(title="sneak", board="Spit's list").get("error") == "board not found")

    as_(editor)
    r = M.add_task(title="which board?")
    check("an editor with two boards is asked which, not told 'not found'",
          "more than one board" in r.get("error", ""), str(r))
    check("...and the answer names them", "Spit's list" in r.get("error", ""))

    print("\n— MCP: adding —")
    as_(owner)
    r = M.add_task(title="Read the BAG draft", for_person="Markus", tags="BAG",
                   effort="L", due="2026-09-30")
    check("the owner's task lands on the list", r.get("landed") == "on the list")
    check("...for the person named", r["task"]["for"] == "Markus")
    check("...with the tag and size given",
          r["task"]["tags"] == ["BAG"] and r["task"]["effort"] == "L")
    mcp_task = r["task"]["id"]
    r2 = M.add_task(title="Read the BAG draft")
    check("the same title twice is refused", "already has that exact title" in r2.get("error", ""))
    r3 = M.add_task(title="Read the BAG draft", allow_duplicate=True)
    check("...unless you say so", r3.get("landed") == "on the list")
    M.delete_task(task_id=r3["task"]["id"])

    r = M.add_task(title="bad size", effort="XL")
    check("a size outside S/M/L is refused", "effort must be" in r.get("error", ""))
    r = M.add_task(title="bad date", due="30/09/2026")
    check("a non-ISO date is refused", "ISO" in r.get("error", ""))

    as_(editor)
    r = M.add_task(title="Ask the ministry again", board=str(ws_id))
    check("an editor's task lands as a proposal",
          r.get("landed") == "in the owner's proposals")
    check("...for the proposer, deduced", r["task"]["for"] == "Nikola")
    mcp_prop = r["task"]["id"]

    print("\n— MCP: the roles hold —")
    r = M.update_task(task_id=mcp_task, board=str(ws_id), title="hijacked")
    check("an editor cannot edit", "only the owner" in r.get("error", ""))
    r = M.delete_task(task_id=mcp_task, board=str(ws_id))
    check("an editor cannot delete", "only the owner" in r.get("error", ""))
    r = M.accept_proposal(task_id=mcp_prop, board=str(ws_id))
    check("an editor cannot accept", "only the owner" in r.get("error", ""))
    r = M.add_note(task_id=mcp_task, board=str(ws_id), body="looking at it now")
    check("an editor can note", r.get("added") is True)
    r = M.add_note(task_id=mcp_task, board=str(ws_id), body="   ")
    check("an empty note is refused", "not a note" in r.get("error", ""))

    print("\n— MCP: deciding —")
    as_(owner)
    r = M.decline_proposal(task_id=mcp_prop, reason="")
    check("declining still needs a reason", "needs a reason" in r.get("error", ""))
    r = M.decline_proposal(task_id=mcp_prop, reason="not this quarter")
    check("...and keeps it with the reason",
          r.get("declined") and r["reason"] == "not this quarter")
    r = M.accept_proposal(task_id=mcp_prop, board=str(ws_id))
    check("a decided proposal cannot then be accepted",
          "not an open proposal" in r.get("error", ""))

    print("\n— MCP: editing and clearing —")
    r = M.update_task(task_id=mcp_task, recurring="yes", clear="due")
    check("recurring can be turned on", r["task"]["recurring"] is True)
    check("...and the due date cleared", r["task"]["due"] is None)
    r = M.update_task(task_id=mcp_task, recurring="maybe")
    check("recurring takes yes or no", "yes" in r.get("error", ""))
    r = M.update_task(task_id=mcp_task, tags="BAG, DEH")
    check("tags replace the whole set", sorted(r["task"]["tags"]) == ["BAG", "DEH"])

    print("\n— MCP: completing means two different things —")
    r = M.complete_task(task_id=mcp_task)
    check("a recurring task stays on the list",
          r["outcome"].startswith("recorded") and r["task"]["status"] == "active")
    check("...and its date is recorded", r["task"]["last_done"] is not None)
    r = M.add_task(title="A one-off from the chat", effort="S")
    one = r["task"]["id"]
    r = M.complete_task(task_id=one)
    check("a one-off is archived", r["outcome"] == "archived")
    r = M.complete_task(task_id=one)
    check("...and cannot be completed twice", "only something on the list" in r.get("error", ""))
    r = M.reopen_task(task_id=one)
    check("reopening puts it back", r["task"]["status"] == "active")

    print("\n— MCP: moving —")
    r = M.move_task(task_id=one, to_top=True)
    check("to_top puts it first", r["order"][0]["id"] == one)
    r = M.move_task(task_id=one, after_task_id=one)
    check("a task cannot go after itself", "after itself" in r.get("error", ""))
    r = M.move_task(task_id=one, after_task_id=999999)
    check("an unknown anchor is refused", "not on this list" in r.get("error", ""))
    r = M.move_task(task_id=one)
    check("with no anchor it goes last", r["order"][-1]["id"] == one)

    print("\n— MCP: deletion stays soft —")
    r = M.delete_task(task_id=one)
    check("delete says it is recoverable", r.get("recoverable") is True)
    check("...and it is gone from the list",
          one not in [t["id"] for t in M.list_tasks()["tasks"]])
    r = M.restore_task(task_id=one)
    check("restore brings it back", r["task"]["status"] == "active")
    as_(None)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failing: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main_())
