"""delivery — split from ops.py."""
from ._common import *  # noqa

from uuid import uuid4

@bp.route('/delivery_rents')
@login_required
def delivery_rents_page():
    if not _user_can('can_view_delivery_rent'):
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))

    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    driver = (request.args.get('driver') or '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    per_page = min(max(per_page, 10), 50)

    q = SaleDeliveryPerson.query.options(
        selectinload(SaleDeliveryPerson.sale).selectinload(DirectSale.invoice),
        selectinload(SaleDeliveryPerson.delivery_person)
    ).join(DirectSale, SaleDeliveryPerson.sale_id == DirectSale.id).filter(
        SaleDeliveryPerson.is_void == False
    )
    if date_from:
        q = q.filter(func.date(SaleDeliveryPerson.created_at) >= date_from)
    if date_to:
        q = q.filter(func.date(SaleDeliveryPerson.created_at) <= date_to)
    if driver:
        q = q.join(DeliveryPerson, SaleDeliveryPerson.delivery_person_id == DeliveryPerson.id).filter(
            func.lower(func.trim(DeliveryPerson.name)) == driver.lower()
        )

    total_rent = float(q.with_entities(func.sum(SaleDeliveryPerson.rent_amount)).scalar() or 0)
    pagination = q.order_by(SaleDeliveryPerson.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    rows = pagination.items

    payment_totals = {}
    if rows:
        alloc_ids = [r.id for r in rows]
        pay_rows = db.session.query(
            DeliveryPersonPayment.allocation_id,
            func.sum(DeliveryPersonPayment.amount_paid),
            func.sum(DeliveryPersonPayment.waive_off_amount)
        ).filter(
            DeliveryPersonPayment.is_void == False,
            DeliveryPersonPayment.allocation_id.in_(alloc_ids)
        ).group_by(DeliveryPersonPayment.allocation_id).all()
        for alloc_id, paid_sum, waive_sum in pay_rows:
            payment_totals[alloc_id] = {
                'paid': float(paid_sum or 0),
                'waived': float(waive_sum or 0)
            }

    paid_query = db.session.query(
        func.sum(DeliveryPersonPayment.amount_paid),
        func.sum(DeliveryPersonPayment.waive_off_amount)
    ).join(
        SaleDeliveryPerson, DeliveryPersonPayment.allocation_id == SaleDeliveryPerson.id
    ).filter(
        DeliveryPersonPayment.is_void == False,
        SaleDeliveryPerson.is_void == False
    )
    if date_from:
        paid_query = paid_query.filter(func.date(SaleDeliveryPerson.created_at) >= date_from)
    if date_to:
        paid_query = paid_query.filter(func.date(SaleDeliveryPerson.created_at) <= date_to)
    if driver:
        paid_query = paid_query.join(DeliveryPerson, SaleDeliveryPerson.delivery_person_id == DeliveryPerson.id).filter(
            func.lower(func.trim(DeliveryPerson.name)) == driver.lower()
        )
    paid_sum_all, waived_sum_all = paid_query.first() or (0, 0)
    total_paid = float(paid_sum_all or 0)
    total_waived = float(waived_sum_all or 0)
    total_due = max(0.0, total_rent - total_paid - total_waived)

    for r in rows:
        totals = payment_totals.get(r.id, {'paid': 0.0, 'waived': 0.0})
        r.paid_total = float(totals.get('paid', 0) or 0)
        r.waive_total = float(totals.get('waived', 0) or 0)
        r.due_total = max(0.0, float(r.rent_amount or 0) - r.paid_total - r.waive_total)

    totals_rows = db.session.query(
        DeliveryPerson.name,
        func.sum(SaleDeliveryPerson.rent_amount)
    ).join(SaleDeliveryPerson, SaleDeliveryPerson.delivery_person_id == DeliveryPerson.id).filter(
        SaleDeliveryPerson.is_void == False
    )
    if date_from:
        totals_rows = totals_rows.filter(func.date(SaleDeliveryPerson.created_at) >= date_from)
    if date_to:
        totals_rows = totals_rows.filter(func.date(SaleDeliveryPerson.created_at) <= date_to)
    if driver:
        totals_rows = totals_rows.filter(func.lower(func.trim(DeliveryPerson.name)) == driver.lower())
    totals_by_driver = totals_rows.group_by(DeliveryPerson.name).order_by(
        func.sum(SaleDeliveryPerson.rent_amount).desc()
    ).all()

    active_driver_names = {
        (name or '').strip()
        for (name,) in db.session.query(DeliveryPerson.name).filter(
            DeliveryPerson.is_active == True
        ).all()
        if (name or '').strip()
    }
    historical_driver_names = {
        (name or '').strip()
        for (name,) in db.session.query(DeliveryPerson.name).join(
            SaleDeliveryPerson, SaleDeliveryPerson.delivery_person_id == DeliveryPerson.id
        ).filter(
            SaleDeliveryPerson.is_void == False
        ).distinct().all()
        if (name or '').strip()
    }
    driver_names = sorted(active_driver_names | historical_driver_names)

    payment_accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True,  # noqa: E712
        func.lower(func.trim(Account.category)).in_(['cash', 'bank']),
    ).order_by(Account.name.asc(), Account.id.asc()).all()

    return render_template(
        'delivery_rents.html',
        rows=rows,
        payment_accounts=payment_accounts,
        submission_token=uuid4().hex,
        total_rent=total_rent,
        total_paid=total_paid,
        total_waived=total_waived,
        total_due=total_due,
        totals_by_driver=totals_by_driver,
        driver_names=driver_names,
        date_from=date_from,
        date_to=date_to,
        driver_filter=driver,
        pagination=pagination,
        per_page=per_page
    )


@bp.route('/delivery_rents/void/<int:id>', methods=['POST'])
@login_required
def void_delivery_rent(id):
    if not _user_can('can_manage_sales'):
        flash('Permission denied', 'danger')
        return redirect(url_for('delivery_rents_page'))
    row = db.session.get(SaleDeliveryPerson, id)
    if row:
        row.is_void = True
        db.session.commit()
        flash('Delivery rent entry deleted.', 'success')
    return redirect(url_for('delivery_rents_page'))


@bp.route('/delivery_rents/pay/<int:alloc_id>', methods=['POST'])
@login_required
def pay_delivery_rent(alloc_id):
    """Per-allocation entry point — same core, same single financial transaction."""
    if not _user_can('can_manage_sales'):
        flash('Permission denied', 'danger')
        return redirect(url_for('delivery_rents_page'))

    alloc = db.session.get(SaleDeliveryPerson, alloc_id)
    if not alloc or alloc.is_void:
        flash('Invalid delivery rent entry.', 'danger')
        return redirect(url_for('delivery_rents_page'))

    from app.services.driver_payments import save_driver_payment
    try:
        payment, _created = save_driver_payment(
            delivery_person_id=alloc.delivery_person_id,
            allocation_id=alloc.id,
            amount_paid=request.form.get('paid_amount', 0) or 0,
            waive_off_amount=request.form.get('waive_off_amount', 0) or 0,
            method=(request.form.get('method') or 'Cash'),
            payment_account_id=request.form.get('payment_account_id', type=int),
            reference=(request.form.get('reference') or '').strip(),
            date_posted=(request.form.get('date') or '').strip(),
            note=(request.form.get('note') or '').strip(),
            idempotency_key=(request.form.get('idempotency_key') or '').strip() or None,
            actor=current_user,
        )
        replayed = getattr(payment, '_idempotent_replay', False)
        db.session.commit()
        if replayed:
            flash('This delivery rent payment was already recorded; no duplicate was created.', 'info')
        else:
            flash('Delivery rent payment recorded. The selected account and the driver ledger were updated together.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.getLogger(__name__).exception('Delivery rent payment failed')
        flash(f'Unable to record the delivery rent payment: {exc}', 'danger')
    return redirect(url_for('delivery_rents_page'))


