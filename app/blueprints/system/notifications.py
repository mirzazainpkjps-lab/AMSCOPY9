"""notifications — split from system.py."""
from ._common import *  # noqa

@bp.route('/notifications')
@login_required
def notifications_page():
    category = request.args.get('category', 'all').strip().lower()
    status = request.args.get('status', 'all').strip().lower()
    risk = request.args.get('risk', 'all').strip().lower()
    q = request.args.get('q', '').strip()

    rows = _build_notification_rows(category_filter=category, status_filter=status, risk_filter=risk, q=q)
    reminders = FollowUpReminder.query.filter_by(is_done=False).order_by(FollowUpReminder.remind_at.asc()).all()
    contacts = FollowUpContact.query.order_by(FollowUpContact.contacted_at.desc(), FollowUpContact.id.desc()).all()
    reminder_by_bill = {}
    contact_count_by_bill = {}
    latest_contact_by_bill = {}
    for rem in reminders:
        if rem.pending_bill_id not in reminder_by_bill:
            reminder_by_bill[rem.pending_bill_id] = rem
    for c in contacts:
        bill_id = c.pending_bill_id
        contact_count_by_bill[bill_id] = contact_count_by_bill.get(bill_id, 0) + 1
        if bill_id not in latest_contact_by_bill:
            latest_contact_by_bill[bill_id] = c
    for row in rows:
        bill_id = row['bill'].id
        rem = reminder_by_bill.get(row['bill'].id)
        c = latest_contact_by_bill.get(bill_id)
        row['active_remind_at'] = rem.remind_at if rem else None
        row['active_note'] = rem.note if rem else ''
        row['active_reminder_id'] = rem.id if rem else None
        row['contact_count'] = max(row.get('contact_count', 0), contact_count_by_bill.get(bill_id, 0))
        row['last_contact_at'] = c.contacted_at if c else None
        row['last_contact_channel'] = c.channel if c else ''
        row['last_contact_response'] = c.response if c else ''
        row['last_contact_note'] = c.note if c else ''
    staff_emails = StaffEmail.query.order_by(StaffEmail.email.asc()).all()

    counts = {
        'total': len(rows),
        'very_high': sum(1 for r in rows if _normalize_risk_label(r['risk_level']) == 'very_high'),
        'high': sum(1 for r in rows if r['risk_level'] == 'High'),
        'medium': sum(1 for r in rows if r['risk_level'] == 'Medium'),
        'low': sum(1 for r in rows if r['risk_level'] == 'Low'),
        'pending': sum(1 for r in rows if r['status'] == 'Pending'),
    }

    return render_template(
        'notifications.html',
        rows=rows,
        reminders=reminders,
        staff_emails=staff_emails,
        counts=counts,
        filters={'category': category, 'status': status, 'risk': risk, 'q': q}
    )


@bp.route('/notifications/upcoming')
@login_required
def notifications_upcoming():
    reminders = FollowUpReminder.query.filter_by(is_done=False).order_by(FollowUpReminder.remind_at.asc()).all()
    now = pk_now()
    return render_template('notifications_upcoming.html', reminders=reminders, now=now)


@bp.route('/notifications/add_email', methods=['POST'])
@login_required
def notifications_add_email():
    email = (request.form.get('email') or '').strip().lower()
    if not email or '@' not in email:
        flash('Valid email required', 'danger')
        return redirect(url_for('notifications_page'))
    exists = StaffEmail.query.filter(func.lower(StaffEmail.email) == email).first()
    if not exists:
        db.session.add(StaffEmail(email=email, is_active=True))
        db.session.commit()
        flash('Staff email added', 'success')
    else:
        flash('Email already exists', 'warning')
    return redirect(url_for('notifications_page'))


@bp.route('/notifications/toggle_email/<int:id>', methods=['POST'])
@login_required
def notifications_toggle_email(id):
    rec = db.session.get(StaffEmail, id)
    if rec:
        rec.is_active = not rec.is_active
        db.session.commit()
    return redirect(url_for('notifications_page'))


@bp.route('/notifications/delete_email/<int:id>', methods=['POST'])
@login_required
def notifications_delete_email(id):
    rec = db.session.get(StaffEmail, id)
    if rec:
        db.session.delete(rec)
        db.session.commit()
    return redirect(url_for('notifications_page'))


