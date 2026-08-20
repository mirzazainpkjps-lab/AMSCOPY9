"""cash — configuration-driven Cash Flow routes."""
from ._common import *  # noqa

logger = logging.getLogger(__name__)

from app.services.cash_flow_svc import (
    CF_DIR_IN,
    CF_DIR_OUT,
    CF_DIR_TRANSFER,
    CF_PARTY_TYPES,
    CF_SOURCE_LABELS,
    apply_running_balance,
    categories_for_direction,
    cf_used_category_ids,
    cf_used_party_ids,
    cf_used_subcategory_ids,
    collect_cash_flow_rows,
    compute_physical_cash_difference,
    delete_cf_category,
    delete_cf_party,
    delete_cf_subcategory,
    disable_cf_category,
    disable_cf_party,
    disable_cf_subcategory,
    enable_cf_category,
    enable_cf_party,
    enable_cf_subcategory,
    filter_cash_flow_rows,
    parse_physical_cash_amount,
    rename_cf_category,
    rename_cf_subcategory,
    restore_manual_cash_flow_entry,
    save_cf_category,
    save_cf_party,
    save_cf_subcategory,
    save_manual_cash_flow_entry,
    subcategories_for_category,
    summarize_cash_flow_rows,
    update_cf_party,
    update_manual_cash_flow_entry,
    void_manual_cash_flow_entry,
    _cf_account_label,
    _cf_company_accounts,
    _cf_ensure_indexes,
    _cf_normalize_direction,
    _cf_sort_key,
    _cf_type_label,
)


def _cf_redirect(**kwargs):
    params = {k: v for k, v in kwargs.items() if v not in (None, '')}
    return redirect(url_for('cash_flow', **params))


def _cf_filter_kwargs(source):
    return {
        'from_date': source.get('from_date'),
        'to_date': source.get('to_date'),
        'filter_type': source.get('filter_type') or 'all',
        'origin': source.get('origin') or 'all',
        'category': source.get('category') or '',
        'subcategory': source.get('subcategory') or '',
        'party_type': source.get('party_type') or '',
        'party': source.get('party') or '',
        'account_id': source.get('account_id') or '',
        'q': source.get('q') or '',
        'notes': source.get('notes') or '',
        'reference': source.get('reference') or '',
        'description': source.get('description') or '',
        'created_by': source.get('created_by') or '',
        'status': source.get('status') or 'active',
        'amount_min': source.get('amount_min') or '',
        'amount_max': source.get('amount_max') or '',
    }


def _cf_json_payload():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


@bp.route('/cash_flow/meta/categories', methods=['GET', 'POST'])
@login_required
def cash_flow_categories_meta():
    if request.method == 'POST':
        payload = _cf_json_payload()
        try:
            cat, created = save_cf_category(
                payload.get('name') or payload.get('new_category_name'),
                payload.get('direction') or payload.get('new_category_direction') or 'both',
                notes=payload.get('notes') or payload.get('new_category_notes'),
                actor=current_user,
            )
            db.session.commit()
            return jsonify({
                'ok': True,
                'created': created,
                'category': {
                    'id': cat.id,
                    'name': cat.name,
                    'direction': cat.direction or 'both',
                    'notes': cat.notes or '',
                },
            })
        except ValueError as exc:
            db.session.rollback()
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow quick-add category failed')
            return jsonify({'ok': False, 'error': 'Unable to save category.'}), 500
    direction = _cf_normalize_direction(request.args.get('direction') or '')
    cats = categories_for_direction(direction or 'both')
    return jsonify({
        'categories': [
            {'id': c.id, 'name': c.name, 'direction': c.direction or 'both', 'notes': c.notes or ''}
            for c in cats
        ]
    })


