"""HTTP routes: auth."""
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, make_response, send_from_directory, abort, session
from flask_login import login_required, login_user, logout_user, current_user
from datetime import datetime, date, timedelta
from sqlalchemy import func, case, text, or_, and_, exists, not_
from types import SimpleNamespace
from decimal import Decimal, ROUND_HALF_UP
import os, io, json, re, logging, calendar, zipfile

from werkzeug.security import check_password_hash, generate_password_hash

from models import *
from app.services.api import *  # noqa
from utils.audit import audit_log

bp = Blueprint('auth', __name__)

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        username_norm = username.lower()
        password = str(request.form.get('password') or '')
        remember = (request.form.get('remember_me') or '').lower() in ('1', 'true', 'on', 'yes')
        user = User.query.filter(func.lower(func.trim(User.username)) == username_norm).order_by(User.id.asc()).first()

        def _verify_and_upgrade_password(u, raw_password):
            raw_password = str(raw_password or '')
            if not u or not raw_password:
                return False

            stored_hash = (getattr(u, 'password_hash', None) or '').strip()
            stored_plain = (getattr(u, 'password_plain', None) or '').strip()

            # 1) Normal Werkzeug hash verification.
            if stored_hash:
                try:
                    if check_password_hash(stored_hash, raw_password):
                        # A successful hash login no longer needs the legacy
                        # plaintext fallback. Clear it without logging either
                        # value; a commit failure must not lock the user out.
                        if stored_plain:
                            try:
                                u.password_plain = None
                                db.session.commit()
                            except Exception:
                                db.session.rollback()
                        return True
                except Exception:
                    # Some legacy DBs stored plaintext in password_hash; fall back below.
                    pass

            # 2) Legacy plaintext fallbacks (upgrade on success).
            legacy_match = False
            if stored_plain and stored_plain == raw_password:
                legacy_match = True
            elif stored_hash and stored_hash == raw_password:
                legacy_match = True

            if legacy_match:
                try:
                    u.password_hash = generate_password_hash(raw_password)
                    u.password_plain = None
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return True

            return False

        if user and _verify_and_upgrade_password(user, password):
            if (user.status or '').strip().lower() != 'active':
                flash('Account suspended', 'danger')
                return render_template('login.html')
            # Remember by default so LAN HTTP sessions survive the post-login redirect.
            login_user(user, remember=True)
            session.permanent = True
            session['role'] = user.role
            from utils.sessions import open_login_session
            open_login_session(user)
            next_url = request.args.get('next') or ''
            if not next_url.startswith('/') or next_url.startswith('//'):
                next_url = ''
            return redirect(next_url or url_for('index'))
        try:
            logging.getLogger('auth').info(
                "Login failed username=%s exists=%s has_hash=%s",
                username,
                bool(user),
                bool(getattr(user, 'password_hash', None))
            )
        except Exception:
            pass
        flash('Invalid Credentials', 'danger')
    return render_template('login.html')


@bp.route('/root/recovery', methods=['GET', 'POST'])
def root_recovery():
    require_root()
    root_username = os.environ.get('ROOT_USERNAME', 'root')
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        recovery_code = (request.form.get('recovery_code') or '').strip().upper()
        new_password = str(request.form.get('new_password') or '')
        confirm_password = str(request.form.get('confirm_password') or '')

        if username != root_username:
            flash('Recovery failed. Invalid credentials.', 'danger')
            return render_template('root_recovery.html', root_username=root_username)
        if not recovery_code or not new_password:
            flash('Recovery code and new password are required.', 'danger')
            return render_template('root_recovery.html', root_username=root_username)
        if len(new_password) < 8:
            flash('New password must be at least 8 characters.', 'danger')
            return render_template('root_recovery.html', root_username=root_username)
        if new_password != confirm_password:
            flash('Password confirmation does not match.', 'danger')
            return render_template('root_recovery.html', root_username=root_username)

        root_user = User.query.filter_by(username=root_username).first()
        if not root_user:
            flash('Root account not found.', 'danger')
            return render_template('root_recovery.html', root_username=root_username)

        hit = _consume_root_recovery_code(root_username, recovery_code)
        if not hit:
            flash('Invalid or already used recovery code.', 'danger')
            return render_template('root_recovery.html', root_username=root_username)

        root_user.password_hash = generate_password_hash(new_password)
        root_user.password_plain = None
        db.session.commit()
        flash('Root password reset successful. Please login with new password.', 'success')
        return redirect(url_for('login'))

    return render_template('root_recovery.html', root_username=root_username)