@bp.route('/notifications/set_reminder/<int:bill_id>', methods=['POST'])
@login_required
def notifications_set_reminder(bill_id):
    pb = db.session.get(PendingBill, bill_id)
    if not pb:
        flash('Pending bill not found', 'danger')
        return redirect(url_for('notifications_page'))
    remind_at_txt = (request.form.get('remind_at') or '').strip()
    note = (request.form.get('note') or '').strip()
    remind_at = _parse_dt_safe(remind_at_txt)
    if not remind_at:
        flash('Invalid reminder date/time', 'danger')
        return redirect(url_for('notifications_page'))

    # One active reminder per bill; overwrite old one.
    existing = FollowUpReminder.query.filter_by(pending_bill_id=pb.id, is_done=False).first()
    if existing:
        existing.remind_at = remind_at
        existing.note = note
        existing.alerted_at = None
        existing.acknowledged_at = None
    else:
        db.session.add(FollowUpReminder(pending_bill_id=pb.id, remind_at=remind_at, note=note))
    db.session.commit()
    flash('Reminder saved', 'success')
    return redirect(url_for('notifications_page'))


@bp.route('/notifications/log_contact/<int:bill_id>', methods=['POST'])
@login_required
def notifications_log_contact(bill_id):
    pb = db.session.get(PendingBill, bill_id)
    if not pb:
        flash('Pending bill not found', 'danger')
        return redirect(url_for('notifications_page'))

    contacted_at_txt = (request.form.get('contacted_at') or '').strip()
    contacted_at = _parse_dt_safe(contacted_at_txt) if contacted_at_txt else pk_now()
    if not contacted_at:
        flash('Invalid contact date/time', 'danger')
        return redirect(request.referrer or url_for('notifications_page'))

    channel = (request.form.get('channel') or 'Call').strip()
    if channel not in ['Call', 'WhatsApp', 'SMS', 'Email', 'Visit', 'Other']:
        channel = 'Other'

    response_text = (request.form.get('response') or '').strip()
    if not response_text:
        flash('Customer response is required to save history', 'danger')
        return redirect(request.referrer or url_for('notifications_page'))
    note = (request.form.get('note') or '').strip()

    db.session.add(FollowUpContact(
        pending_bill_id=pb.id,
        contacted_at=contacted_at,
        channel=channel,
        response=response_text[:200],
        note=note[:500],
        created_by=(current_user.username if current_user.is_authenticated else '')
    ))
    db.session.commit()
    flash('Contact history saved', 'success')
    return redirect(request.referrer or url_for('notifications_page'))


@bp.route('/notifications/close_reminder/<int:id>', methods=['POST'])
@login_required
def notifications_close_reminder(id):
    rem = db.session.get(FollowUpReminder, id)
    response_text = (request.form.get('response') or '').strip()
    channel = (request.form.get('channel') or 'Call').strip()
    note = (request.form.get('note') or '').strip()
    contacted_at_txt = (request.form.get('contacted_at') or '').strip()
    contacted_at = _parse_dt_safe(contacted_at_txt) if contacted_at_txt else pk_now()
    if not contacted_at:
        contacted_at = pk_now()

    ok, msg = _resolve_reminder_with_contact(
        rem=rem,
        response_text=response_text,
        channel=channel,
        note=note,
        contacted_at=contacted_at,
        created_by=(current_user.username if current_user.is_authenticated else '')
    )
    flash(msg, 'success' if ok else 'danger')
    return redirect(request.referrer or url_for('notifications_page'))


@bp.route('/notifications/set_severity/<int:bill_id>', methods=['POST'])
@login_required
def notifications_set_severity(bill_id):
    pb = db.session.get(PendingBill, bill_id)
    if not pb:
        flash('Pending bill not found', 'danger')
        return redirect(url_for('notifications_page'))

    level = _normalize_risk_label(request.form.get('severity'))
    valid = {'auto', 'low', 'medium', 'high', 'very_high'}
    if level not in valid:
        flash('Invalid severity selection', 'danger')
        return redirect(request.referrer or url_for('notifications_bill_detail', bill_id=bill_id))

    pb.risk_override = None if level == 'auto' else _risk_label_pretty(level)
    db.session.commit()
    flash('Severity updated', 'success')
    return redirect(request.referrer or url_for('notifications_bill_detail', bill_id=bill_id))


