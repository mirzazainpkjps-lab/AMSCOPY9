"""Delivery-person financial ledger routes."""
from ._common import *  # noqa

import csv
import logging
from io import StringIO
from datetime import datetime
from uuid import uuid4

from app.services.financial_ledgers import (
    build_delivery_person_financial_ledger,
    filter_ledger_rows,
)


def _driver_filters():
    return {
        'start_date': (request.args.get('start_date') or request.args.get('date_from') or '').strip(),
        'end_date': (request.args.get('end_date') or request.args.get('date_to') or '').strip(),
        'type_filter': (request.args.get('type') or request.args.get('transaction_type') or '').strip(),
        'query': (request.args.get('q') or request.args.get('search') or '').strip(),
        'amount_min': (request.args.get('amount_min') or '').strip(),
        'amount_max': (request.args.get('amount_max') or '').strip(),
        'account_filter': (request.args.get('account') or '').strip(),
        'status_filter': (request.args.get('status') or '').strip(),
    }


def _driver_page(ledger, filters):
    selected = ledger['filtered_rows']
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    per_page = min(max(request.args.get('per_page', 25, type=int) or 25, 10), 100)
    total = len(selected)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = selected[(page - 1) * per_page: page * per_page]
    closing = rows[-1]['balance'] if rows else ledger['closing_balance']
    return rows, page, per_page, total, pages, closing


def _driver_payment_accounts():
    """Cash/bank accounts selectable as the explicit source of funds."""
    return Account.query.filter(
        func.coalesce(Account.is_active, True) == True,  # noqa: E712
        func.lower(func.trim(Account.category)).in_(['cash', 'bank']),
    ).order_by(Account.name.asc(), Account.id.asc()).all()


def _render_driver_ledger(person, *, filters=None):
    filters = filters or _driver_filters()
    ledger = build_delivery_person_financial_ledger(person, **filters)
    rows, page, per_page, total, pages, closing = _driver_page(ledger, filters)
    people = DeliveryPerson.query.order_by(DeliveryPerson.name.asc(), DeliveryPerson.id.asc()).all()
    return render_template(
        'financial_ledger.html',
        entity=person,
        entity_type='delivery_person',
        ledger=ledger,
        rows=rows,
        all_rows=ledger['rows'],
        obligations=[],
        filters=filters,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=pages,
        filtered_closing=closing,
        selector_entities=people,
        current_payable=max(0.0, float(ledger['closing_balance'] or 0)),
        back_url=url_for('delivery_persons_page'),
        opening_url=url_for('delivery_person_opening_balance', id=person.id),
        today_date=pk_today().strftime('%Y-%m-%d'),
        payment_accounts=_driver_payment_accounts(),
        submission_token=uuid4().hex,
    )


@bp.route('/delivery_person_ledger/<int:id>')
@bp.route('/delivery_ledger/<int:id>')
@login_required
def delivery_person_ledger(id):
    if not _user_can('can_view_delivery_rent') and not _user_can('can_view_client_ledger'):
        flash('Permission denied', 'danger')
        return redirect(url_for('index'))
    person = DeliveryPerson.query.get_or_404(id)
    requested_person = (request.args.get('driver_search') or '').strip()
    if requested_person:
        alternate = DeliveryPerson.query.filter(
            func.lower(func.trim(DeliveryPerson.name)) == requested_person.casefold()
        ).first()
        if alternate and alternate.id != person.id:
            preserved = request.args.to_dict()
            preserved.pop('driver_search', None)
            return redirect(url_for('delivery_person_ledger', id=alternate.id, **preserved))
    return _render_driver_ledger(person)


@bp.route('/delivery_person_opening_balance/<int:id>', methods=['POST'])
@login_required
def delivery_person_opening_balance(id):
    if not _user_can('can_manage_delivery_persons'):
        flash('Permission denied', 'danger')
        return redirect(url_for('delivery_person_ledger', id=id))
    person = DeliveryPerson.query.get_or_404(id)
    person.opening_balance = _to_float_or_zero(request.form.get('opening_balance', 0))
    person.opening_balance_date = _resolve_opening_balance_date(
        request.form.get('opening_balance_date'),
        fallback_dt=person.opening_balance_date or person.created_at,
    )
    db.session.commit()
    flash('Delivery person opening balance updated.', 'success')
    return redirect(url_for('delivery_person_ledger', id=id))


