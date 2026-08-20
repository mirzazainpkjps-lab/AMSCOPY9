#!/usr/bin/env python
"""List user account state without exposing credential material."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from main import app  # noqa: E402
from models import db, User  # noqa: E402


def main() -> int:
    with app.app_context():
        users = User.query.order_by(User.id.asc()).all()
        print(f"Total users: {len(users)}")
        print("")
        print("id\tusername\trole\tstatus\thash_configured\tlegacy_plaintext_present")
        for user in users:
            hash_configured = bool((user.password_hash or "").strip())
            plaintext_present = bool((user.password_plain or "").strip())
            print(
                f"{user.id}\t{user.username}\t{user.role}\t{user.status}\t"
                f"{str(hash_configured).lower()}\t{str(plaintext_present).lower()}"
            )
        db.session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
