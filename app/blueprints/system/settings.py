"""settings — split from system.py."""
from ._common import *  # noqa

@bp.route('/debug/db')
@login_required
def debug_db():
    if getattr(current_user, 'role', None) != 'admin':
        abort(403)
    try:
        st = os.stat(db_path) if os.path.exists(db_path) else None
        info = {
            'db_path': db_path,
            'db_exists': os.path.exists(db_path),
            'db_size_bytes': (st.st_size if st else None),
            'db_mtime': (datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S') if st else None),
            'counts': _db_debug_counts(),
        }
    except Exception as exc:
        info = {'error': str(exc)}
    return jsonify(info)


@bp.route('/settings')
@login_required
def settings():
    if current_user.role != 'admin' and not _user_can('can_access_settings'):
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))
    settings_obj = Settings.query.first()
    if not settings_obj:
        settings_obj = Settings()
    categories = MaterialCategory.query.order_by(MaterialCategory.name.asc()).all()
    recon_report = session.pop('recon_report', None)
    recent_audit = []
    try:
        recent_audit = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(20).all()
    except Exception:
        recent_audit = []
    # Single source of truth for the user-role UI: feature groups + the
    # permission value of every user, so the checkboxes and the access
    # matrix always show exactly what exists and who has it.
    from app.services.constants import (
        EDITABLE_USER_PERMISSION_FIELDS,
        PERMISSION_GROUPS,
        USER_PERMISSION_DEFAULTS,
    )
    users_all = User.query.all()
    user_perm_values = {
        u.id: {field: bool(getattr(u, field, None)) for field in EDITABLE_USER_PERMISSION_FIELDS}
        for u in users_all
    }
    default_permissions = {
        field: bool(USER_PERMISSION_DEFAULTS.get(field, False))
        for field in EDITABLE_USER_PERMISSION_FIELDS
    }
    return render_template(
        'settings.html',
        users=users_all,
        settings=settings_obj,
        categories=categories,
        recon_report=recon_report,
        recent_audit=recent_audit,
        permission_groups=PERMISSION_GROUPS,
        permission_fields=EDITABLE_USER_PERMISSION_FIELDS,
        user_perm_values=user_perm_values,
        default_permissions=default_permissions,
    )


@bp.route('/settings/activity')
@login_required
def activity_log_page():
    if current_user.role != 'admin' and not _user_can('can_access_settings'):
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))
    q = (request.args.get('q') or '').strip()
    query = AuditLog.query
    if q:
        like = f'%{q}%'
        query = query.filter(or_(
            AuditLog.username.ilike(like),
            AuditLog.action.ilike(like),
            AuditLog.details.ilike(like),
        ))
    page = request.args.get('page', 1, type=int)
    rows = query.order_by(AuditLog.timestamp.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('activity_log.html', rows=rows, q=q)


@bp.route('/settings/sessions')
@login_required
def login_sessions_page():
    if current_user.role != 'admin' and not _user_can('can_access_settings'):
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))
    from utils.sessions import list_active_sessions
    from models import UserLoginSession
    live = list_active_sessions(45)
    recent = UserLoginSession.query.order_by(UserLoginSession.created_at.desc()).limit(80).all()
    return render_template('login_sessions.html', live=live, recent=recent)