@bp.route('/delivery_person_ledger/<int:id>/pay', methods=['POST'])
@login_required
def settle_delivery_person(id):
    """Driver-section entry point: delegates to the shared financial core.

    This is a convenience *action layer* only.  The settlement is allocated
    FIFO across open rent items, and every allocated slice creates the same
    authoritative AccountTransaction that the Accounts section would create.
    """
    if not _user_can('can_manage_sales'):
        flash('Permission denied', 'danger')
        return redirect(url_for('delivery_person_ledger', id=id))
    person = DeliveryPerson.query.get_or_404(id)
    from app.services.driver_payments import settle_driver_fifo
    try:
        rows = settle_driver_fifo(
            delivery_person_id=person.id,
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
        replayed = bool(rows) and getattr(rows[0], '_idempotent_replay', False)
        db.session.commit()
        if replayed:
            flash('This settlement was already recorded; no duplicate was created.', 'info')
        else:
            flash(f'Driver payment recorded across {len(rows)} rent item(s). '
                  f'The cash/bank account and driver ledger were updated together.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.getLogger(__name__).exception('Driver settlement failed')
        flash(f'Unable to record the driver payment: {exc}', 'danger')
    return redirect(url_for('delivery_person_ledger', id=id))


@bp.route('/delivery_person_payments/<int:id>/edit', methods=['POST'])
@login_required
def edit_delivery_person_payment(id):
    """Edit through the same core so the net delta is applied exactly once."""
    if not _user_can('can_manage_sales'):
        flash('Permission denied', 'danger')
        return redirect(url_for('delivery_persons_page'))
    payment = DeliveryPersonPayment.query.get_or_404(id)
    person_id = payment.delivery_person_id
    from app.services.driver_payments import save_driver_payment
    try:
        account_raw = request.form.get('payment_account_id', type=int)
        save_driver_payment(
            payment_id=payment.id,
            delivery_person_id=person_id,
            amount_paid=request.form.get('amount_paid', 0) or 0,
            waive_off_amount=request.form.get('waive_off_amount', 0) or 0,
            method=(request.form.get('method') or payment.method or 'Cash'),
            payment_account_id=(account_raw if account_raw else payment.payment_account_id),
            allocation_id=payment.allocation_id,
            reference=(request.form.get('reference') or payment.reference or '').strip(),
            date_posted=(request.form.get('date') or '').strip(),
            note=(request.form.get('note') or '').strip(),
            expected_revision=request.form.get('expected_revision'),
            actor=current_user,
        )
        db.session.commit()
        flash('Driver payment updated. Balances were adjusted by the difference only.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.getLogger(__name__).exception('Driver payment edit failed')
        flash(f'Unable to update the driver payment: {exc}', 'danger')
    return redirect(url_for('delivery_person_ledger', id=person_id))


@bp.route('/delivery_person_payments/<int:id>/void', methods=['POST'])
@login_required
def void_delivery_person_payment(id):
    """Controlled reversal: restores the account balance and driver payable once."""
    if not _user_can('can_manage_sales'):
        flash('Permission denied', 'danger')
        return redirect(url_for('delivery_persons_page'))
    payment = DeliveryPersonPayment.query.get_or_404(id)
    person_id = payment.delivery_person_id
    from app.services.driver_payments import delete_driver_payment
    try:
        if delete_driver_payment(payment, actor=current_user):
            db.session.commit()
            flash('Driver payment reversed. The account balance was restored and history preserved.', 'success')
        else:
            flash('This driver payment is already reversed.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.getLogger(__name__).exception('Driver payment reversal failed')
        flash(f'Unable to reverse the driver payment: {exc}', 'danger')
    return redirect(url_for('delivery_person_ledger', id=person_id))


@bp.route('/delivery_person_payments/<int:id>/restore', methods=['POST'])
@login_required
def restore_delivery_person_payment(id):
    if not _user_can('can_manage_sales'):
        flash('Permission denied', 'danger')
        return redirect(url_for('delivery_persons_page'))
    payment = DeliveryPersonPayment.query.get_or_404(id)
    person_id = payment.delivery_person_id
    from app.services.driver_payments import restore_driver_payment
    try:
        if restore_driver_payment(payment, actor=current_user):
            db.session.commit()
            flash('Driver payment restored. The account effect was re-applied once.', 'success')
        else:
            flash('This driver payment is already active.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.getLogger(__name__).exception('Driver payment restore failed')
        flash(f'Unable to restore the driver payment: {exc}', 'danger')
    return redirect(url_for('delivery_person_ledger', id=person_id))


@bp.route('/download_delivery_person_ledger/<int:id>')
@login_required
def download_delivery_person_ledger(id):
    person = DeliveryPerson.query.get_or_404(id)
    filters = _driver_filters()
    ledger = build_delivery_person_financial_ledger(person, **filters)
    action = (request.args.get('action') or 'download').lower()
    if action != 'print':
        out = StringIO(newline='')
        writer = csv.writer(out)
        writer.writerow(['Date', 'Type', 'Reference', 'Description', 'Debit', 'Credit', 'Balance', 'Notes'])
        for row in ledger['filtered_rows']:
            writer.writerow([
                row['date'].strftime('%Y-%m-%d %H:%M') if row.get('date') and row['date'] != datetime.min else '',
                row.get('type', ''), row.get('reference', ''), row.get('description', ''),
                f"{row.get('debit', 0):.2f}", f"{row.get('credit', 0):.2f}",
                f"{row.get('balance', 0):.2f}", row.get('note', ''),
            ])
        response = Response(out.getvalue(), mimetype='text/csv; charset=utf-8')
        response.headers['Content-Disposition'] = 'attachment; filename=delivery-person-ledger.csv'
        return response
    # Print/download HTML remains dependency-free and can be printed by any
    # browser, just like the legacy ledger fallback.
    rendered = render_template(
        'supplier_ledger_print.html',
        supplier=person,
        ledger=ledger['filtered_rows'],
        final_balance=ledger['closing_balance'],
        total_bill=ledger['total_debit'],
        total_paid=ledger['total_credit'],
        generated_at=pk_now(), auto_print=True,
    )
    response = make_response(rendered)
    response.headers['Content-Disposition'] = 'inline; filename=delivery-person-ledger.html'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


@bp.route('/api/delivery_persons/search')
@login_required
def delivery_person_search_api():
    query = (request.args.get('q') or '').strip()
    people = DeliveryPerson.query
    if query:
        people = people.filter(DeliveryPerson.name.ilike(f'%{query}%'))
    return jsonify([{
        'id': person.id, 'name': person.name, 'phone': person.phone or '', 'is_active': bool(person.is_active)
    } for person in people.order_by(DeliveryPerson.name.asc()).limit(25).all()])


@bp.route('/api/delivery_person_ledger/<int:id>')
@login_required
def delivery_person_ledger_api(id):
    person = DeliveryPerson.query.get_or_404(id)
    ledger = build_delivery_person_financial_ledger(person)
    return jsonify({
        'ok': True,
        'delivery_person': {'id': person.id, 'name': person.name, 'phone': person.phone or ''},
        'opening_balance': float(person.opening_balance or 0),
        'total_debit': ledger['total_debit'],
        'total_credit': ledger['total_credit'],
        'closing_balance': ledger['closing_balance'],
        'status': ledger['status'],
        'rows': [{
            'date': row['date'].isoformat() if row.get('date') and row['date'] != datetime.min else None,
            'type': row.get('type'), 'reference': row.get('reference'),
            'description': row.get('description'), 'debit': row.get('debit', 0),
            'credit': row.get('credit', 0), 'balance': row.get('balance', 0),
            'source_type': row.get('source_type'), 'source_id': row.get('source_id'),
            'note': row.get('note') or '',
        } for row in ledger['rows']],
    })