@bp.route('/notifications/bill/<int:bill_id>')
@login_required
def notifications_bill_detail(bill_id):
    pb = db.session.get(PendingBill, bill_id)
    if not pb:
        flash('Pending bill not found', 'danger')
        return redirect(url_for('notifications_page'))

    age_days = _pending_bill_age_days(pb)
    category = _pending_bill_category(pb)
    # Load all open credit bills for this same client on detail screen.
    if (pb.client_code or '').strip():
        client_bills_query = PendingBill.query.filter(
            PendingBill.is_void == False,
            PendingBill.is_paid == False,
            PendingBill.is_cash == False,
            PendingBill.amount > 0,
            PendingBill.client_code == pb.client_code
        )
    else:
        client_name_norm = (pb.client_name or '').strip().lower()
        client_bills_query = PendingBill.query.filter(
            PendingBill.is_void == False,
            PendingBill.is_paid == False,
            PendingBill.is_cash == False,
            PendingBill.amount > 0,
            func.lower(func.trim(PendingBill.client_name)) == client_name_norm
        )
    client_open_bills = client_bills_query.order_by(PendingBill.id.desc()).all()
    client_bill_ids = [b.id for b in client_open_bills]
    client_total_due = sum(float(b.amount or 0) for b in client_open_bills)

    active_reminder = FollowUpReminder.query.filter_by(
        pending_bill_id=pb.id,
        is_done=False
    ).order_by(FollowUpReminder.remind_at.asc()).first()
    reminders = FollowUpReminder.query.filter(
        FollowUpReminder.pending_bill_id.in_(client_bill_ids)
    ).order_by(FollowUpReminder.created_at.desc(), FollowUpReminder.id.desc()).all() if client_bill_ids else []
    contact_logs = FollowUpContact.query.filter(
        FollowUpContact.pending_bill_id.in_(client_bill_ids)
    ).order_by(FollowUpContact.contacted_at.desc(), FollowUpContact.id.desc()).all() if client_bill_ids else []
    score, risk_level = _pending_bill_risk(pb, contact_count=len(contact_logs))

    reminder_ids = [r.id for r in reminders]
    reminder_contact_by_id = {}
    used_contact_ids = set()
    if reminder_ids:
        closure_contacts = FollowUpContact.query.filter(
            FollowUpContact.reminder_id.in_(reminder_ids)
        ).order_by(FollowUpContact.contacted_at.desc(), FollowUpContact.id.desc()).all()
        for c in closure_contacts:
            if c.reminder_id and c.reminder_id not in reminder_contact_by_id:
                reminder_contact_by_id[c.reminder_id] = c
                used_contact_ids.add(c.id)

    # Backfill matching for older rows created before reminder_id linkage existed.
    for r in reminders:
        if r.id in reminder_contact_by_id:
            continue
        if not r.is_done or not r.acknowledged_at:
            continue
        best = None
        best_diff = None
        for c in contact_logs:
            if c.id in used_contact_ids:
                continue
            if c.reminder_id:
                continue
            if not c.contacted_at:
                continue
            diff = abs((c.contacted_at - r.acknowledged_at).total_seconds())
            if diff <= 180 and (best_diff is None or diff < best_diff):
                best = c
                best_diff = diff
        if best:
            reminder_contact_by_id[r.id] = best
            used_contact_ids.add(best.id)

    additional_contacts = [c for c in contact_logs if c.id not in used_contact_ids]

    return render_template(
        'notifications_detail.html',
        bill=pb,
        client_open_bills=client_open_bills,
        client_total_due=client_total_due,
        score=score,
        risk_level=risk_level,
        age_days=age_days,
        category=category,
        active_reminder=active_reminder,
        reminders=reminders,
        contact_logs=contact_logs,
        reminder_contact_by_id=reminder_contact_by_id,
        additional_contacts=additional_contacts,
        severity_override=(_normalize_risk_label(pb.risk_override) if pb.risk_override else 'auto')
    )


@bp.route('/notifications/ack_reminder/<int:id>', methods=['POST'])
@login_required
def notifications_ack_reminder(id):
    rem = db.session.get(FollowUpReminder, id)
    response_text = (request.form.get('response') or '').strip()
    channel = (request.form.get('channel') or 'Call').strip()
    note = (request.form.get('note') or '').strip()
    contacted_at_txt = (request.form.get('contacted_at') or '').strip()
    contacted_at = _parse_dt_safe(contacted_at_txt) if contacted_at_txt else pk_now()
    if not contacted_at:
        contacted_at = pk_now()

    ok, msg = _resolve_reminder_with_contact(
        rem=rem,
        response_text=response_text,
        channel=channel,
        note=note,
        contacted_at=contacted_at,
        created_by=(current_user.username if current_user.is_authenticated else '')
    )
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400 if msg == 'Customer response is required' else 404
    return jsonify({'success': True})



