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
os.environ["SMTP_ENABLED"] = "false"

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

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failing: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main_())
