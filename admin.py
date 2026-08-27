"""
The way back in, from a shell.

/admin can lock itself: the last administrator cannot be demoted or deactivated
through the interface, which is the right rule right up until the account is
gone for another reason — somebody left, an address changed at the gate, a
profile was created twice. This script is the answer to that, and the reason the
web guard can afford to be strict.

    docker exec thelist python admin.py --list
    docker exec thelist python admin.py --promote someone@example.org
    docker exec thelist python admin.py --demote someone@example.org

It changes only the administration flag. It cannot read a board, and it cannot
read the SMTP password — that is Fernet-encrypted and this script has no reason
to touch it.
"""
import argparse
import sys

from models import SessionLocal, User, init_db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="who exists, and their level")
    ap.add_argument("--promote", metavar="EMAIL", help="make this account an administrator")
    ap.add_argument("--demote", metavar="EMAIL", help="take the level away")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.list or not (args.promote or args.demote):
            rows = db.query(User).order_by(User.email).all()
            if not rows:
                print("no accounts yet — the first one to arrive becomes admin")
            for u in rows:
                level = "admin " if u.is_admin else "member"
                state = "" if u.is_active else "  (deactivated)"
                print(f"  {level}  {u.email}{state}")
            return 0

        email = (args.promote or args.demote).strip().lower()
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            print(f"no account for {email}")
            return 1

        if args.promote:
            if user.is_admin:
                print(f"{email} is already an administrator")
                return 0
            user.is_admin = True
        else:
            others = (db.query(User)
                        .filter(User.is_admin == True, User.is_active == True,  # noqa: E712
                                User.id != user.id).count())
            if others == 0:
                print(f"{email} is the last administrator; promote somebody else first")
                return 1
            user.is_admin = False
        db.commit()
        print(f"{email}: admin={user.is_admin}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
