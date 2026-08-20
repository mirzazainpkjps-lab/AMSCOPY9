#!/usr/bin/env python
"""Reset User account passwords and roles.

The reset credential must be supplied through ``ACCOUNT_RESET_PASSWORD`` and
is never printed or stored in the legacy plaintext column.

Usage:
    ACCOUNT_RESET_PASSWORD=... python tools/repair_controlled/fix_accounts_and_test.py --confirm
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools.repair_controlled.repair_guard import preflight
preflight(
    script_name=__file__,
    description="Reset selected account passwords/roles and deactivate other accounts",
)

from werkzeug.security import generate_password_hash  # noqa: E402

from main import app  # noqa: E402
from models import db, User  # noqa: E402


ALLOWED = {
    # desired_username: role
    "Rehman Ahmed": "admin",
    "Rizwan Ahmed": "user",
    "Adnan Ahmed": "user",
    "Shujaat Muzaffar": "user",
    "Admin": "admin",
}


def _login_ok(client, username: str, password: str) -> bool:
    resp = client.post(
        "/login",
        data={"username": username, "password": password, "remember_me": "0"},
        follow_redirects=False,
    )
    if resp.status_code not in (301, 302, 303, 307, 308):
        return False
    loc = resp.headers.get("Location") or ""
    return ("/login" not in loc) and ("/root/recovery" not in loc)


def main() -> int:
    reset_password = os.environ.get("ACCOUNT_RESET_PASSWORD", "")
    if len(reset_password) < 12:
        print("ACCOUNT_RESET_PASSWORD must be set to at least 12 characters.", file=sys.stderr)
        return 2

    with app.app_context():
        users = User.query.order_by(User.id.asc()).all()

        # Preserve account rows and their historical references. Accounts outside
        # the selected set are deactivated rather than hard-deleted.
        for user in users:
            if user.username not in ALLOWED:
                user.status = "inactive"

        db.session.flush()

        # Ensure/normalize the allowed accounts (create missing ones).
        for desired_username, role in ALLOWED.items():
            u = User.query.filter_by(username=desired_username).order_by(User.id.asc()).first()
            if not u:
                u = User(username=desired_username, role=role, status="active")
                db.session.add(u)

            u.username = desired_username
            u.role = role
            u.status = "active"

            u.password_hash = generate_password_hash(reset_password)
            u.password_plain = None

        db.session.commit()

        # Test logins
        client = app.test_client()
        results = {}
        for username in ALLOWED.keys():
            results[username] = _login_ok(client, username, reset_password)
            client.get("/logout")

        print("Login test results:")
        for username, ok in results.items():
            print(f"- {username}: {'OK' if ok else 'FAIL'}")

        bad = [u for u, ok in results.items() if not ok]
        if bad:
            print("")
            print("One or more logins failed:", ", ".join(bad))
            return 2

        print("")
        print("Account reset completed; credential material was not emitted or stored in plaintext.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
