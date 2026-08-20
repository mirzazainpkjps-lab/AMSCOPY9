from ._common import *  # noqa

@bp.route('/edit_client/<int:id>', methods=['POST'])
@login_required
def edit_client(id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))
    c = db.session.get(Client, id)
    if c:
        before = {'id': c.id, 'code': c.code, 'name': c.name, 'phone': c.phone,
                  'address': c.address, 'opening_balance': c.opening_balance,
                  'is_active': bool(c.is_active)}
        old_code = c.code
        old_name = c.name
        new_code = request.form.get('code', '').strip()
        new_name = request.form.get('name', '').strip()

        if not new_code:
            flash('Client code is required', 'danger')
            return redirect(url_for('clients'))

        existing = Client.query.filter_by(code=new_code).first()
        if existing and existing.id != id:
            flash(f'Client code "{new_code}" already exists', 'danger')
            return redirect(url_for('clients'))

        if old_code != new_code or old_name != new_name:
            PendingBill.query.filter_by(client_code=old_code).update({
                'client_code': new_code,
                'client_name': new_name
            })
            Entry.query.filter_by(client_code=old_code).update({
                'client_code': new_code,
                'client': new_name
            })
            Entry.query.filter_by(client=old_name).update({'client': new_name})

            # Propagate name change to all related tables to prevent broken links
            Booking.query.filter_by(client_name=old_name).update({'client_name': new_name})
            DirectSale.query.filter_by(client_name=old_name).update({
                'client_name': new_name,
                'client_code': new_code
            })
            # Payment.client_id is the stable relationship; keep client_name as
            # the historical display snapshot instead of rewriting financial history.
            WaiveOff.query.filter_by(client_name=old_name).update({
                'client_name': new_name,
                'client_code': new_code
            })
            Invoice.query.filter_by(client_name=old_name).update({'client_name': new_name})
            Invoice.query.filter_by(client_code=old_code).update({'client_code': new_code})
            
            # CRITICAL FIX: Update orphaned tables that were previously being skipped
            # See CLIENT_ORPHANING_FIX_PLAN.md for context on this data integrity issue
            MaterialReturn.query.filter_by(client_name=old_name).update({'client_name': new_name})
            Delivery.query.filter_by(client_name=old_name).update({'client_name': new_name})
            DirectSaleDraft.query.filter_by(client_name=old_name).update({'client_name': new_name})
            DirectSaleDraft.query.filter_by(client_code=old_code).update({'client_code': new_code})

        page_entries_raw = request.form.getlist('page_entry')
        page_entries_clean = [str(x).strip() for x in page_entries_raw if str(x).strip()]
        page_notes_value = ' | '.join(page_entries_clean) if page_entries_clean else (request.form.get('page_notes', '') or '').strip()

        c.name = new_name
        c.code = new_code
        c.phone = request.form.get('phone', '')
        c.address = request.form.get('address', '')
        c.category = (request.form.get('category', 'General').strip() or 'General')
        c.book_no = ''
        c.financial_book_no = ''
        c.financial_page = ''
        c.cement_book_no = ''
        c.cement_page = ''
        c.steel_book_no = ''
        c.steel_page = page_notes_value
        c.location_url = (request.form.get('location_url', '') or '').strip()
        c.page_notes = page_notes_value
        c.opening_balance = _to_float_or_zero(request.form.get('opening_balance', c.opening_balance))
        c.opening_balance_date = _resolve_opening_balance_date(
            request.form.get('opening_balance_date'),
            fallback_dt=(c.opening_balance_date or c.created_at)
        )

        from utils.accounting_audit import record_accounting_audit
        after = {'id': c.id, 'code': c.code, 'name': c.name, 'phone': c.phone,
                 'address': c.address, 'opening_balance': c.opening_balance,
                 'is_active': bool(c.is_active)}
        record_accounting_audit(
            current_user, action='Edit', entity_type='Client', entity_id=c.id,
            before=before, after=after, party_before_id=c.id, party_after_id=c.id,
            reason='Client master updated', module='clients',
        )
        db.session.commit()
        flash('Client updated', 'success')
    return redirect(url_for('clients'))

