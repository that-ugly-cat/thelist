"""
Create the first account, once, from the environment.

Only meaningful in `local` mode: in `gateway` mode the first person to arrive
through the gate gets a profile and a board automatically. Idempotent — running
it twice reports the existing account and changes nothing.

    docker exec thelist python seed.py
"""
import os
import sys

from auth import hash_password
from models import SessionLocal, User, ensure_workspace, init_db


def main() -> int:
    email = (os.environ.get("ADMIN_EMAIL", "") or "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    name = (os.environ.get("ADMIN_NAME", "") or "").strip()

    if not email or not password:
        print("ADMIN_EMAIL and ADMIN_PASSWORD are needed in the environment.")
        return 1

    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is not None:
            print(f"already there: {email} (nothing changed)")
        else:
            user = User(email=email, name=name or email,
                        hashed_password=hash_password(password),
                        is_active=True, is_admin=True)
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"created: {email}")
        ws = ensure_workspace(db, user)
        db.commit()
        print(f"board:   {ws.name} (id {ws.id})")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