@bp.route('/root/recovery_codes', methods=['GET', 'POST'])
@login_required
def root_recovery_codes():
    require_root()
    root_username = os.environ.get('ROOT_USERNAME', 'root')
    generated_codes = []

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'generate':
            note = (request.form.get('note') or '').strip()
            generated_codes = _create_root_recovery_codes(username=root_username, count=10, note=note)
            db.session.commit()
            audit_log(current_user, None, 'root.recovery_codes.generate', f'count=10 username={root_username}')
            flash('New offline recovery codes generated. Save them now; they will not be shown again.', 'success')
        elif action == 'revoke_unused':
            deleted = RootRecoveryCode.query.filter(
                RootRecoveryCode.username == root_username,
                RootRecoveryCode.used_at.is_(None)
            ).delete(synchronize_session=False)
            db.session.commit()
            audit_log(current_user, None, 'root.recovery_codes.revoke', f'deleted={deleted} username={root_username}')
            flash(f'Revoked {deleted} unused recovery codes.', 'warning')

    codes = RootRecoveryCode.query.filter(
        RootRecoveryCode.username == root_username
    ).order_by(RootRecoveryCode.created_at.desc()).all()
    unused_count = sum(1 for c in codes if c.used_at is None)
    used_count = sum(1 for c in codes if c.used_at is not None)

    return render_template(
        'root_recovery_codes.html',
        root_username=root_username,
        generated_codes=generated_codes,
        unused_count=unused_count,
        used_count=used_count,
        codes=codes
    )


@bp.route('/logout')
@login_required
def logout():
    from utils.sessions import close_login_session
    close_login_session(current_user)
    logout_user()
    session.pop('role', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


@bp.route('/root/backup-settings')
@login_required
def root_backup_settings():
    require_root()
    row = _get_or_create_root_backup_settings()
    history_rows = RootBackupEmailHistory.query.order_by(RootBackupEmailHistory.created_at.desc()).limit(200).all()
    return render_template('root_backup_settings.html', row=row, history_rows=history_rows)


@bp.route('/root/backup-settings/save', methods=['POST'])
@login_required
def root_backup_settings_save():
    require_root()
    row = _get_or_create_root_backup_settings()

    row.enabled = ('enabled' in request.form)
    row.frequency = 'hourly'
    row.recipient_emails = (request.form.get('recipient_emails') or '').strip()
    row.include_full_raw_xlsx = ('include_full_raw_xlsx' in request.form)
    row.include_sqlite_db = ('include_sqlite_db' in request.form)
    row.subject_prefix = (request.form.get('subject_prefix') or 'PWARE Root Backup').strip() or 'PWARE Root Backup'
    try:
        keep_count = int(request.form.get('keep_history_count') or row.keep_history_count or 200)
    except Exception:
        keep_count = 200
    row.keep_history_count = max(10, min(5000, keep_count))

    if not row.include_full_raw_xlsx and not row.include_sqlite_db:
        flash('Select at least one backup payload (XLSX or DB).', 'danger')
        return redirect(url_for('root_backup_settings'))

    db.session.commit()
    _cleanup_root_backup_history(row.keep_history_count)
    flash('Root backup settings updated.', 'success')
    return redirect(url_for('root_backup_settings'))


@bp.route('/root/backup-settings/send-now', methods=['POST'])
@login_required
def root_backup_settings_send_now():
    require_root()
    ok, msg = _send_hourly_all_tenants_backup_email(trigger_type='manual-send-now', force_send=True)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('root_backup_settings'))


@bp.route('/root/backup-settings/history/download/<int:history_id>')
@login_required
def root_backup_settings_history_download(history_id):
    require_root()
    row = db.session.get(RootBackupEmailHistory, history_id)
    if not row:
        flash('History record not found.', 'danger')
        return redirect(url_for('root_backup_settings'))
    fpath = (row.backup_path or '').strip()
    if not fpath or not os.path.exists(fpath):
        flash('Backup ZIP not found on disk.', 'danger')
        return redirect(url_for('root_backup_settings'))
    if os.path.isdir(fpath):
        # Official backups are private directories. Build the legacy download
        # in memory so no second persistent ZIP can accumulate.
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
            for root, _, names in os.walk(fpath):
                for name in sorted(names):
                    item = os.path.join(root, name)
                    archive.write(item, os.path.relpath(item, fpath))
        payload.seek(0)
        download = payload
    else:
        download = fpath
    return send_file(
        download,
        as_attachment=True,
        download_name=_download_filename('ROOTBACKUP', 'zip'),
        mimetype='application/zip'
    )


@bp.route('/root/backup-settings/history/clear', methods=['POST'])
@login_required
def root_backup_settings_history_clear():
    require_root()
    rows = RootBackupEmailHistory.query.all()
    removed = 0
    for r in rows:
        fpath = (r.backup_path or '').strip()
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass
        db.session.delete(r)
        removed += 1
    db.session.commit()
    flash(f'Cleared root backup history ({removed} record(s)).', 'success')
    return redirect(url_for('root_backup_settings'))


