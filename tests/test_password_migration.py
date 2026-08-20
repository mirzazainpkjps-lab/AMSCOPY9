"""Password migration must remove plaintext without changing credentials."""
from __future__ import annotations

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from models import User, db


@pytest.fixture()
def password_app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'passwords.db'}",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "password-migration-test",
            "SESSION_COOKIE_SECURE": False,
        }
    )
    with application.app_context():
        yield application
        db.session.remove()


def test_successful_hash_login_clears_redundant_plaintext(password_app):
    with password_app.app_context():
        original_hash = generate_password_hash("correct-value")
        user = User(
            username="Hash User",
            password_hash=original_hash,
            password_plain="correct-value",
            role="user",
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    response = password_app.test_client().post(
        "/login",
        data={"username": "Hash User", "password": "correct-value"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    with password_app.app_context():
        migrated = db.session.get(User, user_id)
        assert migrated.password_plain is None
        assert migrated.password_hash == original_hash
        assert check_password_hash(migrated.password_hash, "correct-value")


def test_plaintext_only_login_hashes_and_clears_atomically(password_app):
    with password_app.app_context():
        user = User(
            username="Legacy User",
            password_hash=None,
            password_plain="legacy-value",
            role="user",
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    response = password_app.test_client().post(
        "/login",
        data={"username": "Legacy User", "password": "legacy-value"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    with password_app.app_context():
        migrated = db.session.get(User, user_id)
        assert migrated.password_plain is None
        assert migrated.password_hash != "legacy-value"
        assert check_password_hash(migrated.password_hash, "legacy-value")
