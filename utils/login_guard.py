"""Brute-force protection for the login endpoint.

Deliberately dependency-free (no flask-limiter) and in-process: this app is
deployed as a single Flask process, and an in-memory sliding window costs
nothing and cannot fail closed against a missing Redis.

Two independent counters are tracked so neither axis alone defeats the guard:

* per-username  -- stops credential stuffing against one known account from
                   a rotating pool of source addresses.
* per-IP        -- stops one host spraying many usernames.

Both use a sliding window; a lockout simply means "too many failures inside
the window", and it decays on its own without any scheduled cleanup.

NOTE: state is per-process. If this app is ever run under multiple workers
(gunicorn -w N), move this to a shared store or the effective limit becomes
N x MAX_ATTEMPTS.
"""
from __future__ import annotations

import os
import threading
import time

# Tunable via environment so operators can tighten/loosen without a code change.
MAX_ATTEMPTS = int(os.environ.get('AMS_LOGIN_MAX_ATTEMPTS', '8'))
WINDOW_SECONDS = int(os.environ.get('AMS_LOGIN_WINDOW_SECONDS', '300'))
LOCKOUT_SECONDS = int(os.environ.get('AMS_LOGIN_LOCKOUT_SECONDS', '900'))

# Keep memory bounded even under a sustained distributed attack.
_MAX_TRACKED_KEYS = 4096

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def _prune(now: float) -> None:
    """Drop expired entries. Caller must hold the lock."""
    cutoff = now - WINDOW_SECONDS
    for key in list(_failures):
        kept = [t for t in _failures[key] if t > cutoff]
        if kept:
            _failures[key] = kept
        else:
            _failures.pop(key, None)
    for key in list(_locked_until):
        if _locked_until[key] <= now:
            _locked_until.pop(key, None)

    # Hard cap: if we are still oversized, evict the oldest-activity keys.
    if len(_failures) > _MAX_TRACKED_KEYS:
        ordered = sorted(_failures.items(), key=lambda kv: max(kv[1]))
        for key, _ in ordered[: len(_failures) - _MAX_TRACKED_KEYS]:
            _failures.pop(key, None)


def _keys(username: str, ip: str) -> list[str]:
    keys = []
    uname = (username or '').strip().lower()
    if uname:
        keys.append(f'user:{uname}')
    if ip:
        keys.append(f'ip:{ip}')
    return keys


def client_ip() -> str:
    """Best-effort client address.

    Only trusts X-Forwarded-For when AMS_TRUST_PROXY is set, otherwise a
    client could spoof the header and reset its own counter at will.
    """
    from flask import request

    if os.environ.get('AMS_TRUST_PROXY', '').strip() in ('1', 'true', 'True'):
        fwd = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
        if fwd:
            return fwd
    return request.remote_addr or 'unknown'


def check(username: str, ip: str) -> int:
    """Return seconds remaining in a lockout, or 0 if the attempt may proceed."""
    now = time.time()
    with _lock:
        _prune(now)
        remaining = 0
        for key in _keys(username, ip):
            until = _locked_until.get(key, 0)
            if until > now:
                remaining = max(remaining, int(until - now))
        return remaining


def record_failure(username: str, ip: str) -> int:
    """Record a failed attempt. Returns seconds of lockout now in force (0 if none)."""
    now = time.time()
    with _lock:
        _prune(now)
        locked_for = 0
        for key in _keys(username, ip):
            attempts = _failures.setdefault(key, [])
            attempts.append(now)
            if len(attempts) >= MAX_ATTEMPTS:
                _locked_until[key] = now + LOCKOUT_SECONDS
                _failures.pop(key, None)
                locked_for = LOCKOUT_SECONDS
        return locked_for


def record_success(username: str, ip: str) -> None:
    """Clear counters for this identity after a genuine login."""
    now = time.time()
    with _lock:
        _prune(now)
        for key in _keys(username, ip):
            _failures.pop(key, None)
            _locked_until.pop(key, None)


def reset_all() -> None:
    """Test helper: forget all tracked state."""
    with _lock:
        _failures.clear()
        _locked_until.clear()