@bp.route('/cash_flow/meta/subcategories', methods=['GET', 'POST'])
@login_required
def cash_flow_subcategories_meta():
    if request.method == 'POST':
        payload = _cf_json_payload()
        try:
            raw_parent = payload.get('category_id') or payload.get('new_sub_category_id')
            try:
                parent_id = int(raw_parent) if raw_parent not in (None, '') else None
            except (TypeError, ValueError):
                parent_id = None
            if not parent_id and payload.get('category_name'):
                cat_obj = CashFlowCategory.query.filter(func.lower(CashFlowCategory.name) == payload.get('category_name').strip().lower()).first()
                if cat_obj:
                    parent_id = cat_obj.id
            sub, created = save_cf_subcategory(
                parent_id,
                payload.get('name') or payload.get('new_subcategory_name'),
                notes=payload.get('notes') or payload.get('new_subcategory_notes'),
                actor=current_user,
            )
            db.session.commit()
            return jsonify({
                'ok': True,
                'created': created,
                'subcategory': {
                    'id': sub.id,
                    'name': sub.name,
                    'category_id': sub.category_id,
                    'notes': sub.notes or '',
                },
            })
        except (TypeError, ValueError) as exc:
            db.session.rollback()
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow quick-add subcategory failed')
            return jsonify({'ok': False, 'error': 'Unable to save sub-category.'}), 500
    category_id = request.args.get('category_id', type=int)
    if not category_id:
        return jsonify({'subcategories': []})
    return jsonify({
        'subcategories': [
            {'id': s.id, 'name': s.name, 'category_id': s.category_id, 'notes': s.notes or ''}
            for s in subcategories_for_category(category_id)
        ]
    })


@bp.route('/cash_flow/meta/parties', methods=['GET', 'POST'])
@login_required
def cash_flow_parties_meta():
    if request.method == 'POST':
        payload = _cf_json_payload()
        try:
            party, created = save_cf_party(
                payload.get('name') or payload.get('new_party_name'),
                payload.get('party_type') or payload.get('new_party_type') or 'person',
                note=payload.get('note') or payload.get('new_party_note'),
                actor=current_user,
            )
            db.session.commit()
            return jsonify({
                'ok': True,
                'created': created,
                'party': {
                    'id': party.id,
                    'name': party.name,
                    'party_type': party.party_type or 'other',
                    'note': party.note or '',
                },
            })
        except ValueError as exc:
            db.session.rollback()
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow quick-add party failed')
            return jsonify({'ok': False, 'error': 'Unable to save party.'}), 500
    parties = CashFlowParty.query.filter_by(is_active=True).order_by(CashFlowParty.name).all()
    return jsonify({
        'parties': [
            {'id': p.id, 'name': p.name, 'party_type': p.party_type or 'other', 'note': p.note or ''}
            for p in parties
        ]
    })


@bp.route('/cash_flow/entry/<int:entry_id>.json')
@login_required
def cash_flow_entry_json(entry_id):
    entry = CashFlowEntry.query.get_or_404(entry_id)
    dest = getattr(entry, 'destination_account', None)
    audits = []
    for a in sorted(getattr(entry, 'audit_trail', []) or [], key=lambda x: x.id):
        audits.append({
            'action': a.action,
            'reason': a.reason or '',
            'changed_by': a.changed_by or '',
            'changed_at': a.changed_at.strftime('%Y-%m-%d %H:%M') if a.changed_at else '',
        })
    return jsonify({
        'id': entry.id,
        'direction': entry.direction,
        'amount': float(entry.amount or 0),
        'account_id': entry.account_id,
        'account_name': entry.account.name if entry.account else '',
        'destination_account_id': getattr(entry, 'destination_account_id', None),
        'destination_account_name': dest.name if dest else '',
        'category_id': entry.category_id,
        'category_name': entry.category.name if entry.category else '',
        'subcategory_id': entry.subcategory_id,
        'subcategory_name': entry.subcategory.name if entry.subcategory else '',
        'party_id': entry.party_id,
        'party_name': entry.party_name or '',
        'party_type': entry.party_type or 'other',
        'description': entry.description or '',
        'note': entry.note or '',
        'reference': getattr(entry, 'reference', None) or '',
        'date_posted': entry.date_posted.strftime('%Y-%m-%dT%H:%M') if entry.date_posted else '',
        'is_void': bool(entry.is_void),
        'void_reason': getattr(entry, 'void_reason', None) or '',
        'voided_by': getattr(entry, 'voided_by', None) or '',
        'created_by': entry.created_by or '',
        'updated_by': getattr(entry, 'updated_by', None) or '',
        'status': 'voided' if entry.is_void else 'active',
        'audit': audits,
    })


