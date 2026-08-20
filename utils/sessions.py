"""Concurrent login sessions (many users / many IPs at once)."""
from __future__ import annotations

import secrets
from datetime import timedelta

from flask import request, session
from sqlalchemy.orm import Session as SASession

from models import db, UserLoginSession
from utils.audit import audit_log


def _client_ip():
    forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return forwarded or (request.remote_addr or '')


def _ua():
    return (request.headers.get('User-Agent') or '')[:300]


def _now():
    try:
        from app.services.time_money import pk_now
        return pk_now()
    except Exception:
        from datetime import datetime
        return datetime.utcnow()


def open_login_session(user):
    """Record a new browser session. Never closes other users or other IPs."""
    sid = secrets.token_hex(16)
    session['ams_sid'] = sid
    session.permanent = True
    row = UserLoginSession(
        sid=sid,
        user_id=user.id,
        username=user.username,
        role=user.role,
        ip=_client_ip(),
        user_agent=_ua(),
    )
    try:
        bind = db.session.get_bind()
        with SASession(bind) as s:
            s.add(row)
            s.commit()
    except Exception:
        try:
            db.session.add(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
    audit_log(
        user,
        'auth.login',
        f'ip={_client_ip()} role={user.role} sid={sid} concurrent=allowed',
    )
    return sid


def touch_login_session(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return
    sid = session.get('ams_sid')
    if not sid:
        open_login_session(user)
        return
    import time
    now_ts = time.time()
    if now_ts - float(session.get('ams_sid_touch') or 0) < 60:
        return
    session['ams_sid_touch'] = now_ts
    try:
        bind = db.session.get_bind()
        with SASession(bind) as s:
            row = s.query(UserLoginSession).filter_by(sid=sid, ended_at=None).first()
            if not row:
                return
            row.last_seen_at = _now()
            row.ip = _client_ip() or row.ip
            s.commit()
    except Exception:
        pass


def close_login_session(user):
    sid = session.pop('ams_sid', None)
    now = _now()
    if sid:
        try:
            bind = db.session.get_bind()
            with SASession(bind) as s:
                row = s.query(UserLoginSession).filter_by(sid=sid).first()
                if row and row.ended_at is None:
                    row.ended_at = now
                    s.commit()
        except Exception:
            pass
    audit_log(user, 'auth.logout', f'ip={_client_ip()} sid={sid or "-"}')


def list_active_sessions(fresh_minutes=45):
    now = _now()
    cutoff = now - timedelta(minutes=fresh_minutes)
    rows = UserLoginSession.query.filter(
        UserLoginSession.ended_at.is_(None)
    ).order_by(UserLoginSession.last_seen_at.desc()).all()
    for r in rows:
        seen = r.last_seen_at or r.created_at
        r.is_live = bool(seen and seen >= cutoff)
    return rows