@bp.route('/void_audit')
@login_required
def void_audit_page():
    if current_user.role != 'admin' and not _user_can('can_access_settings'):
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))

    section = (request.args.get('section') or 'all').strip().lower()
    q = (request.args.get('q') or '').strip().lower()
    rows = []

    def _entry_voided_by_edit(entry_obj):
        if not entry_obj or not entry_obj.is_void:
            return False
        if (entry_obj.nimbus_no or '').strip().lower() != 'direct sale':
            return False
        bill_ref = (entry_obj.bill_no or '').strip() or (entry_obj.auto_bill_no or '').strip()
        if not bill_ref:
            return False
        newer_active = Entry.query.filter(
            Entry.id != entry_obj.id,
            Entry.is_void == False,
            Entry.nimbus_no == entry_obj.nimbus_no,
            Entry.type == entry_obj.type,
            Entry.material == entry_obj.material,
            Entry.client == entry_obj.client,
            Entry.qty == entry_obj.qty,
            or_(Entry.bill_no == bill_ref, Entry.auto_bill_no == bill_ref)
        ).order_by(Entry.id.desc()).first()
        return bool(newer_active and newer_active.id > entry_obj.id)

    def _pending_voided_by_edit(pb_obj):
        if not pb_obj or not pb_obj.is_void:
            return False
        bill_ref = (pb_obj.bill_no or '').strip()
        if not bill_ref:
            return False
        if not (pb_obj.reason or '').strip().lower().startswith('direct sale'):
            return False
        newer_active = PendingBill.query.filter(
            PendingBill.id != pb_obj.id,
            PendingBill.is_void == False,
            PendingBill.bill_no == bill_ref,
            PendingBill.client_name == pb_obj.client_name
        ).order_by(PendingBill.id.desc()).first()
        return bool(newer_active and newer_active.id > pb_obj.id)

    def _push_row(entity, obj_id, title, details, when_dt, status_label):
        rows.append({
            'entity': entity,
            'id': obj_id,
            'title': title,
            'details': details,
            'when': when_dt,
            'status': status_label,
            'is_void': status_label.startswith('Deleted'),
            'is_suspended': status_label.startswith('Suspended'),
        })

    voided_enabled = section in ('all', 'voided', 'transactions')
    suspended_enabled = section in ('all', 'suspended', 'directory')

    if voided_enabled:
        for e in Entry.query.filter_by(is_void=True).all():
            dt = _parse_dt_safe(f"{(e.date or '').strip()} {(e.time or '').strip()}".strip()) or _parse_dt_safe(e.date)
            _push_row(
                'Entry', e.id,
                f"Entry #{e.id} ({e.type or '-'})",
                f"Client: {e.client or '-'} | Material: {e.material or '-'} | Bill: {e.bill_no or e.auto_bill_no or '-'}",
                dt,
                ('Deleted by Edit' if _entry_voided_by_edit(e) else 'Deleted Transaction')
            )
        for b in Booking.query.filter_by(is_void=True).all():
            _push_row(
                'Booking', b.id,
                f"Booking #{b.id}",
                f"Client: {b.client_name or '-'} | Bill: {b.manual_bill_no or b.auto_bill_no or '-'} | Amount: {float(b.amount or 0):.2f}",
                _parse_dt_safe(b.date_posted),
                'Deleted Bill'
            )
        for p in Payment.query.filter_by(is_void=True).all():
            _push_row(
                'Payment', p.id,
                f"Payment #{p.id}",
                f"Client: {p.client_name or '-'} | Bill: {p.manual_bill_no or p.auto_bill_no or '-'} | Amount: {float(p.amount or 0):.2f}",
                _parse_dt_safe(p.date_posted),
                'Deleted Bill'
            )
        for s in DirectSale.query.filter_by(is_void=True).all():
            _push_row(
                'DirectSale', s.id,
                f"Direct Sale #{s.id}",
                f"Client: {s.client_name or '-'} | Bill: {s.manual_bill_no or s.auto_bill_no or '-'} | Amount: {float(s.amount or 0):.2f}",
                _parse_dt_safe(s.date_posted),
                'Deleted Bill'
            )
        for mr in MaterialReturn.query.filter_by(is_void=True).all():
            _push_row(
                'MaterialReturn', mr.id,
                f"Material Return #{mr.id}",
                f"Client: {mr.client_name or '-'} | Bill: {mr.manual_bill_no or mr.auto_bill_no or '-'} | Amount: {float(mr.amount or 0):.2f}",
                _parse_dt_safe(mr.date_posted),
                'Deleted Bill'
            )
        for pb in PendingBill.query.filter_by(is_void=True).all():
            _push_row(
                'PendingBill', pb.id,
                f"Pending Bill #{pb.id}",
                f"Client: {pb.client_name or '-'} | Bill: {pb.bill_no or '-'} | Amount: {float(pb.amount or 0):.2f}",
                _parse_dt_safe(pb.created_at),
                ('Deleted by Edit' if _pending_voided_by_edit(pb) else 'Deleted Bill')
            )
        for dr in DeliveryRent.query.filter_by(is_void=True).all():
            _push_row(
                'DeliveryRent', dr.id,
                f"Delivery Rent #{dr.id}",
                f"Driver: {dr.delivery_person_name or '-'} | Bill: {dr.bill_no or '-'} | Amount: {float(dr.amount or 0):.2f}",
                _parse_dt_safe(dr.date_posted),
                'Deleted Transaction'
            )
        for sp in SupplierPayment.query.filter_by(is_void=True).all():
            supplier_name = sp.supplier.name if sp.supplier else f"Supplier #{sp.supplier_id}"
            _push_row(
                'SupplierPayment', sp.id,
                f"Supplier Payment #{sp.id}",
                f"Supplier: {supplier_name} | Amount: {float(sp.amount or 0):.2f} | Method: {sp.method or '-'}",
                _parse_dt_safe(sp.date_posted),
                'Deleted Transaction'
            )

    if suspended_enabled:
        for c in Client.query.filter_by(is_active=False).all():
            _push_row(
                'Client', c.id,
                f"Client #{c.id}",
                f"{c.name or '-'} ({c.code or '-'})",
                _parse_dt_safe(c.created_at),
                'Suspended Master'
            )
        for m in Material.query.filter_by(is_active=False).all():
            _push_row(
                'Material', m.id,
                f"Material #{m.id}",
                f"{m.name or '-'} ({m.code or '-'}) | Unit: {m.unit or '-'}",
                _parse_dt_safe(m.created_at),
                'Suspended Master'
            )
        for d in DeliveryPerson.query.filter_by(is_active=False).all():
            _push_row(
                'DeliveryPerson', d.id,
                f"Delivery Person #{d.id}",
                d.name or '-',
                _parse_dt_safe(d.created_at),
                'Suspended Master'
            )

    if q:
        rows = [
            r for r in rows
            if q in f"{r['entity']} {r['title']} {r['details']} {r['status']}".lower()
        ]

    rows.sort(key=lambda x: x.get('when') or datetime.min, reverse=True)

    total = len(rows)
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 50
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]

    counts = {
        'total': total,
        'voided': sum(1 for r in rows if r['is_void']),
        'suspended': sum(1 for r in rows if r['is_suspended']),
    }

    return render_template(
        'void_audit.html',
        rows=page_rows,
        section=section,
        q=request.args.get('q', ''),
        page=page,
        pages=pages,
        total=total,
        counts=counts
    )