@bp.route('/cash_flow', methods=['GET', 'POST'])
@login_required
def cash_flow():
    _cf_ensure_indexes()
    source = request.form if request.method == 'POST' else request.args
    fresh_start_dt = pk_today()
    fresh_start_date = fresh_start_dt.strftime('%Y-%m-%d')
    from_date = source.get('from_date', fresh_start_date)
    to_date = source.get('to_date', fresh_start_date)
    filter_kwargs = _cf_filter_kwargs(source)
    filter_type = filter_kwargs['filter_type']
    opening_balance_input = source.get('opening_balance', '').strip()
    export_pdf = request.args.get('export_pdf', '')
    export_csv = request.args.get('export_csv', '')
    export_scope = (request.args.get('export_scope') or 'filtered').strip().lower()

    adjustment_date_input = to_date
    physical_cash_input = ''
    reconciliation_reason = ''
    action = ''
    if request.method == 'POST':
        adjustment_date_input = request.form.get('adjustment_date', to_date).strip() or to_date
        physical_cash_input = request.form.get('physical_cash_available', '').strip()
        reconciliation_reason = request.form.get('reconciliation_reason', '').strip()
        action = request.form.get('action', '').strip()

    def _back():
        return _cf_redirect(**{**filter_kwargs, 'from_date': from_date, 'to_date': to_date})

    if request.method == 'POST' and action == 'add_category':
        try:
            save_cf_category(
                request.form.get('new_category_name'),
                request.form.get('new_category_direction') or 'both',
                notes=request.form.get('new_category_notes'),
                actor=current_user,
            )
            db.session.commit()
            flash('Category saved.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow add category failed')
            flash('Unable to save category.', 'danger')
        return _back()

    if request.method == 'POST' and action == 'add_subcategory':
        try:
            save_cf_subcategory(
                request.form.get('new_sub_category_id', type=int),
                request.form.get('new_subcategory_name'),
                notes=request.form.get('new_subcategory_notes'),
                actor=current_user,
            )
            db.session.commit()
            flash('Sub-category saved.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow add subcategory failed')
            flash('Unable to save sub-category.', 'danger')
        return _back()

    if request.method == 'POST' and action == 'add_party':
        try:
            save_cf_party(
                request.form.get('new_party_name'),
                request.form.get('new_party_type') or 'person',
                note=request.form.get('new_party_note'),
                actor=current_user,
            )
            db.session.commit()
            flash('Party saved.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow add party failed')
            flash('Unable to save party.', 'danger')
        return _back()

    if request.method == 'POST' and action == 'disable_category':
        try:
            disable_cf_category(request.form.get('category_id', type=int))
            db.session.commit()
            flash('Category disabled. Historical rows keep the name.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'disable_subcategory':
        try:
            disable_cf_subcategory(request.form.get('subcategory_id', type=int))
            db.session.commit()
            flash('Sub-category disabled. Historical rows keep the name.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'enable_category':
        try:
            enable_cf_category(request.form.get('category_id', type=int))
            db.session.commit()
            flash('Category enabled.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'enable_subcategory':
        try:
            enable_cf_subcategory(request.form.get('subcategory_id', type=int))
            db.session.commit()
            flash('Sub-category enabled.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'update_party':
        try:
            update_cf_party(
                request.form.get('party_id', type=int),
                request.form.get('party_name'),
                party_type=request.form.get('party_type'),
                note=request.form.get('party_note'),
            )
            db.session.commit()
            flash('Party updated.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'disable_party':
        try:
            disable_cf_party(request.form.get('party_id', type=int))
            db.session.commit()
            flash('Party disabled. Historical rows keep the name.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'enable_party':
        try:
            enable_cf_party(request.form.get('party_id', type=int))
            db.session.commit()
            flash('Party enabled.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'rename_category':
        try:
            rename_cf_category(
                request.form.get('category_id', type=int),
                request.form.get('category_name'),
                direction=request.form.get('category_direction'),
                notes=request.form.get('category_notes'),
            )
            db.session.commit()
            flash('Category updated. Historical rows stay linked to the same category.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'rename_subcategory':
        try:
            rename_cf_subcategory(
                request.form.get('subcategory_id', type=int),
                request.form.get('subcategory_name'),
                notes=request.form.get('subcategory_notes'),
            )
            db.session.commit()
            flash('Sub-category updated.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return _back()

    if request.method == 'POST' and action == 'delete_category':
        try:
            delete_cf_category(request.form.get('category_id', type=int))
            db.session.commit()
            flash('Unused category deleted.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow delete category failed')
            flash('Unable to delete category.', 'danger')
        return _back()

    if request.method == 'POST' and action == 'delete_subcategory':
        try:
            delete_cf_subcategory(request.form.get('subcategory_id', type=int))
            db.session.commit()
            flash('Unused sub-category deleted.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow delete subcategory failed')
            flash('Unable to delete sub-category.', 'danger')
        return _back()

    if request.method == 'POST' and action == 'delete_party':
        try:
            delete_cf_party(request.form.get('party_id', type=int))
            db.session.commit()
            flash('Unused party deleted.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow delete party failed')
            flash('Unable to delete party.', 'danger')
        return _back()

    if request.method == 'POST' and action in ('delete_entry', 'void_entry'):
        entry_id = request.form.get('entry_id', type=int)
        entry = CashFlowEntry.query.get(entry_id) if entry_id else None
        try:
            void_manual_cash_flow_entry(
                entry, reason=request.form.get('void_reason'), actor=current_user,
            )
            db.session.commit()
            audit_log(current_user, 'cash_flow.entry.void', f'id={entry_id}')
            flash('Transaction voided. The financial effect was reversed and the history was kept.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow void failed')
            flash('Unable to void transaction.', 'danger')
        return _back()

    if request.method == 'POST' and action == 'restore_entry':
        entry_id = request.form.get('entry_id', type=int)
        entry = CashFlowEntry.query.get(entry_id) if entry_id else None
        try:
            restore_manual_cash_flow_entry(
                entry, reason=request.form.get('restore_reason'), actor=current_user,
            )
            db.session.commit()
            audit_log(current_user, 'cash_flow.entry.restore', f'id={entry_id}')
            flash('Transaction restored and the account effect was re-applied.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow restore failed')
            flash('Unable to restore transaction.', 'danger')
        return _back()

    if request.method == 'POST' and action in ('record_movement', 'edit_entry'):
        entry_id = request.form.get('entry_id', type=int) if action == 'edit_entry' else None
        payload = dict(
            direction=request.form.get('direction'),
            amount=request.form.get('amount', 0),
            account_id=request.form.get('cash_account_id', type=int) or request.form.get('from_account_id', type=int),
            destination_account_id=request.form.get('to_account_id', type=int),
            category_id=request.form.get('category_id', type=int),
            category_name=request.form.get('category_name'),
            subcategory_id=request.form.get('subcategory_id', type=int),
            subcategory_name=request.form.get('subcategory_name'),
            party_id=request.form.get('party_id', type=int),
            party_name=request.form.get('party_name'),
            party_type=request.form.get('party_type') or 'other',
            description=request.form.get('description'),
            note=request.form.get('movement_note') or request.form.get('note'),
            reference=request.form.get('reference'),
            actor=current_user,
            create_missing=True,
        )
        date_raw = (request.form.get('movement_date') or '').strip()
        payload['date_posted'] = resolve_posted_datetime(date_raw or None)
        try:
            if action == 'edit_entry':
                entry = CashFlowEntry.query.get(entry_id) if entry_id else None
                update_manual_cash_flow_entry(
                    entry, reason=request.form.get('edit_reason'), **payload,
                )
                db.session.commit()
                audit_log(current_user, 'cash_flow.entry.edit', f'id={entry_id}')
                flash('Cash flow transaction updated.', 'success')
            else:
                payload['idempotency_key'] = (request.form.get('idempotency_key') or '').strip() or None
                entry, created = save_manual_cash_flow_entry(**payload)
                db.session.commit()
                if created:
                    audit_log(current_user, 'cash_flow.record', f'dir={entry.direction}, amount={entry.amount}')
                    flash(f'{_cf_type_label(entry.direction)} Rs. {float(entry.amount or 0):,.0f} recorded.', 'success')
                else:
                    flash('This transaction was already saved.', 'info')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Cash flow save failed')
            flash('Unable to save cash flow transaction.', 'danger')
        return _back()

    if request.method == 'POST' and action in ('set_opening_override', 'clear_opening_override', 'reset_fresh_start'):
        if action == 'reset_fresh_start':
            session['cash_flow_fresh_start_cutoff'] = {
                'date': fresh_start_date,
                'at': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            session.pop('cash_flow_today_opening_override', None)
            flash('Cash Flow fresh-start view reset. Existing entries are hidden from this report and today opening is Rs. 0.', 'success')
            return redirect(url_for('cash_flow', from_date=fresh_start_date, to_date=fresh_start_date, filter_type='all'))
        if action == 'clear_opening_override':
            session.pop('cash_flow_today_opening_override', None)
            flash('Today cash flow opening override cleared. Opening is back to Rs. 0.', 'success')
        else:
            opening_override_amount = _money_round(request.form.get('today_opening_override', 0))
            session['cash_flow_today_opening_override'] = {
                'date': fresh_start_date,
                'amount': opening_override_amount,
            }
            flash(f'Today cash flow opening override set to Rs. {opening_override_amount:,.0f}. Source accounts were not changed.', 'success')
        return redirect(url_for('cash_flow', from_date=fresh_start_date, to_date=fresh_start_date, filter_type='all'))

    try:
        from_date_dt = datetime.strptime(from_date, '%Y-%m-%d').date()
    except Exception:
        from_date_dt = fresh_start_dt
        from_date = fresh_start_date
    try:
        to_date_dt = datetime.strptime(to_date, '%Y-%m-%d').date()
    except Exception:
        to_date_dt = fresh_start_dt
        to_date = fresh_start_date

    fresh_start_clamped = False
    if to_date_dt < from_date_dt:
        to_date_dt = from_date_dt
        to_date = from_date_dt.strftime('%Y-%m-%d')

    today_opening_override = _cash_flow_today_opening_override(fresh_start_date)
    if opening_balance_input:
        try:
            opening_balance = float(opening_balance_input)
        except ValueError:
            opening_balance = 0.0
    elif from_date_dt == fresh_start_dt:
        opening_balance = today_opening_override if today_opening_override is not None else 0.0
    else:
        opening_balance = _automatic_cash_opening_balance(from_date_dt)

    fresh_start_cutoff = _cash_flow_fresh_start_cutoff(fresh_start_date)
    hide_existing_today_entries = from_date_dt == fresh_start_dt
    posted_after = fresh_start_cutoff if hide_existing_today_entries else None

    filter_account_id = source.get('account_id', type=int)
    filter_status = (filter_kwargs.get('status') or 'active').strip().lower()
    all_rows = collect_cash_flow_rows(
        from_date, to_date,
        posted_after=posted_after,
        include_voided=(filter_status in ('voided', 'all')),
    )
    all_rows.sort(key=_cf_sort_key)

    display_rows = filter_cash_flow_rows(all_rows, {
        **filter_kwargs,
        'account_id': filter_account_id,
        'filter_type': filter_type,
        'status': filter_status,
    })
    if export_scope == 'all' and (export_csv == '1' or export_pdf == '1'):
        display_rows = list(all_rows)
        if filter_status in ('active', 'voided'):
            display_rows = [r for r in display_rows if (r.get('status') or 'active') == filter_status]

    closing_balance = apply_running_balance(display_rows, opening_balance, account_id=filter_account_id)
    summary = summarize_cash_flow_rows(display_rows, account_id=filter_account_id)
    total_cash_in = summary['total_cash_in']
    total_cash_out = summary['total_cash_out']

    try:
        adjustment_date_dt = datetime.strptime(adjustment_date_input, '%Y-%m-%d').date()
    except Exception:
        adjustment_date_dt = datetime.strptime(to_date, '%Y-%m-%d').date()

    adjustment_entry = CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=adjustment_date_dt).first()
    if request.method == 'POST' and action == 'delete':
        if adjustment_entry:
            audit = CashFlowReconciliationAudit(
                reconciliation_id=adjustment_entry.id,
                adjustment_date=adjustment_entry.adjustment_date,
                change_type='DELETE',
                old_physical_cash=adjustment_entry.physical_cash_available,
                old_difference=adjustment_entry.difference if adjustment_entry.difference is not None else adjustment_entry.amount,
                old_reason=adjustment_entry.reason or adjustment_entry.note,
                changed_by=_current_username(),
                changed_at=pk_model_now(),
            )
            db.session.add(audit)
            adjustment_entry.physical_cash_available = None
            adjustment_entry.calculated_closing = None
            adjustment_entry.difference = None
            adjustment_entry.reason = None
            adjustment_entry.amount = 0
            adjustment_entry.note = 'Reconciliation removed; audit trail retained.'
            adjustment_entry.edited_by = _current_username()
            adjustment_entry.edited_date = pk_model_now()
            adjustment_entry.edit_count = (adjustment_entry.edit_count or 0) + 1
            db.session.commit()
            flash(f'Reconciliation removed for {adjustment_date_dt.strftime("%Y-%m-%d")}. Audit trail retained.', 'success')
        return _back()

    if request.method == 'POST' and action == 'save_reconciliation':
        try:
            physical_cash_available = parse_physical_cash_amount(physical_cash_input)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return _back()
        difference = compute_physical_cash_difference(physical_cash_available, closing_balance)
        username = _current_username()
        if not adjustment_entry:
            adjustment_entry = CashFlowDifferenceAdjustment(
                adjustment_date=adjustment_date_dt,
                created_by=username,
                created_at=pk_model_now(),
                edit_count=0,
            )
            db.session.add(adjustment_entry)
            db.session.flush()
            change_type = 'CREATE'
            old_physical_cash = None
            old_difference = None
            old_reason = None
        else:
            change_type = 'EDIT' if adjustment_entry.physical_cash_available is not None else 'CREATE'
            old_physical_cash = adjustment_entry.physical_cash_available
            old_difference = adjustment_entry.difference if adjustment_entry.difference is not None else adjustment_entry.amount
            old_reason = adjustment_entry.reason or adjustment_entry.note
            adjustment_entry.old_physical_cash = old_physical_cash
            adjustment_entry.edited_by = username
            adjustment_entry.edited_date = pk_model_now()

        adjustment_entry.physical_cash_available = physical_cash_available
        adjustment_entry.calculated_closing = _money_round(closing_balance)
        adjustment_entry.difference = difference
        adjustment_entry.amount = difference
        adjustment_entry.reason = reconciliation_reason
        adjustment_entry.note = reconciliation_reason
        adjustment_entry.edit_count = (adjustment_entry.edit_count or 0) + 1
        db.session.add(CashFlowReconciliationAudit(
            reconciliation_id=adjustment_entry.id,
            adjustment_date=adjustment_date_dt,
            change_type=change_type,
            old_physical_cash=old_physical_cash,
            new_physical_cash=physical_cash_available,
            old_difference=old_difference,
            new_difference=difference,
            old_reason=old_reason,
            new_reason=reconciliation_reason,
            changed_by=username,
            changed_at=pk_model_now(),
        ))
        db.session.commit()
        flash(
            f'Reconciliation saved for {adjustment_date_dt.strftime("%Y-%m-%d")}. '
            f'Next day opening will be Rs. {physical_cash_available:,.0f}. Account balances were not changed.',
            'success',
        )
        return _back()

    adjustment_entry = CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=adjustment_date_dt).first()
    physical_cash_available = adjustment_entry.physical_cash_available if adjustment_entry and adjustment_entry.physical_cash_available is not None else None
    adjustment_amount = float((adjustment_entry.difference if adjustment_entry and adjustment_entry.difference is not None else adjustment_entry.amount) or 0) if adjustment_entry else 0.0
    reconciliation_reason = (adjustment_entry.reason or adjustment_entry.note or '') if adjustment_entry else ''
    adjusted_closing_balance = physical_cash_available if physical_cash_available is not None else closing_balance

    if export_csv == '1':
        buf = io.StringIO()
        import csv
        writer = csv.writer(buf)
        writer.writerow([
            'Date', 'Type', 'Account', 'Category', 'Subcategory', 'Party',
            'Amount', 'Received', 'Spent', 'Transfer', 'Description', 'Notes',
            'Reference', 'Source', 'Created By', 'Status',
        ])
        for r in display_rows:
            writer.writerow([
                r.get('date'), r.get('tx_type_label'), r.get('account_display'),
                r.get('category'), r.get('subcategory'), r.get('party_name'),
                r.get('amount'), r.get('cash_in') or '', r.get('cash_out') or '',
                r.get('transfer_amount') or '', r.get('description'), r.get('note'),
                r.get('reference'), r.get('origin_label'), r.get('created_by'),
                r.get('status'),
            ])
        resp = make_response(buf.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename=cash_flow_{from_date}_{to_date}.csv'
        return resp

    cash_accounts = _cf_company_accounts(active_only=True)
    cash_accounts_list = [a for a in cash_accounts if (a.category or 'cash').lower() == 'cash']
    bank_accounts_list = [a for a in cash_accounts if (a.category or '').lower() == 'bank']
    cash_total = sum(_money_round(a.balance or 0) for a in cash_accounts_list)
    bank_total = sum(_money_round(a.balance or 0) for a in bank_accounts_list)

    # Per-account activity within the currently filtered rows (for cash/bank cards).
    account_activity = {}
    for r in display_rows:
        if (r.get('status') or 'active') != 'active':
            continue
        rtype = r.get('type')
        aid = r.get('account_id')
        if aid:
            act = account_activity.setdefault(aid, {'in': 0.0, 'out': 0.0, 'transfer_in': 0.0, 'transfer_out': 0.0})
            if rtype == CF_DIR_IN:
                act['in'] += float(r.get('cash_in') or 0)
            elif rtype == CF_DIR_OUT:
                act['out'] += float(r.get('cash_out') or 0)
            elif rtype == CF_DIR_TRANSFER:
                act['transfer_out'] += float(r.get('transfer_amount') or 0)
        aid2 = r.get('account_to_id')
        if aid2 and rtype == CF_DIR_TRANSFER:
            act2 = account_activity.setdefault(aid2, {'in': 0.0, 'out': 0.0, 'transfer_in': 0.0, 'transfer_out': 0.0})
            act2['transfer_in'] += float(r.get('transfer_amount') or 0)

    cf_categories = CashFlowCategory.query.order_by(CashFlowCategory.sort_order, CashFlowCategory.name).all()
    cf_subcategories = CashFlowSubcategory.query.order_by(CashFlowSubcategory.name).all()
    cf_parties = CashFlowParty.query.filter_by(is_active=True).order_by(CashFlowParty.name).all()
    cf_parties_all = CashFlowParty.query.order_by(CashFlowParty.name).all()
    created_by_options = sorted({
        (r.get('created_by') or '').strip()
        for r in all_rows
        if (r.get('created_by') or '').strip()
    })
    account_options = [{'id': a.id, 'label': _cf_account_label(a)} for a in cash_accounts]
    source_options = [('all', 'All'), ('manual', 'Manual'), ('system', 'System-generated')]
    for key, label in CF_SOURCE_LABELS.items():
        if key == 'MANUAL_CASH_FLOW':
            continue
        source_options.append((key.lower(), label))

    today_str = fresh_start_date
    yesterday_str = (fresh_start_dt - timedelta(days=1)).strftime('%Y-%m-%d')
    this_week_str = (fresh_start_dt - timedelta(days=fresh_start_dt.weekday())).strftime('%Y-%m-%d')
    this_month_str = fresh_start_dt.replace(day=1).strftime('%Y-%m-%d')
    last_30_days_str = (fresh_start_dt - timedelta(days=30)).strftime('%Y-%m-%d')

    common = dict(
        rows=display_rows,
        cash_accounts=cash_accounts,
        cash_accounts_list=cash_accounts_list,
        bank_accounts_list=bank_accounts_list,
        cash_total=cash_total,
        bank_total=bank_total,
        account_activity=account_activity,
        account_options=account_options,
        cf_categories=cf_categories,
        cf_subcategories=cf_subcategories,
        cf_parties=cf_parties,
        cf_parties_all=cf_parties_all,
        used_category_ids=cf_used_category_ids(),
        used_subcategory_ids=cf_used_subcategory_ids(),
        used_party_ids=cf_used_party_ids(),
        default_movement_datetime=pk_now().strftime('%Y-%m-%dT%H:%M'),
        party_types=CF_PARTY_TYPES,
        source_options=source_options,
        created_by_options=created_by_options,
        today_str=today_str,
        yesterday_str=yesterday_str,
        this_week_str=this_week_str,
        this_month_str=this_month_str,
        last_30_days_str=last_30_days_str,
        breakdown_cat=summary['breakdown_cat'],
        breakdown_party=summary['breakdown_party'],
        breakdown_account=summary['breakdown_account'],
        filter_origin=filter_kwargs['origin'],
        filter_category=filter_kwargs['category'],
        filter_subcategory=filter_kwargs['subcategory'],
        filter_party_type=filter_kwargs['party_type'],
        filter_party=filter_kwargs['party'],
        filter_account_id=filter_account_id,
        filter_q=filter_kwargs['q'],
        filter_notes=filter_kwargs['notes'],
        filter_reference=filter_kwargs['reference'],
        filter_description=filter_kwargs['description'],
        filter_created_by=filter_kwargs['created_by'],
        filter_status=filter_status,
        filter_amount_min=filter_kwargs['amount_min'],
        filter_amount_max=filter_kwargs['amount_max'],
        from_date=from_date, to_date=to_date,
        filter_type=filter_type,
        opening_balance=opening_balance,
        opening_balance_input=opening_balance_input,
        adjustment_amount=adjustment_amount,
        physical_cash_available=physical_cash_available,
        reconciliation_reason=reconciliation_reason,
        show_delete_button=bool(adjustment_entry and adjustment_entry.physical_cash_available is not None),
        adjusted_closing_balance=adjusted_closing_balance,
        adjustment_date_input=adjustment_date_input,
        closing_balance=closing_balance,
        total_cash_in=total_cash_in,
        total_cash_out=total_cash_out,
        total_transfer_in=summary['total_transfer_in'],
        total_transfer_out=summary['total_transfer_out'],
        generated_at=pk_now().strftime('%Y-%m-%d %H:%M'),
        settings=None,
        fresh_start_date=fresh_start_date,
        fresh_start_cutoff=fresh_start_cutoff,
        fresh_start_clamped=fresh_start_clamped,
        today_opening_override=today_opening_override,
        is_fresh_start_view=(from_date_dt == fresh_start_dt),
    )

    if export_pdf == '1':
        rendered = render_template('cash_flow.html', pdf_mode=True, **common)
        pdf_resp = _try_render_weasy_pdf(rendered, f'cash_flow_{from_date}_{to_date}.pdf')
        if pdf_resp:
            return pdf_resp
        return Response(rendered, content_type='text/html')

    return render_template('cash_flow.html', pdf_mode=False, **common)


@bp.route('/cash_flow_differences')
@login_required
def cash_flow_differences():
    from_date = request.args.get('from_date', '').strip()
    to_date = request.args.get('to_date', '').strip()
    workflow_filter = request.args.get('workflow_filter', 'all').strip()
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = 25

    query = CashFlowDifferenceAdjustment.query
    if from_date:
        try:
            query = query.filter(CashFlowDifferenceAdjustment.adjustment_date >= datetime.strptime(from_date, '%Y-%m-%d').date())
        except Exception:
            from_date = ''
    if to_date:
        try:
            query = query.filter(CashFlowDifferenceAdjustment.adjustment_date <= datetime.strptime(to_date, '%Y-%m-%d').date())
        except Exception:
            to_date = ''
    if workflow_filter == 'new':
        query = query.filter(CashFlowDifferenceAdjustment.physical_cash_available.isnot(None))
    elif workflow_filter == 'legacy':
        query = query.filter(CashFlowDifferenceAdjustment.physical_cash_available.is_(None))
    else:
        workflow_filter = 'all'

    total_count = CashFlowDifferenceAdjustment.query.count()
    new_workflow_count = CashFlowDifferenceAdjustment.query.filter(CashFlowDifferenceAdjustment.physical_cash_available.isnot(None)).count()
    legacy_count = CashFlowDifferenceAdjustment.query.filter(CashFlowDifferenceAdjustment.physical_cash_available.is_(None)).count()
    total_audit_events = CashFlowReconciliationAudit.query.count()

    filtered_count = query.count()
    pages = max(1, (filtered_count + per_page - 1) // per_page)
    page = min(page, pages)
    reconciliations = query.order_by(
        CashFlowDifferenceAdjustment.adjustment_date.desc(),
        CashFlowDifferenceAdjustment.id.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()
    for rec in reconciliations:
        rec.display_opening_balance = _automatic_cash_opening_balance(rec.adjustment_date)
        rec.display_cash_in, rec.display_cash_out = _cash_flow_in_out_between(rec.adjustment_date, rec.adjustment_date)
        if rec.calculated_closing is None:
            rec.display_calculated_closing = rec.display_opening_balance + rec.display_cash_in - rec.display_cash_out
        else:
            rec.display_calculated_closing = rec.calculated_closing

    return render_template(
        'cash_flow_differences.html',
        reconciliations=reconciliations,
        total_count=total_count,
        new_workflow_count=new_workflow_count,
        legacy_count=legacy_count,
        total_audit_events=total_audit_events,
        from_date=from_date,
        to_date=to_date,
        workflow_filter=workflow_filter,
        page=page,
        pages=pages,
    )


@bp.route('/cash_flow_differences/<int:rec_id>')
@login_required
def cash_flow_reconciliation_detail(rec_id):
    reconciliation = CashFlowDifferenceAdjustment.query.get_or_404(rec_id)
    audit_trail = CashFlowReconciliationAudit.query.filter_by(
        adjustment_date=reconciliation.adjustment_date
    ).order_by(CashFlowReconciliationAudit.changed_at.asc(), CashFlowReconciliationAudit.id.asc()).all()
    return render_template(
        'cash_flow_reconciliation_detail.html',
        reconciliation=reconciliation,
        audit_trail=audit_trail,
    )