@bp.route('/void_audit/restore/<string:entity>/<int:record_id>', methods=['POST'])
@login_required
def restore_audit_record(entity, record_id):
    if current_user.role != 'admin' and not _user_can('can_access_settings'):
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))

    key = (entity or '').strip()
    changed = False
    found = True

    if key == 'Entry':
        obj = db.session.get(Entry, record_id)
        found = bool(obj)
        changed = _set_entry_void_state(obj, False) if obj else False
    elif key == 'Booking':
        obj = db.session.get(Booking, record_id)
        found = bool(obj)
        changed = _set_booking_void_state(obj, False) if obj else False
    elif key == 'Payment':
        obj = db.session.get(Payment, record_id)
        found = bool(obj)
        changed = _set_payment_void_state(obj, False) if obj else False
    elif key == 'DirectSale':
        obj = db.session.get(DirectSale, record_id)
        found = bool(obj)
        changed = _set_direct_sale_void_state(obj, False) if obj else False
    elif key == 'MaterialReturn':
        obj = db.session.get(MaterialReturn, record_id)
        found = bool(obj)
        changed = _set_material_return_void_state(obj, False) if obj else False
    elif key == 'PendingBill':
        obj = db.session.get(PendingBill, record_id)
        found = bool(obj)
        if obj and obj.is_void:
            obj.is_void = False
            changed = True
    elif key == 'DeliveryRent':
        obj = db.session.get(DeliveryRent, record_id)
        found = bool(obj)
        if obj and obj.is_void:
            obj.is_void = False
            changed = True
    elif key == 'SupplierPayment':
        obj = db.session.get(SupplierPayment, record_id)
        found = bool(obj)
        if obj and obj.is_void:
            obj.is_void = False
            changed = True
    elif key == 'Client':
        obj = db.session.get(Client, record_id)
        found = bool(obj)
        if obj and not obj.is_active:
            obj.is_active = True
            changed = True
    elif key == 'Material':
        obj = db.session.get(Material, record_id)
        found = bool(obj)
        if obj and not obj.is_active:
            obj.is_active = True
            changed = True
    elif key == 'DeliveryPerson':
        obj = db.session.get(DeliveryPerson, record_id)
        found = bool(obj)
        if obj and not obj.is_active:
            obj.is_active = True
            changed = True
    else:
        found = False

    if not found:
        flash('Record not found', 'danger')
    elif changed:
        db.session.commit()
        flash('Record restored successfully', 'success')
    else:
        flash('Record is already active/restored', 'info')

    next_url = (request.form.get('next') or '').strip()
    return redirect(next_url or url_for('void_audit_page'))


